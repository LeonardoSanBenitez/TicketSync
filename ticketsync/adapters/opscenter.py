"""AWS Systems Manager OpsCenter adapter (stub implementation).

This adapter maps between OpsCenter OpsItems and the TicketSync IR.  In v0.2.0
it is a *stub*: all real boto3 calls are mediated through an injected client
object so the adapter can be fully tested without AWS credentials.

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

Callers inject a boto3-like client via the constructor.  Production code passes
``boto3.client("ssm", region_name=...)``.  Tests pass a mock / stub object that
implements the methods this adapter calls: ``get_ops_item``,
``describe_ops_items``, ``create_ops_item``, ``update_ops_item``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ticketsync.models import SeverityLevel, Ticket, TicketStatus


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

        return Ticket.model_validate({
            "source_system": self.system_name,
            "source_id": str(ops_item.get("OpsItemId", "")),
            "title": str(ops_item.get("Title", "")),
            "description": str(ops_item.get("Description", "")),
            "severity": severity,
            "status": status,
            "tags": tags,
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

        return {
            "Title": ticket.title,
            "Description": ticket.description or " ",  # OpsCenter rejects empty
            "Severity": severity_num,
            "Status": status_str,
            "Source": source,
        }

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
            self._client.update_ops_item(
                OpsItemId=ticket.source_id,
                Title=str(payload["Title"]),
                Description=str(payload["Description"]),
                Severity=str(payload["Severity"]),
                Status=str(payload["Status"]),
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
            )
            return str(response.get("OpsItemId", ""))
