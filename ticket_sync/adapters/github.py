"""GitHub issues webhook adapter for TicketSync.

Parses GitHub ``issues`` webhook payloads (action: opened, edited, closed,
reopened, labeled, assigned, etc.).

Payload reference:
    https://docs.github.com/en/webhooks/webhook-events-and-payloads#issues

Example payload::

    {
        "action": "opened",
        "issue": {
            "number": 42,
            "title": "Feature request: dark mode",
            "body": "Would be great to have dark mode support.",
            "state": "open",
            "html_url": "https://github.com/owner/repo/issues/42",
            "labels": [{"name": "enhancement"}, {"name": "good first issue"}],
            "created_at": "2024-01-15T10:30:00Z",
            "user": {"login": "contributor"}
        },
        "repository": {
            "full_name": "owner/repo",
            "name": "repo"
        }
    }
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from ticket_sync.adapters.base import BaseAdapter
from ticket_sync.models import Ticket, TicketPriority, TicketSource, TicketStatus

# GitHub issue state -> TicketStatus
_STATE_MAP: Dict[str, TicketStatus] = {
    "open": TicketStatus.OPEN,
    "closed": TicketStatus.CLOSED,
}

# GitHub label names that suggest priority (case-insensitive)
_PRIORITY_LABELS: Dict[str, TicketPriority] = {
    "critical": TicketPriority.CRITICAL,
    "priority: critical": TicketPriority.CRITICAL,
    "p0": TicketPriority.CRITICAL,
    "high": TicketPriority.HIGH,
    "priority: high": TicketPriority.HIGH,
    "p1": TicketPriority.HIGH,
    "medium": TicketPriority.MEDIUM,
    "priority: medium": TicketPriority.MEDIUM,
    "p2": TicketPriority.MEDIUM,
    "low": TicketPriority.LOW,
    "priority: low": TicketPriority.LOW,
    "p3": TicketPriority.LOW,
}


def _priority_from_labels(labels: List[str]) -> TicketPriority:
    """Infer priority from GitHub issue label names."""
    for label in labels:
        key = label.lower().strip()
        if key in _PRIORITY_LABELS:
            return _PRIORITY_LABELS[key]
    return TicketPriority.UNKNOWN


class GitHubAdapter(BaseAdapter):
    """Parse GitHub issues webhook payloads into Tickets.

    Priority is inferred from label names (e.g., ``"critical"``, ``"p0"``,
    ``"high"``) since GitHub issues have no native priority field.

    >>> adapter = GitHubAdapter()
    >>> payload = {
    ...     "action": "opened",
    ...     "issue": {
    ...         "number": 42,
    ...         "title": "Dark mode support",
    ...         "body": "Please add dark mode.",
    ...         "state": "open",
    ...         "html_url": "https://github.com/org/repo/issues/42",
    ...         "labels": [{"name": "enhancement"}, {"name": "high"}],
    ...         "created_at": "2024-01-15T10:30:00Z",
    ...         "user": {"login": "contributor"},
    ...     },
    ...     "repository": {"full_name": "org/repo", "name": "repo"},
    ... }
    >>> ticket = adapter.parse(payload)
    >>> ticket.title
    'Dark mode support'
    >>> ticket.source.value
    'github'
    >>> ticket.priority.value
    'high'
    """

    def parse(self, payload: Dict[str, Any]) -> Ticket:
        """Parse a GitHub issues webhook payload into a Ticket.

        Args:
            payload: Decoded JSON GitHub issues webhook payload.

        Returns:
            A :class:`Ticket` representing the GitHub issue.

        Raises:
            KeyError: If the ``issue`` key or ``issue.title`` is missing.
        """
        action: str = payload.get("action", "")
        issue: Dict[str, Any] = payload["issue"]
        repository: Dict[str, Any] = payload.get("repository", {})

        issue_number: int = issue["number"]
        title: str = issue["title"]
        body: str = issue.get("body") or ""
        state: str = issue.get("state", "open")
        html_url: str = issue.get("html_url", "")

        status = _STATE_MAP.get(state.lower(), TicketStatus.UNKNOWN)

        # Labels
        raw_labels = issue.get("labels") or []
        label_names: List[str] = [
            lbl["name"] for lbl in raw_labels if isinstance(lbl, dict) and "name" in lbl
        ]

        priority = _priority_from_labels(label_names)

        # Tags: labels + action + repo name
        tags: List[str] = list(label_names)
        if action:
            tags.append(f"action:{action}")
        repo_name = repository.get("name")
        if repo_name:
            tags.append(f"repo:{repo_name}")

        created_at: Optional[datetime] = None
        raw_created = issue.get("created_at")
        if raw_created:
            try:
                created_at = datetime.fromisoformat(
                    str(raw_created).replace("Z", "+00:00")
                )
            except ValueError:
                created_at = None

        repo_full_name = repository.get("full_name", "")
        source_id = f"{repo_full_name}#{issue_number}" if repo_full_name else str(issue_number)

        metadata: Dict[str, Any] = {
            "issue_number": issue_number,
            "action": action,
            "html_url": html_url,
            "repository": repo_full_name,
            "author": (issue.get("user") or {}).get("login"),
        }

        return Ticket(
            title=title,
            description=body,
            source=TicketSource.GITHUB,
            source_id=source_id,
            priority=priority,
            status=status,
            tags=tags,
            metadata={k: v for k, v in metadata.items() if v is not None},
            created_at=created_at,
        )
