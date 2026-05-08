"""AWS CloudWatch Alarms adapter.

Maps between CloudWatch alarm payloads (as returned by boto3
``describe_alarms``) and the TicketSync IR.  All real boto3 calls are
mediated through an injected client object so the adapter can be fully
tested without AWS credentials.

The client must expose:
    client.describe_alarms(**kwargs) -> dict

CloudWatch field mapping
------------------------

CloudWatch concept              -> Ticket IR field
------------------------------  ------------------------------------
AlarmName                       source_id
"[Namespace] MetricName: Name"  title  (constructed from Namespace+MetricName+AlarmName)
AlarmDescription                description
StateValue (ALARM/OK/INSUFFICIENT_DATA)
                                severity + status (see below)
AlarmArn                        external_url
StateUpdatedTimestamp           updated_at
AlarmConfigurationUpdatedTimestamp
                                created_at (best proxy available)
Namespace                       tags (added as "namespace:<value>")
MetricName                      tags (added as "metric:<value>")

Severity mapping:
  ALARM             -> "high"
  OK                -> "informational"
  INSUFFICIENT_DATA -> "medium"

Status mapping:
  ALARM             -> "open"
  OK                -> "resolved"
  INSUFFICIENT_DATA -> "open"   (unresolvable / unknown — still needs attention)

Parameters
----------
client:
    A boto3 CloudWatch client (or compatible stub) with a
    ``describe_alarms`` method.  Production code passes
    ``boto3.client("cloudwatch", region_name=...)``.
region:
    AWS region name, stored for reference.
system_name:
    The ``source_system`` label embedded in produced tickets.
state_filter:
    List of StateValue strings to filter on when calling ``fetch_new``.
    Defaults to ``["ALARM", "INSUFFICIENT_DATA"]`` (exclude OK alarms).
    Pass ``None`` to fetch alarms in all states.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ticketsync.models import SeverityLevel, Ticket, TicketStatus


_SEVERITY_MAP: dict[str, SeverityLevel] = {
    "ALARM": "high",
    "OK": "informational",
    "INSUFFICIENT_DATA": "medium",
}

_STATUS_MAP: dict[str, TicketStatus] = {
    "ALARM": "open",
    "OK": "resolved",
    "INSUFFICIENT_DATA": "open",
}

_DEFAULT_STATE_FILTER: list[str] = ["ALARM", "INSUFFICIENT_DATA"]


def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _build_title(alarm: dict[str, Any]) -> str:
    """Construct a human-readable title from alarm metadata."""
    namespace: str = str(alarm.get("Namespace", ""))
    metric: str = str(alarm.get("MetricName", ""))
    name: str = str(alarm.get("AlarmName", ""))

    if namespace and metric:
        return f"[{namespace}] {metric}: {name}"
    if metric:
        return f"{metric}: {name}"
    return name


class CloudWatchAlarmsAdapter:
    """Adapter for AWS CloudWatch Alarms.

    Parameters
    ----------
    client:
        A boto3 CloudWatch client (or compatible stub).
    region:
        AWS region name.  Stored for reference; the client itself determines
        the actual endpoint.
    system_name:
        The ``source_system`` label embedded in tickets produced here.
    state_filter:
        List of alarm state values to include when fetching.  If ``None``,
        all states are returned.
    """

    system_name: str

    def __init__(
        self,
        client: Any,
        region: str = "us-east-1",
        system_name: str = "cloudwatch_alarms",
        state_filter: list[str] | None = None,
    ) -> None:
        self._client = client
        self._region = region
        self.system_name = system_name
        self._state_filter: list[str] | None = (
            list(state_filter) if state_filter is not None else list(_DEFAULT_STATE_FILTER)
        )

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def to_ticket(self, raw: dict[str, object]) -> Ticket:
        """Map a CloudWatch alarm dict (from describe_alarms) to a Ticket IR."""
        alarm: dict[str, Any] = dict(raw)

        state: str = str(alarm.get("StateValue", "INSUFFICIENT_DATA"))
        severity: SeverityLevel = _SEVERITY_MAP.get(state, "medium")
        status: TicketStatus = _STATUS_MAP.get(state, "open")

        tags: list[str] = []
        namespace: str = str(alarm.get("Namespace", ""))
        metric: str = str(alarm.get("MetricName", ""))
        if namespace:
            tags.append(f"namespace:{namespace}")
        if metric:
            tags.append(f"metric:{metric}")

        created_raw = alarm.get("AlarmConfigurationUpdatedTimestamp")
        updated_raw = alarm.get("StateUpdatedTimestamp")

        created_at = _ensure_utc(
            created_raw if isinstance(created_raw, datetime) else None
        )
        updated_at = _ensure_utc(
            updated_raw if isinstance(updated_raw, datetime) else None
        )

        return Ticket.model_validate({
            "source_system": self.system_name,
            "source_id": str(alarm.get("AlarmName", "")),
            "title": _build_title(alarm),
            "description": str(alarm.get("AlarmDescription", "") or ""),
            "severity": severity,
            "status": status,
            "tags": tags,
            "created_at": created_at,
            "updated_at": updated_at,
            "external_url": str(alarm.get("AlarmArn", "")),
            "raw": alarm,
        })

    def from_ticket(self, ticket: Ticket) -> dict[str, object]:
        """Map a Ticket IR back to CloudWatch alarm fields.

        Note: CloudWatch alarms are read-only via their alarm API — you
        cannot *create* or *update* alarms via describe_alarms.  This
        method returns a dict suitable for passing to ``put_metric_alarm``
        or for auditing purposes only.  The ``write`` method raises
        ``NotImplementedError`` because alarm state is managed by AWS.
        """
        return {
            "AlarmName": ticket.source_id or ticket.title,
            "AlarmDescription": ticket.description,
        }

    def fetch_new(self, since: datetime | None = None) -> list[dict[str, object]]:
        """Fetch CloudWatch alarms, optionally filtered by state.

        Parameters
        ----------
        since:
            If provided, only alarms whose ``StateUpdatedTimestamp`` is
            strictly after this datetime are returned.  Note: CloudWatch
            describe_alarms does not natively support time filters — we
            filter client-side.
        """
        kwargs: dict[str, Any] = {"MaxRecords": 100}
        if self._state_filter is not None:
            kwargs["StateValue"] = self._state_filter[0] if len(self._state_filter) == 1 else None
            # CloudWatch only accepts a single StateValue; if multiple are
            # requested we fetch without state filter and filter client-side.

        # Always fetch all states when multiple states are requested and then
        # filter client-side (simpler and avoids multiple API calls).
        response: dict[str, Any] = self._client.describe_alarms(MaxRecords=100)
        alarms: list[dict[str, Any]] = response.get("MetricAlarms", [])

        # Filter by state
        if self._state_filter is not None:
            state_set = set(self._state_filter)
            alarms = [a for a in alarms if str(a.get("StateValue", "")) in state_set]

        # Filter by time
        if since is not None:
            since_utc = _ensure_utc(since) if since.tzinfo is None else since
            filtered: list[dict[str, Any]] = []
            for alarm in alarms:
                updated = alarm.get("StateUpdatedTimestamp")
                if isinstance(updated, datetime):
                    updated_utc = _ensure_utc(updated)
                    if updated_utc > since_utc:
                        filtered.append(alarm)
            return list(filtered)

        return list(alarms)

    def write(self, ticket: Ticket) -> str:
        """Not supported — CloudWatch alarm state is managed by AWS.

        CloudWatch alarms cannot be created or updated via the alarms
        describe/list API.  Use ``put_metric_alarm`` directly if you need
        to manage alarms.

        Raises
        ------
        NotImplementedError
            Always.
        """
        raise NotImplementedError(
            "CloudWatchAlarmsAdapter does not support write operations. "
            "CloudWatch alarm state is managed by AWS; use put_metric_alarm "
            "directly to create or modify alarms."
        )
