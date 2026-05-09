"""GitHub Issues adapter.

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
assignees[0].login             assignee (first assignee only)

Severity extraction: if any label has the form ``severity:<level>``, that
level is used.  Otherwise the ticket is classified as "informational".

Write routing
-------------

On ``write(ticket)``, the adapter first searches for an existing GitHub Issue
carrying the label ``ticketsync:source_id=<ticket.source_id>``.  If found it
sends ``PATCH`` to update that issue.  If not found it sends ``POST`` to
create a new issue and adds the tracking label.

This approach is safe for cross-adapter pipelines (e.g. source=OpsCenter →
dest=GitHub): OpsCenter IDs like ``oi-abc123`` would previously have been
routed through ``PATCH`` because the old code used ``source_id.isdigit()``,
which is wrong.  The label-based lookup is system-agnostic.

The ``owner`` and ``repo`` constructor parameters form the base URL prefix
for all API calls: ``https://api.github.com/repos/{owner}/{repo}``.

Assignees
---------

``from_ticket`` includes ``ticket.assignee`` as a single-element
``assignees`` list when non-empty.  GitHub may silently ignore assignees
that are not collaborators on the repo; this adapter does not verify
membership.

OpsCenter (write destination) does not support assignees — they are dropped
silently by ``OpsCenterAdapter.from_ticket``.  This is documented in
ADAPTERS.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ticketsync.models import SeverityLevel, Ticket, TicketStatus
from ticketsync.metadata import TriageMetadata


_ALL_SEVERITY_LEVELS: tuple[SeverityLevel, ...] = (
    "critical", "high", "medium", "low", "informational"
)


def _parse_metadata_comment(body: str) -> TriageMetadata | None:
    """Parse a TriageMetadata from a structured comment body.

    Expected format (one key: value per line after the marker comment)::

        <!-- TicketSync triage metadata (do not edit) -->
        Assignee: alice@example.com
        Priority: 2
        Triage notes: Confirmed malicious.
        Resolution: Blocked at perimeter.
        Triaged at: 2026-05-09T12:00:00+00:00

    Returns None if the body cannot be parsed (defensive — never crash).
    """
    kwargs: dict[str, object] = {}
    for line in body.splitlines():
        if line.startswith("<!--") or not line.strip():
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        key_lower = key.strip().lower().replace(" ", "_")
        if key_lower == "assignee":
            kwargs["assignee"] = value
        elif key_lower == "priority":
            try:
                kwargs["priority"] = int(value)
            except ValueError:
                pass
        elif key_lower == "severity_override":
            kwargs["severity_override"] = value
        elif key_lower == "triage_notes":
            kwargs["triage_notes"] = value
        elif key_lower == "resolution":
            kwargs["resolution"] = value
        elif key_lower == "triaged_at":
            try:
                normalised = value.replace("Z", "+00:00") if value.endswith("Z") else value
                dt = datetime.fromisoformat(normalised)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                kwargs["triaged_at"] = dt
            except ValueError:
                pass
    try:
        return TriageMetadata(**kwargs)  # type: ignore[arg-type]
    except Exception:
        return None

# Label prefix used to track which source ticket was written to an issue.
# Format: "ticketsync:source_id=<source_system>/<source_id>"
_TRACKING_LABEL_PREFIX = "ticketsync:source_id="


def _tracking_label(ticket: Ticket) -> str:
    """Return the label string that uniquely identifies this source ticket."""
    # Include source_system so that two different systems with the same
    # source_id (e.g. OpsCenter "1" vs GitHub "1") map to different labels.
    safe = f"{ticket.source_system}/{ticket.source_id}".replace(" ", "_")
    # GitHub label names are limited to 50 chars in practice — truncate if needed.
    full = f"{_TRACKING_LABEL_PREFIX}{safe}"
    return full[:50]


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

        # Non-severity, non-tracking labels become tags.
        tags: list[str] = [
            lbl["name"]
            for lbl in labels
            if (
                not str(lbl.get("name", "")).startswith("severity:")
                and not str(lbl.get("name", "")).startswith(_TRACKING_LABEL_PREFIX)
            )
        ]

        # First assignee only (GitHub supports multiple, IR supports one string).
        assignee: str = ""
        assignees: list[dict[str, Any]] = issue.get("assignees", [])
        if assignees and isinstance(assignees, list) and len(assignees) > 0:
            first = assignees[0]
            if isinstance(first, dict):
                assignee = str(first.get("login", ""))

        return Ticket.model_validate({
            "source_system": self.system_name,
            "source_id": str(issue.get("number", "")),
            "title": str(issue.get("title", "")),
            "description": str(issue.get("body", "") or ""),
            "severity": severity,
            "status": status,
            "tags": tags,
            "assignee": assignee,
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

        payload: dict[str, object] = {
            "title": ticket.title,
            "body": ticket.description,
            "labels": labels,
            "state": "closed" if ticket.status in ("resolved", "closed") else "open",
        }

        # Include assignee if set.  GitHub ignores non-collaborators silently.
        if ticket.assignee:
            payload["assignees"] = [ticket.assignee]

        return payload

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

    def _find_existing_issue_number(self, ticket: Ticket) -> str | None:
        """Search for a GitHub Issue already written for this source ticket.

        Returns the issue number as a string, or ``None`` if not found.

        The search uses the ``ticketsync:source_id=<system>/<id>`` label
        which is attached to every issue created by this adapter.  This
        lookup is reliable across process restarts and for cross-adapter
        pipelines where source_id may be any string (numeric or not).
        """
        label = _tracking_label(ticket)
        url = f"{self._base}/issues"
        params: dict[str, Any] = {
            "labels": label,
            "state": "all",
            "per_page": 1,
        }
        result: Any = self._client.get(url, params=params)
        if isinstance(result, list) and len(result) > 0:
            first: dict[str, Any] = result[0]
            num = first.get("number")
            if num is not None:
                return str(num)
        return None

    def write(self, ticket: Ticket) -> str:
        """Create or update a GitHub Issue; return its issue number as a string.

        Decision logic
        --------------
        1. If the ticket originated from this adapter (``ticket.source_system``
           matches ``self.system_name``) AND ``ticket.source_id`` is a non-empty
           numeric string, use it directly as the issue number and ``PATCH``.
        2. Otherwise, search GitHub for an existing issue carrying the label
           ``ticketsync:source_id=<source_system>/<source_id>``.
           - If found → ``PATCH`` that issue.
           - If not found → ``POST`` a new issue; the tracking label is
             automatically included so future writes update in-place.

        Case 1 handles the normal update path (ticket round-trips within GitHub).
        Case 2 handles cross-adapter pipelines (e.g. OpsCenter → GitHub) where
        the source_id is not a GitHub issue number.
        """
        payload = self.from_ticket(ticket)

        # Case 1: native GitHub ticket — source_id IS the issue number.
        if (
            ticket.source_system == self.system_name
            and ticket.source_id
            and ticket.source_id.isdigit()
        ):
            url = f"{self._base}/issues/{ticket.source_id}"
            response: dict[str, Any] = self._client.patch(url, json=payload)
            return str(response.get("number", ""))

        # Case 2: cross-adapter or new issue — use tracking-label lookup.
        tracking = _tracking_label(ticket)
        raw_labels = payload.get("labels", [])
        existing_labels: list[str] = list(raw_labels) if isinstance(raw_labels, list) else []
        if tracking not in existing_labels:
            existing_labels.append(tracking)
        payload = dict(payload)
        payload["labels"] = existing_labels

        existing_number = self._find_existing_issue_number(ticket)

        if existing_number is not None:
            url = f"{self._base}/issues/{existing_number}"
            response = self._client.patch(url, json=payload)
        else:
            url = f"{self._base}/issues"
            response = self._client.post(url, json=payload)

        return str(response.get("number", ""))

    # ------------------------------------------------------------------
    # Tag-based sync write-back
    # ------------------------------------------------------------------

    def mark_synced(self, source_id: str) -> None:
        """Add the ``ticketsync:synced`` label to a GitHub Issue.

        Called by the engine after a successful write to the destination
        when ``dedup_strategy: tag-based`` is configured.  This label
        persists across process restarts and prevents re-syncing the same
        issue.

        Parameters
        ----------
        source_id:
            The GitHub issue number (as returned by ``write`` or from
            ``to_ticket().source_id``).

        Note: If the issue does not exist, the PATCH will raise; callers
        should wrap in try/except to make this non-fatal.
        """
        url = f"{self._base}/issues/{source_id}/labels"
        self._client.post(url, json={"labels": ["ticketsync:synced"]})

    # ------------------------------------------------------------------
    # Destination-check dedup
    # ------------------------------------------------------------------

    def find_by_source_coordinates(
        self, source_system: str, source_id: str
    ) -> str | None:
        """Return the GitHub issue number for a ticket from another system.

        Searches for an issue carrying the label
        ``ticketsync:source_id=<source_system>/<source_id>``.

        Returns the issue number string, or ``None`` if not found.  Used by
        the engine when ``dedup_strategy: destination-check`` is configured.
        """
        # Build a fake minimal ticket to reuse _tracking_label logic.
        label = f"{_TRACKING_LABEL_PREFIX}{source_system}/{source_id}"[:50]
        url = f"{self._base}/issues"
        params: dict[str, Any] = {
            "labels": label,
            "state": "all",
            "per_page": 1,
        }
        result: Any = self._client.get(url, params=params)
        if isinstance(result, list) and len(result) > 0:
            first: dict[str, Any] = result[0]
            num = first.get("number")
            if num is not None:
                return str(num)
        return None

    # ------------------------------------------------------------------
    # Triage metadata (MetadataAdapter optional extension)
    # ------------------------------------------------------------------

    def write_metadata(self, source_id: str, metadata: TriageMetadata) -> None:
        """Persist triage metadata for a GitHub Issue.

        Strategy: add a structured comment to the issue that is human-readable
        when viewed in the GitHub UI.  GitHub Issues have no native hidden
        field equivalent to OpsCenter OperationalData, so a comment is the
        only option.

        The comment starts with an HTML comment marker so automated tooling
        can locate it, but the rest of the content is plain text that any
        human can read.

        Parameters
        ----------
        source_id:
            GitHub issue number (as a string).
        metadata:
            Triage decisions to persist.
        """
        meta = metadata.with_timestamp()
        body = meta.to_human_readable()
        url = f"{self._base}/issues/{source_id}/comments"
        self._client.post(url, json={"body": body})

    def read_metadata(self, source_id: str) -> TriageMetadata | None:
        """Read triage metadata from a GitHub Issue's comments.

        Scans issue comments for one starting with the TicketSync metadata
        marker.  Returns the most recently written metadata, or ``None`` if
        not found.

        Note: This reads from structured comments because GitHub Issues have
        no native hidden metadata store.  The comment format is intentionally
        human-readable (see ``TriageMetadata.to_human_readable``).
        """
        url = f"{self._base}/issues/{source_id}/comments"
        result: Any = self._client.get(url, params={"per_page": 100})
        if not isinstance(result, list):
            return None

        marker = "<!-- TicketSync triage metadata"
        # Scan in reverse to get the most recent metadata comment
        for comment in reversed(result):
            if not isinstance(comment, dict):
                continue
            body: str = str(comment.get("body", ""))
            if marker in body:
                return _parse_metadata_comment(body)
        return None
