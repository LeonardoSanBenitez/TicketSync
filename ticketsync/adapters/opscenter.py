"""AWS Systems Manager OpsCenter adapter.

This adapter maps between OpsCenter OpsItems and the TicketSync IR.  All
real boto3 calls are mediated through an injected client object so the
adapter can be fully tested without AWS credentials.

OpsCenter field mapping
-----------------------

OpsCenter concept         -> Ticket IR field
------------------------  -----------------------------------
OpsItemId                 source_id
Title                     title
Description               description
Severity (1-4)            severity (mapped via _SEVERITY_MAP)
Status                    status (mapped via _STATUS_MAP)
Source                    tags (prepended as "source:<value>")
CreatedTime               created_at
LastModifiedTime          updated_at
OperationalData           raw (verbatim)

Severity mapping (OpsCenter uses numeric 1-4, we normalise to words):

  "1" -> "critical"
  "2" -> "high"
  "3" -> "medium"
  "4" -> "low"
  anything else -> "informational"

Assignee
--------

OpsCenter does not have a first-class "assignee" field.  The adapter
stores ``ticket.assignee`` in ``OperationalData`` under the key
``/ticketsync/assignee`` on write, and reads it back from there on
``to_ticket``.  This keeps human-readable comment views clean while
preserving the data for round-trips.

If ``ticket.assignee`` is empty the key is omitted; existing assignee
data in OperationalData is left untouched on update.

Callers inject a boto3-like client via the constructor.  Production code
passes ``boto3.client("ssm", region_name=...)``.  Tests pass a mock / stub
object that implements the methods this adapter calls: ``get_ops_item``,
``describe_ops_items``, ``create_ops_item``, ``update_ops_item``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ticketsync.models import SeverityLevel, Ticket, TicketStatus
from ticketsync.metadata import TriageMetadata


_SEVERITY_MAP: dict[str, SeverityLevel] = {
    "1": "critical",
    "2": "high",
    "3": "medium",
    "4": "low",
}

_STATUS_MAP: dict[str, TicketStatus] = {
    "Open": "open",
    "InProgress": "in_progress",
    "Resolved": "resolved",
}

_REVERSE_SEVERITY: dict[SeverityLevel, str] = {v: k for k, v in _SEVERITY_MAP.items()}
_REVERSE_SEVERITY["informational"] = "4"  # best-effort fallback

_REVERSE_STATUS: dict[TicketStatus, str] = {v: k for k, v in _STATUS_MAP.items()}
_REVERSE_STATUS["closed"] = "Resolved"  # OpsCenter has no "closed" state

# OperationalData key used to store TicketSync metadata.
_ASSIGNEE_KEY = "/ticketsync/assignee"
_SOURCE_SYSTEM_KEY = "/ticketsync/source_system"
_SOURCE_ID_KEY = "/ticketsync/source_id"
_SYNCED_KEY = "/ticketsync/synced"

# Triage metadata OperationalData keys
_TRIAGE_ASSIGNEE_KEY = "/ticketsync/triage/assignee"
_TRIAGE_PRIORITY_KEY = "/ticketsync/triage/priority"
_TRIAGE_NOTES_KEY = "/ticketsync/triage/notes"
_TRIAGE_SEVERITY_OVERRIDE_KEY = "/ticketsync/triage/severity_override"
_TRIAGE_RESOLUTION_KEY = "/ticketsync/triage/resolution"
_TRIAGE_AT_KEY = "/ticketsync/triage/triaged_at"


def _ops_data_value(value: str) -> dict[str, str]:
    """Wrap a string as an OpsCenter OperationalData value dict."""
    return {"Value": value, "Type": "SearchableString"}


class OpsCenterAdapter:
    """Adapter for AWS Systems Manager OpsCenter.

    Parameters
    ----------
    client:
        A boto3 SSM client (or compatible stub).
    region:
        AWS region name.  Stored for reference; the client itself determines
        the actual endpoint.
    system_name:
        The ``source_system`` label embedded in tickets produced here.
    """

    system_name: str

    def __init__(
        self,
        client: Any,
        region: str = "us-east-1",
        system_name: str = "opscenter",
    ) -> None:
        self._client = client
        self._region = region
        self.system_name = system_name

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _read_operational_data(
        ops_item: dict[str, Any], key: str
    ) -> str:
        """Extract a string value from OpsItem OperationalData by key."""
        od: dict[str, Any] = ops_item.get("OperationalData", {}) or {}
        entry = od.get(key, {}) or {}
        return str(entry.get("Value", ""))

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def to_ticket(self, raw: dict[str, object]) -> Ticket:
        """Map an OpsItem dict (as returned by boto3) to a Ticket IR."""
        ops_item: dict[str, Any] = dict(raw)

        severity_str = str(ops_item.get("Severity", "4"))
        severity: SeverityLevel = _SEVERITY_MAP.get(severity_str, "informational")

        status_str = str(ops_item.get("Status", "Open"))
        status: TicketStatus = _STATUS_MAP.get(status_str, "open")

        # Source field → tag (e.g. "source:aws/cloudwatch")
        source_tag = ops_item.get("Source", "")
        tags: list[str] = []
        if source_tag:
            tags.append(f"source:{source_tag}")

        created_raw = ops_item.get("CreatedTime", datetime.now(timezone.utc))
        updated_raw = ops_item.get("LastModifiedTime", datetime.now(timezone.utc))

        created_at = (
            self._ensure_utc(created_raw)
            if isinstance(created_raw, datetime)
            else datetime.now(timezone.utc)
        )
        updated_at = (
            self._ensure_utc(updated_raw)
            if isinstance(updated_raw, datetime)
            else datetime.now(timezone.utc)
        )

        # Assignee stored in OperationalData (hidden from human UI)
        assignee = self._read_operational_data(ops_item, _ASSIGNEE_KEY)

        return Ticket.model_validate({
            "source_system": self.system_name,
            "source_id": str(ops_item.get("OpsItemId", "")),
            "title": str(ops_item.get("Title", "")),
            "description": str(ops_item.get("Description", "")),
            "severity": severity,
            "status": status,
            "tags": tags,
            "assignee": assignee,
            "created_at": created_at,
            "updated_at": updated_at,
            "raw": ops_item,
        })

    def from_ticket(self, ticket: Ticket) -> dict[str, object]:
        """Map a Ticket IR to an OpsItem payload dict."""
        severity_num = _REVERSE_SEVERITY.get(ticket.severity, "4")
        status_str = _REVERSE_STATUS.get(ticket.status, "Open")

        source_tags = [t for t in ticket.tags if t.startswith("source:")]
        source = source_tags[0].split(":", 1)[1] if source_tags else "ticketsync"

        payload: dict[str, object] = {
            "Title": ticket.title,
            "Description": ticket.description or " ",  # OpsCenter rejects empty
            "Severity": severity_num,
            "Status": status_str,
            "Source": source,
        }

        # Store TicketSync metadata in OperationalData (machine-readable, hidden).
        operational_data: dict[str, dict[str, str]] = {
            _SOURCE_SYSTEM_KEY: _ops_data_value(ticket.source_system),
            _SOURCE_ID_KEY: _ops_data_value(ticket.source_id),
        }
        if ticket.assignee:
            operational_data[_ASSIGNEE_KEY] = _ops_data_value(ticket.assignee)

        payload["OperationalData"] = operational_data
        return payload

    def fetch_new(self, since: datetime | None = None) -> list[dict[str, object]]:
        """List OpsItems, optionally filtered by LastModifiedTime.

        Note: OpsCenter ``describe_ops_items`` does not natively support a
        time filter; we filter client-side after fetching.
        """
        response: dict[str, Any] = self._client.describe_ops_items(
            OpsItemFilters=[]
        )
        items: list[dict[str, Any]] = response.get("OpsItemSummaries", [])

        if since is not None:
            since_utc = self._ensure_utc(since) if since.tzinfo is None else since
            filtered: list[dict[str, object]] = []
            for item in items:
                modified = item.get("LastModifiedTime")
                if isinstance(modified, datetime):
                    modified_utc = self._ensure_utc(modified)
                    if modified_utc > since_utc:
                        filtered.append(item)
            return filtered

        return list(items)

    def write(self, ticket: Ticket) -> str:
        """Create or update an OpsItem.

        If ``ticket.source_id`` is non-empty and looks like an OpsItem ID
        (starts with ``oi-``), we attempt an update.  Otherwise we create.
        """
        payload = self.from_ticket(ticket)

        if ticket.source_id and ticket.source_id.startswith("oi-"):
            # Update existing
            od = payload.get("OperationalData", {})
            self._client.update_ops_item(
                OpsItemId=ticket.source_id,
                Title=str(payload["Title"]),
                Description=str(payload["Description"]),
                Severity=str(payload["Severity"]),
                Status=str(payload["Status"]),
                OperationalData=od,
            )
            return ticket.source_id
        else:
            # Create new
            response: dict[str, Any] = self._client.create_ops_item(
                Title=str(payload["Title"]),
                Description=str(payload["Description"]),
                Severity=str(payload["Severity"]),
                Source=str(payload["Source"]),
                OpsItemType="/aws/ssm/opsitems",
                OperationalData=payload.get("OperationalData", {}),
            )
            return str(response.get("OpsItemId", ""))

    # ------------------------------------------------------------------
    # Tag-based sync write-back
    # ------------------------------------------------------------------

    def mark_synced(self, source_id: str) -> None:
        """Mark an OpsItem as synced via OperationalData.

        Called by the engine after a successful write to the destination
        when ``dedup_strategy: tag-based`` is configured.  Adds a
        ``/ticketsync/synced`` key to OperationalData; invisible in normal
        OpsCenter UI views but readable programmatically.

        Parameters
        ----------
        source_id:
            OpsItem ID (e.g. ``oi-abc123``).
        """
        self._client.update_ops_item(
            OpsItemId=source_id,
            OperationalData={
                _SYNCED_KEY: _ops_data_value("true"),
            },
        )

    # ------------------------------------------------------------------
    # Triage metadata (MetadataAdapter optional extension)
    # ------------------------------------------------------------------

    def write_metadata(self, source_id: str, metadata: TriageMetadata) -> None:
        """Persist triage metadata in OpsItem OperationalData.

        OpsCenter OperationalData is machine-readable and invisible in the
        normal OpsCenter UI view, making it ideal for hidden metadata.

        All values are stored as SearchableString so they can be queried
        via ``describe_ops_items`` filters.

        Parameters
        ----------
        source_id:
            OpsItem ID (e.g. ``oi-abc123``).
        metadata:
            Triage decisions to persist.
        """
        od: dict[str, dict[str, str]] = {}

        if metadata.assignee:
            od[_TRIAGE_ASSIGNEE_KEY] = _ops_data_value(metadata.assignee)
        if metadata.priority is not None:
            od[_TRIAGE_PRIORITY_KEY] = _ops_data_value(str(metadata.priority))
        if metadata.triage_notes:
            od[_TRIAGE_NOTES_KEY] = _ops_data_value(metadata.triage_notes)
        if metadata.severity_override:
            od[_TRIAGE_SEVERITY_OVERRIDE_KEY] = _ops_data_value(metadata.severity_override)
        if metadata.resolution:
            od[_TRIAGE_RESOLUTION_KEY] = _ops_data_value(metadata.resolution)

        # Only stamp triaged_at if there is at least one substantive field;
        # we do not want to update an OpsItem purely to record that we checked it.
        if od:
            stamp = metadata.with_timestamp()
            if stamp.triaged_at is not None:
                od[_TRIAGE_AT_KEY] = _ops_data_value(stamp.triaged_at.isoformat())
            self._client.update_ops_item(
                OpsItemId=source_id,
                OperationalData=od,
            )

    def read_metadata(self, source_id: str) -> TriageMetadata | None:
        """Read triage metadata from OpsItem OperationalData.

        Returns a ``TriageMetadata`` if any triage keys are found, or
        ``None`` if the OpsItem has never had metadata written to it.

        Parameters
        ----------
        source_id:
            OpsItem ID (e.g. ``oi-abc123``).
        """
        response: dict[str, Any] = self._client.get_ops_item(OpsItemId=source_id)
        ops_item: dict[str, Any] = response.get("OpsItem", {}) or {}

        assignee = self._read_operational_data(ops_item, _TRIAGE_ASSIGNEE_KEY)
        priority_str = self._read_operational_data(ops_item, _TRIAGE_PRIORITY_KEY)
        notes = self._read_operational_data(ops_item, _TRIAGE_NOTES_KEY)
        severity_override = self._read_operational_data(
            ops_item, _TRIAGE_SEVERITY_OVERRIDE_KEY
        )
        resolution = self._read_operational_data(ops_item, _TRIAGE_RESOLUTION_KEY)
        triaged_at_str = self._read_operational_data(ops_item, _TRIAGE_AT_KEY)

        # If none of the triage keys are populated, return None
        if not any([assignee, priority_str, notes, severity_override, resolution,
                    triaged_at_str]):
            return None

        priority: int | None = None
        if priority_str:
            try:
                priority = int(priority_str)
            except ValueError:
                pass

        triaged_at: datetime | None = None
        if triaged_at_str:
            try:
                normalised = (
                    triaged_at_str.replace("Z", "+00:00")
                    if triaged_at_str.endswith("Z")
                    else triaged_at_str
                )
                dt = datetime.fromisoformat(normalised)
                triaged_at = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        return TriageMetadata(
            assignee=assignee,
            priority=priority,
            triage_notes=notes,
            severity_override=severity_override,
            resolution=resolution,
            triaged_at=triaged_at,
        )

    # ------------------------------------------------------------------
    # Destination-check dedup
    # ------------------------------------------------------------------

    def find_by_source_coordinates(
        self, source_system: str, source_id: str
    ) -> str | None:
        """Return the OpsItem ID for a ticket from another system.

        Searches OperationalData for matching ``/ticketsync/source_system``
        and ``/ticketsync/source_id`` keys.  Returns the OpsItem ID, or
        ``None`` if not found.

        Note: ``describe_ops_items`` with OperationalData filters requires
        the keys to be indexed (SearchableString type).  This adapter writes
        them with SearchableString on create, so the filter works.
        """
        response: dict[str, Any] = self._client.describe_ops_items(
            OpsItemFilters=[
                {
                    "Key": "OperationalData",
                    "Values": [
                        f'{{"key":"{_SOURCE_SYSTEM_KEY}",'
                        f'"value":"{source_system}"}}'
                    ],
                    "Operator": "Equal",
                },
                {
                    "Key": "OperationalData",
                    "Values": [
                        f'{{"key":"{_SOURCE_ID_KEY}",'
                        f'"value":"{source_id}"}}'
                    ],
                    "Operator": "Equal",
                },
            ]
        )
        summaries: list[dict[str, Any]] = response.get("OpsItemSummaries", [])
        if summaries:
            return str(summaries[0].get("OpsItemId", ""))
        return None
