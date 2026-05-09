"""TriageMetadata model and optional adapter extension protocol.

This module defines:

1. ``TriageMetadata`` — a Pydantic model capturing triage decisions
   (assignee, severity override, priority, notes, resolution).

2. ``MetadataAdapter`` — an optional Protocol that adapters may implement
   to persist triage metadata using the system's native hidden fields.

Design rules
------------

- **System-native metadata is primary**.  OpsCenter stores metadata in
  ``OperationalData`` (invisible to humans in the OpsCenter UI).  GitHub
  stores it via the label system and issue-level metadata fields.  These
  channels are machine-readable and do not pollute the visible comment thread.

- **Structured comments are the fallback** for systems that have no native
  hidden metadata.  If an adapter uses structured comments, the format must
  be human-readable (not raw JSON) so that anyone reading the issue in a
  browser can understand it.

- **Never pollute the main description or visible comment thread** with raw
  machine data when a hidden field exists.

Adapter support matrix
----------------------

+-----------------------+-----------------+-------------------------+
| Adapter               | write_metadata  | Notes                   |
+=======================+=================+=========================+
| OpsCenterAdapter      | OperationalData | keys: /ticketsync/triage/* |
| GitHubIssuesAdapter   | Labels + body   | structured comment      |
| LocalFilesystemAdapter| JSON file       | sidecar .meta.json file |
| CloudWatchAlarms      | not supported   | read-only               |
| GuardDutyFindings     | not supported   | read-only               |
| SecurityHubFindings   | not supported   | read-only               |
+-----------------------+-----------------+-------------------------+

Usage
-----

::

    from ticketsync.metadata import TriageMetadata, read_metadata, write_metadata
    from ticketsync.adapters.github_issues import GitHubIssuesAdapter

    adapter = GitHubIssuesAdapter(client=client, owner="org", repo="repo")

    meta = TriageMetadata(
        assignee="alice@example.com",
        priority=1,
        triage_notes="Confirmed malicious — escalate to IR team.",
        resolution="Blocked the offending IP at perimeter.",
    )

    write_metadata(adapter, source_id="42", metadata=meta)
    recovered = read_metadata(adapter, source_id="42")

    assert recovered is not None
    assert recovered.assignee == "alice@example.com"
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# TriageMetadata model
# ---------------------------------------------------------------------------


class TriageMetadata(BaseModel):
    """Triage decisions attached to a ticket.

    All fields are optional so partial updates are supported.
    """

    # Who is handling it
    assignee: str = Field(
        "",
        description="Username or email of the person responsible for triage",
    )

    # Priority (1 = highest, 5 = lowest; None = not prioritised)
    priority: int | None = Field(
        None,
        ge=1,
        le=5,
        description="Priority level 1–5 (1=highest); None means not set",
    )

    # Triage notes (human-readable; stored in native hidden field if available)
    triage_notes: str = Field(
        "",
        description="Free-text notes from the analyst performing triage",
    )

    # Severity override (leave empty to accept the original)
    severity_override: str = Field(
        "",
        description=(
            "Analyst-confirmed severity — overrides the auto-detected severity. "
            "One of: critical, high, medium, low, informational"
        ),
    )

    # Resolution notes (filled when the ticket is closed/resolved)
    resolution: str = Field(
        "",
        description="Free-text description of how the issue was resolved",
    )

    # Timestamps
    triaged_at: datetime | None = Field(
        None,
        description="When triage was performed (auto-set on first write if None)",
    )

    def with_timestamp(self) -> "TriageMetadata":
        """Return a copy with ``triaged_at`` set to now if currently None."""
        if self.triaged_at is not None:
            return self
        return self.model_copy(update={"triaged_at": datetime.now(timezone.utc)})

    def to_human_readable(self) -> str:
        """Render as a short human-readable block (used for comment fallback).

        The output is intended to be understandable by a developer who sees
        it in a GitHub comment, without any prior knowledge of TicketSync.
        """
        lines: list[str] = ["<!-- TicketSync triage metadata (do not edit) -->"]
        if self.assignee:
            lines.append(f"Assignee: {self.assignee}")
        if self.priority is not None:
            lines.append(f"Priority: {self.priority}")
        if self.severity_override:
            lines.append(f"Severity override: {self.severity_override}")
        if self.triage_notes:
            lines.append(f"Triage notes: {self.triage_notes}")
        if self.resolution:
            lines.append(f"Resolution: {self.resolution}")
        if self.triaged_at:
            lines.append(f"Triaged at: {self.triaged_at.isoformat()}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Optional adapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MetadataAdapter(Protocol):
    """Optional extension protocol for adapters that support triage metadata.

    Adapters that cannot persist hidden metadata (read-only adapters,
    systems with no native metadata storage) simply do not implement this
    protocol.  Use ``isinstance(adapter, MetadataAdapter)`` to check.
    """

    def write_metadata(self, source_id: str, metadata: TriageMetadata) -> None:
        """Persist triage metadata for the given ticket.

        Parameters
        ----------
        source_id:
            The vendor-native identifier of the ticket (e.g. GitHub issue
            number, OpsItem ID).
        metadata:
            The triage decisions to persist.
        """
        ...

    def read_metadata(self, source_id: str) -> TriageMetadata | None:
        """Retrieve triage metadata for the given ticket.

        Returns ``None`` if no metadata has been written.

        Parameters
        ----------
        source_id:
            The vendor-native identifier of the ticket.
        """
        ...


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def write_metadata(
    adapter: Any,
    source_id: str,
    metadata: TriageMetadata,
) -> bool:
    """Write triage metadata if the adapter supports it.

    Returns True if metadata was written, False if the adapter does not
    implement ``MetadataAdapter``.
    """
    if isinstance(adapter, MetadataAdapter):
        adapter.write_metadata(source_id, metadata)
        return True
    return False


def read_metadata(
    adapter: Any,
    source_id: str,
) -> TriageMetadata | None:
    """Read triage metadata if the adapter supports it.

    Returns the ``TriageMetadata`` object, or ``None`` if the adapter
    does not support it or no metadata has been stored.
    """
    if isinstance(adapter, MetadataAdapter):
        return adapter.read_metadata(source_id)
    return None
