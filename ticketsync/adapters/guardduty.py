"""AWS GuardDuty Findings adapter.

Maps between GuardDuty finding payloads (as returned by boto3
``get_findings``) and the TicketSync IR.  All real boto3 calls are
mediated through an injected client object so the adapter can be fully
tested without AWS credentials.

The client must expose:
    client.list_findings(DetectorId=..., **kwargs) -> dict
    client.get_findings(DetectorId=..., FindingIds=[...]) -> dict

GuardDuty field mapping
-----------------------

GuardDuty concept               -> Ticket IR field
------------------------------  ------------------------------------
Id                               source_id
Title                            title
Description                      description
Severity (1.0–10.0 float)        severity (see mapping below)
Service.Archived (bool)          status (True=closed, False=open)
Type                             category
CreatedAt (ISO-8601 string)      created_at
UpdatedAt (ISO-8601 string)      updated_at
Arn                              external_url
AccountId                        entities (AccountEntity)
Region                           tags (added as "region:<value>")
Service.Count (int)              tags (added as "count:<N>")

Severity mapping (GuardDuty scale 1.0–10.0):
  8.0 – 10.0 -> "critical"
  5.0 – 7.9  -> "high"
  2.0 – 4.9  -> "medium"
  1.0 – 1.9  -> "low"

Status mapping:
  Service.Archived = True  -> "closed"
  Service.Archived = False -> "open"

The adapter is read-only: ``write`` raises ``NotImplementedError`` because
GuardDuty findings cannot be created or modified via the findings API
(only archived/unarchived).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ticketsync.models import AccountEntity, Entity, SeverityLevel, Ticket, TicketStatus

# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

_DEFAULT_BATCH_SIZE: int = 50  # GuardDuty get_findings max is 50


def _map_severity(score: float) -> SeverityLevel:
    """Map a GuardDuty float severity score to a TicketSync SeverityLevel."""
    if score >= 8.0:
        return "critical"
    if score >= 5.0:
        return "high"
    if score >= 2.0:
        return "medium"
    return "low"


def _map_status(service: dict[str, Any]) -> TicketStatus:
    """Map GuardDuty service metadata to a TicketSync TicketStatus."""
    if service.get("Archived", False):
        return "closed"
    return "open"


def _parse_utc(ts: str | None) -> datetime:
    """Parse an ISO-8601 string to a timezone-aware UTC datetime."""
    if not ts:
        return datetime.now(timezone.utc)
    normalised = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(normalised)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _build_entities(finding: dict[str, Any]) -> list[Entity]:
    """Extract known entities from a GuardDuty finding."""
    entities: list[Entity] = []
    account_id: str = str(finding.get("AccountId", ""))
    if account_id:
        entities.append(
            AccountEntity(
                uid=account_id,
                name=account_id,
                type="aws",
            )
        )
    return entities


class GuardDutyFindingsAdapter:
    """Adapter for AWS GuardDuty Findings.

    Parameters
    ----------
    client:
        A boto3 GuardDuty client (or compatible stub) with
        ``list_findings`` and ``get_findings`` methods.
    detector_id:
        The GuardDuty detector ID for the target account and region.
    region:
        AWS region name.  Stored for reference only.
    system_name:
        The ``source_system`` label embedded in produced tickets.
    batch_size:
        Number of finding IDs to pass per ``get_findings`` call.
        Maximum allowed by AWS is 50.
    """

    system_name: str

    def __init__(
        self,
        client: Any,
        detector_id: str,
        region: str = "us-east-1",
        system_name: str = "guardduty",
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._client = client
        self._detector_id = detector_id
        self._region = region
        self.system_name = system_name
        self._batch_size = min(batch_size, _DEFAULT_BATCH_SIZE)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_all_finding_ids(
        self,
        finding_criteria: dict[str, Any] | None = None,
    ) -> list[str]:
        """Page through list_findings and return all finding IDs."""
        ids: list[str] = []
        kwargs: dict[str, Any] = {
            "DetectorId": self._detector_id,
            "MaxResults": 50,
        }
        if finding_criteria:
            kwargs["FindingCriteria"] = finding_criteria

        while True:
            response: dict[str, Any] = self._client.list_findings(**kwargs)
            ids.extend(response.get("FindingIds", []))
            next_token: str = response.get("NextToken", "")
            if not next_token:
                break
            kwargs["NextToken"] = next_token

        return ids

    def _get_findings_batch(self, finding_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch up to ``batch_size`` findings in one API call."""
        if not finding_ids:
            return []
        response: dict[str, Any] = self._client.get_findings(
            DetectorId=self._detector_id,
            FindingIds=finding_ids,
        )
        return list(response.get("Findings", []))

    def _fetch_all_findings(
        self,
        finding_criteria: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """List + batch-fetch all findings matching the given criteria."""
        ids = self._list_all_finding_ids(finding_criteria)
        findings: list[dict[str, Any]] = []
        for i in range(0, len(ids), self._batch_size):
            batch = ids[i : i + self._batch_size]
            findings.extend(self._get_findings_batch(batch))
        return findings

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def to_ticket(self, raw: dict[str, object]) -> Ticket:
        """Map a GuardDuty finding dict (from get_findings) to a Ticket IR."""
        finding: dict[str, Any] = dict(raw)

        service: dict[str, Any] = finding.get("Service", {}) or {}
        severity_score: float = float(finding.get("Severity", 1.0))
        severity: SeverityLevel = _map_severity(severity_score)
        status: TicketStatus = _map_status(service)

        tags: list[str] = []
        region: str = str(finding.get("Region", ""))
        if region:
            tags.append(f"region:{region}")
        count: int = int(service.get("Count", 0))
        if count:
            tags.append(f"count:{count}")

        entities = _build_entities(finding)

        return Ticket.model_validate({
            "source_system": self.system_name,
            "source_id": str(finding.get("Id", "")),
            "title": str(finding.get("Title", "") or "GuardDuty Finding"),
            "description": str(finding.get("Description", "") or ""),
            "severity": severity,
            "status": status,
            "category": str(finding.get("Type", "")),
            "tags": tags,
            "created_at": _parse_utc(finding.get("CreatedAt")),
            "updated_at": _parse_utc(finding.get("UpdatedAt")),
            "external_url": str(finding.get("Arn", "")),
            "entities": [e.model_dump() for e in entities],
            "raw": finding,
        })

    def from_ticket(self, ticket: Ticket) -> dict[str, object]:
        """Map a Ticket IR back to GuardDuty-compatible fields.

        GuardDuty findings are immutable via the findings API; this method
        returns the minimal dict that identifies the finding.  Use
        ``update_findings_feedback`` or ``archive_findings`` directly for
        state changes.
        """
        return {
            "FindingId": ticket.source_id,
            "Title": ticket.title,
            "Description": ticket.description,
        }

    def fetch_new(self, since: datetime | None = None) -> list[dict[str, object]]:
        """Fetch GuardDuty findings, optionally filtered by update time.

        Parameters
        ----------
        since:
            If provided, only findings with ``UpdatedAt`` strictly after
            this timestamp are returned.  GuardDuty's FindingCriteria
            supports date filters via the ``updatedAt`` criterion.
        """
        finding_criteria: dict[str, Any] | None = None
        if since is not None:
            since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            # GuardDuty FindingCriteria uses epoch milliseconds for date fields
            finding_criteria = {
                "Criterion": {
                    "updatedAt": {
                        "GreaterThan": int(since_utc.timestamp() * 1000),
                    }
                }
            }

        findings = self._fetch_all_findings(finding_criteria)
        return list(findings)

    def write(self, ticket: Ticket) -> str:
        """Not supported — GuardDuty findings are managed by AWS.

        Use ``archive_findings`` or ``update_findings_feedback`` directly
        if you need to change finding state.

        Raises
        ------
        NotImplementedError
            Always.
        """
        raise NotImplementedError(
            "GuardDutyFindingsAdapter does not support write operations. "
            "GuardDuty findings are managed by AWS; use archive_findings "
            "or update_findings_feedback to change finding state."
        )
