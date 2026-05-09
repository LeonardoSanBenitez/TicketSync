"""AWS Security Hub Findings adapter.

Maps between Security Hub finding payloads in ASFF (Amazon Security
Finding Format) and the TicketSync IR.  All real boto3 calls are
mediated through an injected client object so the adapter can be fully
tested without AWS credentials.

The client must expose:
    client.get_findings(**kwargs) -> dict

ASFF field mapping
------------------

ASFF concept                    -> Ticket IR field
------------------------------  ------------------------------------
Id                               source_id
Title                            title
Description.Text                 description
Severity.Label (CRITICAL/HIGH/MEDIUM/LOW/INFORMATIONAL)
                                 severity (direct mapping)
Workflow.Status (NEW/NOTIFIED/SUPPRESSED/RESOLVED)
                                 status (see below)
Types[0]                         category
CreatedAt (ISO-8601 string)      created_at
UpdatedAt (ISO-8601 string)      updated_at
ProductArn                       external_url
AwsAccountId                     entities (AccountEntity)
Region                           tags (added as "region:<value>")
ProductName                      tags (added as "product:<value>")
CompanyName                      tags (added as "company:<value>")

Severity mapping (direct from ASFF Severity.Label):
  CRITICAL     -> "critical"
  HIGH         -> "high"
  MEDIUM       -> "medium"
  LOW          -> "low"
  INFORMATIONAL -> "informational"

Status mapping:
  Workflow.Status = NEW        -> "open"
  Workflow.Status = NOTIFIED   -> "open"
  Workflow.Status = SUPPRESSED -> "closed"
  Workflow.Status = RESOLVED   -> "resolved"
  (absent or unknown)          -> "open"

The adapter is read-only: ``write`` raises ``NotImplementedError`` because
Security Hub findings are managed by the originating product and should be
updated via ``batch_update_findings`` directly, not via TicketSync writes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ticketsync.models import AccountEntity, Entity, SeverityLevel, Ticket, TicketStatus

# ---------------------------------------------------------------------------
# Severity / status maps
# ---------------------------------------------------------------------------

_SEVERITY_MAP: dict[str, SeverityLevel] = {
    "CRITICAL": "critical",
    "HIGH": "high",
    "MEDIUM": "medium",
    "LOW": "low",
    "INFORMATIONAL": "informational",
}

_STATUS_MAP: dict[str, TicketStatus] = {
    "NEW": "open",
    "NOTIFIED": "open",
    "SUPPRESSED": "closed",
    "RESOLVED": "resolved",
}

_DEFAULT_PAGE_SIZE: int = 100  # Security Hub max per page


def _parse_utc(ts: str | None) -> datetime:
    """Parse an ISO-8601 string to a timezone-aware UTC datetime."""
    if not ts:
        return datetime.now(timezone.utc)
    normalised = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(normalised)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _map_severity(finding: dict[str, Any]) -> SeverityLevel:
    """Extract and map Severity.Label from an ASFF finding."""
    severity_block: dict[str, Any] = finding.get("Severity", {}) or {}
    label: str = str(severity_block.get("Label", "INFORMATIONAL")).upper()
    return _SEVERITY_MAP.get(label, "informational")


def _map_status(finding: dict[str, Any]) -> TicketStatus:
    """Extract and map Workflow.Status from an ASFF finding."""
    workflow: dict[str, Any] = finding.get("Workflow", {}) or {}
    ws: str = str(workflow.get("Status", "NEW")).upper()
    return _STATUS_MAP.get(ws, "open")


def _build_tags(finding: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    region: str = str(finding.get("Region", ""))
    if region:
        tags.append(f"region:{region}")
    product: str = str(finding.get("ProductName", ""))
    if product:
        tags.append(f"product:{product}")
    company: str = str(finding.get("CompanyName", ""))
    if company:
        tags.append(f"company:{company}")
    return tags


def _build_entities(finding: dict[str, Any]) -> list[Entity]:
    entities: list[Entity] = []
    account_id: str = str(finding.get("AwsAccountId", ""))
    if account_id:
        entities.append(
            AccountEntity(
                uid=account_id,
                name=account_id,
                type="aws",
            )
        )
    return entities


def _extract_description(finding: dict[str, Any]) -> str:
    """Extract description text from ASFF Description block or fall back to Title."""
    desc_block = finding.get("Description")
    if isinstance(desc_block, dict):
        return str(desc_block.get("Text", "") or "")
    if isinstance(desc_block, str):
        return desc_block
    return ""


def _extract_category(finding: dict[str, Any]) -> str:
    """Use the first entry in Types[] as the category, if present."""
    types: list[Any] = finding.get("Types", []) or []
    if types:
        return str(types[0])
    return ""


class SecurityHubFindingsAdapter:
    """Adapter for AWS Security Hub Findings (ASFF format).

    Parameters
    ----------
    client:
        A boto3 Security Hub client (or compatible stub) with a
        ``get_findings`` method.
    region:
        AWS region name.  Stored for reference only.
    system_name:
        The ``source_system`` label embedded in produced tickets.
    filters:
        Optional ASFF filters dict passed to ``get_findings``.  Use this
        to narrow results by product, severity, workflow status, etc.
        If ``None``, all findings visible to the account are returned.
    """

    system_name: str

    def __init__(
        self,
        client: Any,
        region: str = "us-east-1",
        system_name: str = "securityhub",
        filters: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._region = region
        self.system_name = system_name
        self._filters: dict[str, Any] = dict(filters) if filters else {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_all_raw(
        self,
        extra_filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Page through get_findings and collect all results."""
        findings: list[dict[str, Any]] = []
        merged: dict[str, Any] = dict(self._filters)
        if extra_filters:
            # Merge lists for each filter key — ASFF filters are lists
            for k, v in extra_filters.items():
                if k in merged and isinstance(merged[k], list):
                    merged[k] = list(merged[k]) + list(v)
                else:
                    merged[k] = v

        kwargs: dict[str, Any] = {"MaxResults": _DEFAULT_PAGE_SIZE}
        if merged:
            kwargs["Filters"] = merged

        while True:
            response: dict[str, Any] = self._client.get_findings(**kwargs)
            findings.extend(response.get("Findings", []))
            next_token: str = response.get("NextToken", "")
            if not next_token:
                break
            kwargs["NextToken"] = next_token

        return findings

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def to_ticket(self, raw: dict[str, object]) -> Ticket:
        """Map an ASFF finding dict (from get_findings) to a Ticket IR."""
        finding: dict[str, Any] = dict(raw)

        severity = _map_severity(finding)
        status = _map_status(finding)
        tags = _build_tags(finding)
        entities = _build_entities(finding)
        description = _extract_description(finding)
        category = _extract_category(finding)

        # Fallback title: Security Hub sometimes returns an empty Title
        title: str = str(finding.get("Title", "") or "Security Hub Finding").strip()
        if not title:
            title = "Security Hub Finding"

        return Ticket.model_validate({
            "source_system": self.system_name,
            "source_id": str(finding.get("Id", "")),
            "title": title,
            "description": description,
            "severity": severity,
            "status": status,
            "category": category,
            "tags": tags,
            "created_at": _parse_utc(finding.get("CreatedAt")),
            "updated_at": _parse_utc(finding.get("UpdatedAt")),
            "external_url": str(finding.get("ProductArn", "")),
            "entities": [e.model_dump() for e in entities],
            "raw": finding,
        })

    def from_ticket(self, ticket: Ticket) -> dict[str, object]:
        """Map a Ticket IR back to ASFF-compatible fields.

        Security Hub findings are immutable via ``get_findings``; use
        ``batch_update_findings`` directly to change workflow status or
        note text.  This method returns the minimal dict that identifies
        the finding for audit purposes.
        """
        return {
            "FindingId": ticket.source_id,
            "Title": ticket.title,
            "Description": ticket.description,
        }

    def fetch_new(self, since: datetime | None = None) -> list[dict[str, object]]:
        """Fetch Security Hub findings, optionally filtered by update time.

        Parameters
        ----------
        since:
            If provided, only findings with ``UpdatedAt`` strictly after
            this timestamp are returned.  The filter is applied via ASFF
            ``DateRange`` / ``Start`` criteria.
        """
        extra: dict[str, Any] | None = None
        if since is not None:
            since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            # ASFF date filter format: ISO-8601 with Z suffix
            since_str = since_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            extra = {
                "UpdatedAt": [
                    {"Start": since_str, "End": "9999-12-31T23:59:59.999Z"}
                ]
            }

        findings = self._fetch_all_raw(extra_filters=extra)
        return list(findings)

    def write(self, ticket: Ticket) -> str:
        """Not supported — Security Hub findings are managed by AWS services.

        Use ``batch_update_findings`` to change workflow status or add notes.

        Raises
        ------
        NotImplementedError
            Always.
        """
        raise NotImplementedError(
            "SecurityHubFindingsAdapter does not support write operations. "
            "Security Hub findings are managed by originating AWS services; "
            "use batch_update_findings to change workflow state or add notes."
        )
