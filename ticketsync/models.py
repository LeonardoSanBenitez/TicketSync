"""TicketSync domain models.

The ``Ticket`` class is the intermediate representation (IR) that flows through
every adapter pair.  All external system payloads are mapped *into* a Ticket on
ingestion and *out of* a Ticket on egress.

Entity types loosely follow the OCSF vocabulary (Account, Host, Process,
IpAddress, File, Url) but are not strict OCSF subclasses — we diverge wherever
the ticket-centric lifecycle fields matter more than event-centric semantics.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Entity types (OCSF-inspired)
# ---------------------------------------------------------------------------


class AccountEntity(BaseModel):
    """An IAM account, cloud account, or OS user account."""

    kind: Literal["account"] = "account"
    uid: str = Field(..., description="Unique identifier (account ID, ARN, etc.)")
    name: str = Field("", description="Human-readable account or user name")
    type: str = Field("", description="E.g. 'aws', 'gcp', 'ad', 'local'")


class HostEntity(BaseModel):
    """A host, VM, or container."""

    kind: Literal["host"] = "host"
    hostname: str = Field(..., description="FQDN or short hostname")
    ip: str = Field("", description="Primary IP address (best-effort)")
    os: str = Field("", description="OS family, e.g. 'linux', 'windows'")


class ProcessEntity(BaseModel):
    """A running OS process."""

    kind: Literal["process"] = "process"
    pid: int = Field(..., description="Process ID")
    name: str = Field("", description="Process name")
    cmd_line: str = Field("", description="Full command line, if available")


class IpAddressEntity(BaseModel):
    """A bare IP address that is not attached to a specific host."""

    kind: Literal["ip_address"] = "ip_address"
    ip: str = Field(..., description="IPv4 or IPv6 address")
    version: Literal["v4", "v6"] = "v4"


class FileEntity(BaseModel):
    """A filesystem path or object."""

    kind: Literal["file"] = "file"
    path: str = Field(..., description="Full filesystem path")
    hash_sha256: str = Field("", description="SHA-256 digest, if known")


class UrlEntity(BaseModel):
    """A URL observed in the event."""

    kind: Literal["url"] = "url"
    url: str = Field(..., description="Full URL")
    domain: str = Field("", description="Extracted domain, if pre-parsed")


# Discriminated union — callers use ``Entity`` as the field type.
Entity = Annotated[
    Union[
        AccountEntity,
        HostEntity,
        ProcessEntity,
        IpAddressEntity,
        FileEntity,
        UrlEntity,
    ],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Severity / Status enumerations
# ---------------------------------------------------------------------------

SeverityLevel = Literal["critical", "high", "medium", "low", "informational"]
TicketStatus = Literal["open", "in_progress", "resolved", "closed"]


# ---------------------------------------------------------------------------
# Ticket — the intermediate representation
# ---------------------------------------------------------------------------


class Ticket(BaseModel):
    """Canonical intermediate representation of a ticket / alert / issue.

    Required fields are the minimum that every adapter must populate.
    Optional fields are populated on a best-effort basis depending on what the
    source system exposes.

    The ``raw`` field preserves the full vendor payload so callers can access
    fields that the IR does not model.
    """

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="TicketSync-owned UUID, stable across syncs",
    )
    source_system: str = Field(
        ..., description="Short name of the system this ticket came from"
    )
    source_id: str = Field(
        ..., description="Vendor-native identifier, preserved verbatim"
    )

    # ------------------------------------------------------------------
    # Core content
    # ------------------------------------------------------------------
    title: str = Field(..., min_length=1, description="One-line summary")
    description: str = Field("", description="Longer description / body")

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------
    severity: SeverityLevel = Field(..., description="Severity level")
    status: TicketStatus = Field("open", description="Current lifecycle status")
    category: str = Field(
        "", description="Free-text category, e.g. 'Impossible Travel'"
    )
    tags: list[str] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # Timestamps (always UTC)
    # ------------------------------------------------------------------
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the ticket was created in the source system",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the ticket was last modified in the source system",
    )

    # ------------------------------------------------------------------
    # Optional enrichment
    # ------------------------------------------------------------------
    entities: list[Entity] = Field(
        default_factory=list,
        description="OCSF-inspired entity references found in the alert",
    )
    remediation_steps: str = Field(
        "", description="Free-text remediation guidance"
    )
    external_url: str = Field(
        "", description="Deep-link back into the source system"
    )
    assignee: str = Field("", description="Username or email of assignee")

    # ------------------------------------------------------------------
    # Raw vendor payload
    # ------------------------------------------------------------------
    raw: dict[str, object] = Field(
        default_factory=dict,
        description="Full vendor payload — opaque to TicketSync",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def ensure_utc(cls, v: object) -> datetime:
        """Coerce naive datetimes to UTC; reject non-datetime values."""
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v
        if isinstance(v, str):
            # datetime.fromisoformat() does not accept the 'Z' UTC suffix on
            # Python 3.10.  Replace it with '+00:00' for cross-version compat.
            normalised = v.replace("Z", "+00:00") if v.endswith("Z") else v
            dt = datetime.fromisoformat(normalised)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        raise ValueError(f"Cannot parse datetime from {type(v)}: {v!r}")

    @field_validator("tags", mode="before")
    @classmethod
    def deduplicate_tags(cls, v: object) -> list[str]:
        """Remove duplicate tags while preserving order."""
        if not isinstance(v, list):
            raise ValueError("tags must be a list")
        seen: set[str] = set()
        result: list[str] = []
        for tag in v:
            if not isinstance(tag, str):
                raise ValueError(f"All tags must be strings, got {type(tag)}")
            if tag not in seen:
                seen.add(tag)
                result.append(tag)
        return result

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, v: object) -> str:
        if not isinstance(v, str):
            raise ValueError("title must be a string")
        stripped = v.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def is_open(self) -> bool:
        """Return True if the ticket has not been resolved or closed."""
        return self.status in ("open", "in_progress")

    def with_status(self, status: TicketStatus) -> "Ticket":
        """Return a new Ticket with the given status (immutable update)."""
        return self.model_copy(update={"status": status})

    def with_assignee(self, assignee: str) -> "Ticket":
        """Return a new Ticket with a new assignee."""
        return self.model_copy(update={"assignee": assignee})
