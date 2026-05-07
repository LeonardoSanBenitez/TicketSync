"""Jira webhook adapter for TicketSync.

Parses Jira webhook payloads as sent by the Jira Webhooks feature.

Payload reference:
    https://developer.atlassian.com/server/jira/platform/webhooks/

Example payload::

    {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "id": "10001",
            "key": "PROJ-123",
            "self": "https://jira.example.com/rest/api/2/issue/10001",
            "fields": {
                "summary": "Login button broken on mobile",
                "description": "Steps to reproduce...",
                "status": {"name": "To Do"},
                "priority": {"name": "High"},
                "issuetype": {"name": "Bug"},
                "labels": ["frontend", "mobile"],
                "created": "2024-01-15T10:30:00.000+0000",
                "project": {"key": "PROJ", "name": "My Project"}
            }
        }
    }
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from ticket_sync.adapters.base import BaseAdapter
from ticket_sync.models import Ticket, TicketPriority, TicketSource, TicketStatus

# Jira priority name (case-insensitive prefix match) -> TicketPriority
_PRIORITY_MAP: Dict[str, TicketPriority] = {
    "blocker": TicketPriority.CRITICAL,
    "critical": TicketPriority.CRITICAL,
    "highest": TicketPriority.CRITICAL,
    "high": TicketPriority.HIGH,
    "major": TicketPriority.HIGH,
    "medium": TicketPriority.MEDIUM,
    "normal": TicketPriority.MEDIUM,
    "low": TicketPriority.LOW,
    "lowest": TicketPriority.LOW,
    "minor": TicketPriority.LOW,
    "trivial": TicketPriority.LOW,
}

# Jira status category -> TicketStatus
_STATUS_MAP: Dict[str, TicketStatus] = {
    "to do": TicketStatus.OPEN,
    "open": TicketStatus.OPEN,
    "new": TicketStatus.OPEN,
    "backlog": TicketStatus.OPEN,
    "in progress": TicketStatus.IN_PROGRESS,
    "in review": TicketStatus.IN_PROGRESS,
    "selected for development": TicketStatus.OPEN,
    "pending": TicketStatus.PENDING,
    "blocked": TicketStatus.PENDING,
    "waiting": TicketStatus.PENDING,
    "resolved": TicketStatus.RESOLVED,
    "done": TicketStatus.RESOLVED,
    "closed": TicketStatus.CLOSED,
    "won't do": TicketStatus.CLOSED,
    "won't fix": TicketStatus.CLOSED,
    "duplicate": TicketStatus.CLOSED,
}

# webhookEvent -> relevant action tag
_EVENT_TAG: Dict[str, str] = {
    "jira:issue_created": "created",
    "jira:issue_updated": "updated",
    "jira:issue_deleted": "deleted",
}


def _map_priority(priority_name: Optional[str]) -> TicketPriority:
    if not priority_name:
        return TicketPriority.UNKNOWN
    key = priority_name.lower().strip()
    return _PRIORITY_MAP.get(key, TicketPriority.UNKNOWN)


def _map_status(status_name: Optional[str]) -> TicketStatus:
    if not status_name:
        return TicketStatus.UNKNOWN
    key = status_name.lower().strip()
    return _STATUS_MAP.get(key, TicketStatus.UNKNOWN)


class JiraAdapter(BaseAdapter):
    """Parse Jira webhook payloads into Tickets.

    Supports both Jira Cloud and Jira Server webhook formats (they differ
    only in minor date format details, which this adapter handles).

    >>> adapter = JiraAdapter()
    >>> payload = {
    ...     "webhookEvent": "jira:issue_created",
    ...     "issue": {
    ...         "id": "10001",
    ...         "key": "PROJ-123",
    ...         "fields": {
    ...             "summary": "Login button broken",
    ...             "description": "Steps to reproduce...",
    ...             "status": {"name": "To Do"},
    ...             "priority": {"name": "High"},
    ...             "labels": ["frontend"],
    ...             "created": "2024-01-15T10:30:00.000+0000",
    ...         }
    ...     }
    ... }
    >>> ticket = adapter.parse(payload)
    >>> ticket.title
    'Login button broken'
    >>> ticket.source.value
    'jira'
    >>> ticket.priority.value
    'high'
    """

    def parse(self, payload: Dict[str, Any]) -> Ticket:
        """Parse a Jira webhook payload into a Ticket.

        Args:
            payload: Decoded JSON Jira webhook payload.

        Returns:
            A :class:`Ticket` representing the Jira issue.

        Raises:
            KeyError: If the ``issue`` key or ``fields.summary`` is missing.
        """
        issue: Dict[str, Any] = payload["issue"]
        fields: Dict[str, Any] = issue.get("fields", {})

        issue_key: str = issue.get("key", issue.get("id", ""))
        title: str = fields["summary"]
        description: str = fields.get("description") or ""

        priority = _map_priority(
            (fields.get("priority") or {}).get("name")
        )
        status = _map_status(
            (fields.get("status") or {}).get("name")
        )

        # Tags: Jira labels + issue type
        tags: List[str] = list(fields.get("labels") or [])
        issuetype = (fields.get("issuetype") or {}).get("name")
        if issuetype:
            tags.append(f"type:{issuetype.lower()}")

        event_type: str = payload.get("webhookEvent", "")
        action_tag = _EVENT_TAG.get(event_type)
        if action_tag:
            tags.append(f"action:{action_tag}")

        created_at: Optional[datetime] = None
        raw_created = fields.get("created")
        if raw_created:
            try:
                # Jira uses: "2024-01-15T10:30:00.000+0000"
                normalized = str(raw_created).replace("+0000", "+00:00")
                created_at = datetime.fromisoformat(normalized)
            except ValueError:
                created_at = None

        project = fields.get("project") or {}

        metadata: Dict[str, Any] = {
            "issue_key": issue_key,
            "webhook_event": event_type,
            "project_key": project.get("key"),
            "project_name": project.get("name"),
            "issue_url": issue.get("self"),
        }

        return Ticket(
            title=title,
            description=description,
            source=TicketSource.JIRA,
            source_id=issue_key,
            priority=priority,
            status=status,
            tags=tags,
            metadata={k: v for k, v in metadata.items() if v is not None},
            created_at=created_at,
        )
