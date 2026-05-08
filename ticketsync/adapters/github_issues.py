"""GitHub Issues adapter (stub implementation).

Maps between GitHub Issues JSON payloads and the TicketSync IR.  All HTTP
calls are mediated through an injected ``client`` object so tests can run
without a real GitHub token.

The client must expose:
    client.get(url: str, params: dict | None) -> dict | list
    client.post(url: str, json: dict) -> dict
    client.patch(url: str, json: dict) -> dict

Field mapping
-------------

GitHub concept                -> Ticket IR field
-----------------------------  ------------------------------------
number (str)                   source_id
title                          title
body                           description
labels -> name="severity:*"    severity (extracted label)
labels -> other names          tags
state ("open"/"closed")        status ("open" / "resolved")
created_at                     created_at
updated_at                     updated_at
html_url                       external_url

Severity extraction: if any label has the form ``severity:<level>``, that
level is used.  Otherwise the ticket is classified as "informational".

The ``owner`` and ``repo`` constructor parameters form the base URL prefix
for all API calls: ``https://api.github.com/repos/{owner}/{repo}``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ticketsync.models import SeverityLevel, Ticket, TicketStatus


_ALL_SEVERITY_LEVELS: tuple[SeverityLevel, ...] = (
    "critical", "high", "medium", "low", "informational"
)


def _parse_severity(labels: list[dict[str, Any]]) -> SeverityLevel:
    for label in labels:
        name: str = label.get("name", "")
        if name.startswith("severity:"):
            level = name[len("severity:"):]
            for sev in _ALL_SEVERITY_LEVELS:
                if level == sev:
                    return sev
    return "informational"


def _parse_status(state: str) -> TicketStatus:
    if state == "closed":
        return "resolved"
    return "open"


def _ensure_utc(ts: str | datetime | None) -> datetime:
    if ts is None:
        return datetime.now(timezone.utc)
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(ts.rstrip("Z"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class GitHubIssuesAdapter:
    """Adapter for GitHub Issues via the REST API.

    Parameters
    ----------
    client:
        An HTTP client object with ``get``, ``post``, and ``patch`` methods.
    owner:
        GitHub organization or user name.
    repo:
        Repository name.
    system_name:
        The ``source_system`` label embedded in produced tickets.
    """

    system_name: str

    def __init__(
        self,
        client: Any,
        owner: str,
        repo: str,
        system_name: str = "github_issues",
    ) -> None:
        self._client = client
        self._owner = owner
        self._repo = repo
        self.system_name = system_name
        self._base = f"https://api.github.com/repos/{owner}/{repo}"

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def to_ticket(self, raw: dict[str, object]) -> Ticket:
        """Map a GitHub Issue JSON dict to a Ticket IR."""
        issue: dict[str, Any] = dict(raw)
        labels: list[dict[str, Any]] = issue.get("labels", [])

        severity = _parse_severity(labels)
        status = _parse_status(str(issue.get("state", "open")))

        # Non-severity labels become tags
        tags: list[str] = [
            lbl["name"]
            for lbl in labels
            if not str(lbl.get("name", "")).startswith("severity:")
        ]

        return Ticket.model_validate({
            "source_system": self.system_name,
            "source_id": str(issue.get("number", "")),
            "title": str(issue.get("title", "")),
            "description": str(issue.get("body", "") or ""),
            "severity": severity,
            "status": status,
            "tags": tags,
            "created_at": _ensure_utc(issue.get("created_at")),
            "updated_at": _ensure_utc(issue.get("updated_at")),
            "external_url": str(issue.get("html_url", "")),
            "raw": issue,
        })

    def from_ticket(self, ticket: Ticket) -> dict[str, object]:
        """Map a Ticket IR to a GitHub Issue create/update payload."""
        labels: list[str] = list(ticket.tags)
        if ticket.severity != "informational":
            labels.append(f"severity:{ticket.severity}")

        return {
            "title": ticket.title,
            "body": ticket.description,
            "labels": labels,
            "state": "closed" if ticket.status in ("resolved", "closed") else "open",
        }

    def fetch_new(self, since: datetime | None = None) -> list[dict[str, object]]:
        """Fetch issues updated after ``since``, or all open issues if None."""
        params: dict[str, Any] = {"state": "all", "per_page": 100}
        if since is not None:
            params["since"] = since.isoformat()

        url = f"{self._base}/issues"
        result: Any = self._client.get(url, params=params)
        if isinstance(result, list):
            return list(result)
        return []

    def write(self, ticket: Ticket) -> str:
        """Create a GitHub Issue and return its issue number as a string."""
        payload = self.from_ticket(ticket)

        # If source_id is a numeric string, try to update (PATCH) the issue.
        if ticket.source_id and ticket.source_id.isdigit():
            url = f"{self._base}/issues/{ticket.source_id}"
            response: dict[str, Any] = self._client.patch(url, json=payload)
        else:
            url = f"{self._base}/issues"
            response = self._client.post(url, json=payload)

        return str(response.get("number", ""))
