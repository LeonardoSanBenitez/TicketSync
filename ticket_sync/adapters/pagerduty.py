"""PagerDuty webhook v2 adapter for TicketSync.

Parses PagerDuty webhook v2 payloads containing incident events.

Payload reference:
    https://developer.pagerduty.com/docs/db0fa8c8984fc-overview

The top-level envelope is::

    {
        "messages": [
            {
                "event": "incident.trigger",
                "log_entries": [...],
                "incident": { ... },
                "webhook": { ... },
                "created_on": "2024-01-15T10:30:00Z"
            }
        ]
    }

This adapter processes the *first* message in the ``messages`` array.
For batch processing, call :meth:`parse` once per message.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ticket_sync.adapters.base import BaseAdapter
from ticket_sync.models import Ticket, TicketPriority, TicketSource, TicketStatus

# PagerDuty urgency -> TicketPriority
_URGENCY_TO_PRIORITY: Dict[str, TicketPriority] = {
    "high": TicketPriority.HIGH,
    "low": TicketPriority.MEDIUM,
}

# PagerDuty status -> TicketStatus
_STATUS_MAP: Dict[str, TicketStatus] = {
    "triggered": TicketStatus.OPEN,
    "acknowledged": TicketStatus.IN_PROGRESS,
    "resolved": TicketStatus.RESOLVED,
}

# PagerDuty event type -> TicketStatus
_EVENT_STATUS_MAP: Dict[str, TicketStatus] = {
    "incident.trigger": TicketStatus.OPEN,
    "incident.acknowledge": TicketStatus.IN_PROGRESS,
    "incident.resolve": TicketStatus.RESOLVED,
    "incident.unacknowledge": TicketStatus.OPEN,
    "incident.escalate": TicketStatus.OPEN,
    "incident.delegate": TicketStatus.OPEN,
    "incident.annotate": TicketStatus.UNKNOWN,
}


class PagerDutyAdapter(BaseAdapter):
    """Parse PagerDuty webhook v2 payloads into Tickets.

    Accepts both the top-level ``{"messages": [...]}`` envelope and a bare
    single-message dict (for ease of testing and one-shot processing).

    >>> adapter = PagerDutyAdapter()
    >>> payload = {
    ...     "messages": [{
    ...         "event": "incident.trigger",
    ...         "incident": {
    ...             "id": "PT4KHLK",
    ...             "incident_number": 1234,
    ...             "title": "Database latency spike",
    ...             "status": "triggered",
    ...             "urgency": "high",
    ...             "html_url": "https://acme.pagerduty.com/incidents/PT4KHLK",
    ...             "created_at": "2024-01-15T10:30:00Z",
    ...             "service": {"name": "production-db"},
    ...         }
    ...     }]
    ... }
    >>> ticket = adapter.parse(payload)
    >>> ticket.title
    'Database latency spike'
    >>> ticket.source.value
    'pagerduty'
    >>> ticket.priority.value
    'high'
    """

    def parse(self, payload: Dict[str, Any]) -> Ticket:
        """Parse a PagerDuty v2 webhook payload into a Ticket.

        Args:
            payload: Decoded JSON payload.  May be the full
                ``{"messages": [...]}`` envelope or a bare message dict.

        Returns:
            A :class:`Ticket` from the first incident in *payload*.

        Raises:
            KeyError: If the incident ``id`` or ``title`` is missing.
        """
        # Unwrap envelope if present
        if "messages" in payload:
            message: Dict[str, Any] = payload["messages"][0]
        else:
            message = payload

        event_type: str = message.get("event", "")
        incident: Dict[str, Any] = message.get("incident", {})

        title: str = incident["title"]
        incident_id: str = incident["id"]
        incident_number: Optional[int] = incident.get("incident_number")

        description = incident.get("description", "")
        html_url = incident.get("html_url", "")
        if html_url:
            description = f"{description}\n\n{html_url}".strip()

        # Priority: prefer explicit field, fall back to urgency, then event type
        urgency = str(incident.get("urgency", "")).lower()
        priority = _URGENCY_TO_PRIORITY.get(urgency, TicketPriority.UNKNOWN)

        # Status: prefer incident.status, fall back to event type
        raw_status = str(incident.get("status", "")).lower()
        status = _STATUS_MAP.get(raw_status)
        if status is None:
            status = _EVENT_STATUS_MAP.get(event_type, TicketStatus.UNKNOWN)

        created_at: Optional[datetime] = None
        raw_created = incident.get("created_at")
        if raw_created:
            try:
                created_at = datetime.fromisoformat(
                    str(raw_created).replace("Z", "+00:00")
                )
            except ValueError:
                created_at = None

        tags: List[str] = []
        service = incident.get("service", {})
        service_name = service.get("name") or service.get("summary")
        if service_name:
            tags.append(f"service:{service_name}")
        if event_type:
            tags.append(f"event:{event_type}")

        source_id = f"PD-{incident_number}" if incident_number else incident_id

        metadata: Dict[str, Any] = {
            "incident_id": incident_id,
            "incident_number": incident_number,
            "event_type": event_type,
            "urgency": urgency,
            "html_url": html_url,
        }

        return Ticket(
            title=title,
            description=description,
            source=TicketSource.PAGERDUTY,
            source_id=source_id,
            priority=priority,
            status=status,
            tags=tags,
            metadata={k: v for k, v in metadata.items() if v is not None},
            created_at=created_at,
        )
