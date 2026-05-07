"""Core data models for TicketSync.

The Ticket class is the canonical representation of a ticket/alert/issue
across all platforms in the Libre Ticket Suite. All adapters convert
their platform-specific payloads into this structure.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class TicketPriority(str, Enum):
    """Normalized priority levels across all platforms."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class TicketStatus(str, Enum):
    """Normalized ticket lifecycle states."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    PENDING = "pending"
    RESOLVED = "resolved"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class TicketSource(str, Enum):
    """Known source platforms. Use CUSTOM for anything else."""

    SERVICENOW = "servicenow"
    JIRA = "jira"
    PAGERDUTY = "pagerduty"
    CLOUDWATCH = "cloudwatch"
    GITHUB = "github"
    CUSTOM = "custom"


@dataclass
class Ticket:
    """Canonical ticket representation in the Libre Ticket Suite.

    All source adapters produce Ticket instances. All triage and routing
    components consume Ticket instances.

    Attributes:
        id: Globally unique identifier (auto-generated UUID4 if not provided).
        title: Short human-readable summary of the issue.
        description: Full details of the ticket or alert.
        source: Which platform this ticket originated from.
        source_id: The original ID in the source platform.
        priority: Normalized priority level.
        status: Normalized lifecycle state.
        tags: Free-form labels for categorization.
        metadata: Arbitrary extra fields from the source (not normalized).
        created_at: When the ticket was created in the source system.
        synced_at: When this Ticket object was created by TicketSync.
    """

    title: str
    description: str = ""
    source: TicketSource = TicketSource.CUSTOM
    source_id: Optional[str] = None
    priority: TicketPriority = TicketPriority.UNKNOWN
    status: TicketStatus = TicketStatus.UNKNOWN
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    synced_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dictionary.

        Returns:
            A JSON-serializable dict representation.

        >>> t = Ticket(title="disk full")
        >>> d = t.to_dict()
        >>> d["title"]
        'disk full'
        >>> d["source"]
        'custom'
        """
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "source": self.source.value,
            "source_id": self.source_id,
            "priority": self.priority.value,
            "status": self.status.value,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "synced_at": self.synced_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Ticket":
        """Deserialize from a plain dictionary.

        Args:
            data: Dict previously produced by ``to_dict()``.

        Returns:
            A Ticket instance.

        >>> t = Ticket(title="test")
        >>> t2 = Ticket.from_dict(t.to_dict())
        >>> t2.title
        'test'
        """
        created_raw = data.get("created_at")
        created_at: Optional[datetime] = (
            datetime.fromisoformat(created_raw) if created_raw else None
        )
        synced_raw = data.get("synced_at", datetime.now(timezone.utc).isoformat())
        synced_at = datetime.fromisoformat(synced_raw)

        return cls(
            id=data.get("id", str(uuid.uuid4())),
            title=data["title"],
            description=data.get("description", ""),
            source=TicketSource(data.get("source", TicketSource.CUSTOM.value)),
            source_id=data.get("source_id"),
            priority=TicketPriority(
                data.get("priority", TicketPriority.UNKNOWN.value)
            ),
            status=TicketStatus(
                data.get("status", TicketStatus.UNKNOWN.value)
            ),
            tags=list(data.get("tags", [])),
            metadata=dict(data.get("metadata", {})),
            created_at=created_at,
            synced_at=synced_at,
        )
