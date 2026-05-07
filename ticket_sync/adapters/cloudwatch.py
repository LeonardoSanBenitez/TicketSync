"""AWS CloudWatch alarm adapter for TicketSync.

Parses CloudWatch alarm state-change notifications as delivered by SNS
(when an SNS topic subscribes to the alarm and forwards the payload).

Payload reference:
    https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html

Example payload::

    {
        "AlarmName": "CPU-High",
        "AlarmDescription": "CPU above 80% for 5 minutes",
        "NewStateValue": "ALARM",
        "OldStateValue": "OK",
        "NewStateReason": "Threshold Crossed: 1 datapoint...",
        "StateChangeTime": "2024-01-15T10:30:00.000+0000",
        "AlarmArn": "arn:aws:cloudwatch:us-east-1:123456789:alarm:CPU-High",
        "Region": "US East (N. Virginia)",
        "AWSAccountId": "123456789012",
        "Trigger": {
            "MetricName": "CPUUtilization",
            "Namespace": "AWS/EC2",
            "Dimensions": [{"name": "InstanceId", "value": "i-0abc123"}]
        }
    }
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from ticket_sync.adapters.base import BaseAdapter
from ticket_sync.models import Ticket, TicketPriority, TicketSource, TicketStatus

# CloudWatch NewStateValue -> TicketStatus
_STATE_TO_STATUS: Dict[str, TicketStatus] = {
    "ALARM": TicketStatus.OPEN,
    "OK": TicketStatus.RESOLVED,
    "INSUFFICIENT_DATA": TicketStatus.UNKNOWN,
}


class CloudWatchAdapter(BaseAdapter):
    """Parse AWS CloudWatch alarm state-change payloads into Tickets.

    Priority mapping:
        CloudWatch alarms do not have a native priority field.  This adapter
        defaults to ``TicketPriority.HIGH`` for ``ALARM`` state and
        ``TicketPriority.UNKNOWN`` for all other states, since a firing alarm
        is actionable by definition.  Callers can override the priority
        afterwards or use TicketTriage to apply policy-based priority.

    >>> adapter = CloudWatchAdapter()
    >>> payload = {
    ...     "AlarmName": "CPU-High",
    ...     "AlarmDescription": "CPU above 80%",
    ...     "NewStateValue": "ALARM",
    ...     "OldStateValue": "OK",
    ...     "StateChangeTime": "2024-01-15T10:30:00.000+0000",
    ...     "AlarmArn": "arn:aws:cloudwatch:us-east-1:123:alarm:CPU-High",
    ...     "AWSAccountId": "123456789012",
    ... }
    >>> ticket = adapter.parse(payload)
    >>> ticket.title
    'CPU-High'
    >>> ticket.source.value
    'cloudwatch'
    >>> ticket.status.value
    'open'
    """

    def parse(self, payload: Dict[str, Any]) -> Ticket:
        """Parse a CloudWatch alarm payload into a Ticket.

        Args:
            payload: Decoded JSON payload from a CloudWatch alarm notification.

        Returns:
            A :class:`Ticket` representing the alarm state change.

        Raises:
            KeyError: If ``AlarmName`` or ``NewStateValue`` is missing.
        """
        alarm_name: str = payload["AlarmName"]
        new_state: str = payload["NewStateValue"]

        description = str(payload.get("AlarmDescription", ""))
        state_reason = payload.get("NewStateReason", "")
        if state_reason and state_reason not in description:
            description = f"{description}\n\n{state_reason}".strip()

        status = _STATE_TO_STATUS.get(new_state, TicketStatus.UNKNOWN)
        priority = (
            TicketPriority.HIGH if new_state == "ALARM" else TicketPriority.UNKNOWN
        )

        created_at: datetime | None = None
        raw_time = payload.get("StateChangeTime")
        if raw_time:
            # CloudWatch uses format: "2024-01-15T10:30:00.000+0000"
            # Python's fromisoformat handles ISO 8601 but not "+0000" pre-3.11
            raw_time = str(raw_time).replace("+0000", "+00:00")
            try:
                created_at = datetime.fromisoformat(raw_time)
            except ValueError:
                created_at = None

        tags: List[str] = []
        trigger = payload.get("Trigger", {})
        metric_name = trigger.get("MetricName")
        if metric_name:
            tags.append(f"metric:{metric_name}")
        namespace = trigger.get("Namespace")
        if namespace:
            tags.append(f"namespace:{namespace}")

        alarm_arn = payload.get("AlarmArn", "")

        metadata: Dict[str, Any] = {
            "old_state": payload.get("OldStateValue"),
            "region": payload.get("Region"),
            "account_id": payload.get("AWSAccountId"),
            "alarm_arn": alarm_arn,
        }
        if trigger:
            metadata["trigger"] = trigger

        return Ticket(
            title=alarm_name,
            description=description,
            source=TicketSource.CLOUDWATCH,
            source_id=alarm_arn or alarm_name,
            priority=priority,
            status=status,
            tags=tags,
            metadata={k: v for k, v in metadata.items() if v is not None},
            created_at=created_at,
        )
