"""Tests for the CloudWatch alarm adapter."""

from datetime import timezone

import pytest

from ticket_sync.adapters.cloudwatch import CloudWatchAdapter
from ticket_sync.models import TicketPriority, TicketSource, TicketStatus

MINIMAL_ALARM = {
    "AlarmName": "CPU-High",
    "NewStateValue": "ALARM",
}

FULL_ALARM = {
    "AlarmName": "CPUUtilization-alarm",
    "AlarmDescription": "CPU above 80% for 5 consecutive minutes",
    "NewStateValue": "ALARM",
    "OldStateValue": "OK",
    "NewStateReason": "Threshold Crossed: 1 datapoint [82.5 (15/01/24 10:25:00)] was greater than the threshold (80.0).",
    "StateChangeTime": "2024-01-15T10:30:00.000+0000",
    "AlarmArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:CPUUtilization-alarm",
    "Region": "US East (N. Virginia)",
    "AWSAccountId": "123456789012",
    "Trigger": {
        "MetricName": "CPUUtilization",
        "Namespace": "AWS/EC2",
        "Dimensions": [{"name": "InstanceId", "value": "i-0abc12345"}],
        "Period": 300,
        "Statistic": "Average",
        "Threshold": 80.0,
    },
}

RESOLVED_ALARM = {
    "AlarmName": "DiskSpace-Low",
    "NewStateValue": "OK",
    "OldStateValue": "ALARM",
    "StateChangeTime": "2024-01-15T11:00:00.000+0000",
    "AlarmArn": "arn:aws:cloudwatch:eu-west-1:999:alarm:DiskSpace-Low",
}


@pytest.fixture
def adapter() -> CloudWatchAdapter:
    return CloudWatchAdapter()


class TestCloudWatchAdapterTitle:
    def test_alarm_name_becomes_title(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ALARM)
        assert ticket.title == "CPU-High"

    def test_full_alarm_title(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(FULL_ALARM)
        assert ticket.title == "CPUUtilization-alarm"


class TestCloudWatchAdapterSource:
    def test_source_is_cloudwatch(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ALARM)
        assert ticket.source == TicketSource.CLOUDWATCH


class TestCloudWatchAdapterStatus:
    def test_alarm_state_maps_to_open(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ALARM)
        assert ticket.status == TicketStatus.OPEN

    def test_ok_state_maps_to_resolved(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(RESOLVED_ALARM)
        assert ticket.status == TicketStatus.RESOLVED

    def test_insufficient_data_maps_to_unknown(self, adapter: CloudWatchAdapter) -> None:
        payload = {**MINIMAL_ALARM, "NewStateValue": "INSUFFICIENT_DATA"}
        ticket = adapter.parse(payload)
        assert ticket.status == TicketStatus.UNKNOWN

    def test_unknown_state_maps_to_unknown(self, adapter: CloudWatchAdapter) -> None:
        payload = {**MINIMAL_ALARM, "NewStateValue": "BOGUS"}
        ticket = adapter.parse(payload)
        assert ticket.status == TicketStatus.UNKNOWN


class TestCloudWatchAdapterPriority:
    def test_alarm_state_maps_to_high(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ALARM)
        assert ticket.priority == TicketPriority.HIGH

    def test_ok_state_maps_to_unknown(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(RESOLVED_ALARM)
        assert ticket.priority == TicketPriority.UNKNOWN


class TestCloudWatchAdapterDescription:
    def test_description_from_alarm_description(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(FULL_ALARM)
        assert "CPU above 80%" in ticket.description

    def test_state_reason_appended_to_description(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(FULL_ALARM)
        assert "Threshold Crossed" in ticket.description

    def test_no_description_no_reason_gives_empty(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ALARM)
        assert ticket.description == ""

    def test_description_only_no_reason(self, adapter: CloudWatchAdapter) -> None:
        payload = {**MINIMAL_ALARM, "AlarmDescription": "something happened"}
        ticket = adapter.parse(payload)
        assert ticket.description == "something happened"


class TestCloudWatchAdapterTags:
    def test_metric_name_in_tags(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(FULL_ALARM)
        assert "metric:CPUUtilization" in ticket.tags

    def test_namespace_in_tags(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(FULL_ALARM)
        assert "namespace:AWS/EC2" in ticket.tags

    def test_no_trigger_no_tags(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ALARM)
        assert ticket.tags == []


class TestCloudWatchAdapterSourceId:
    def test_source_id_from_arn(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(FULL_ALARM)
        assert "alarm:CPUUtilization-alarm" in ticket.source_id  # type: ignore[operator]

    def test_source_id_falls_back_to_alarm_name(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ALARM)
        assert ticket.source_id == "CPU-High"


class TestCloudWatchAdapterCreatedAt:
    def test_created_at_parsed(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(FULL_ALARM)
        assert ticket.created_at is not None
        assert ticket.created_at.year == 2024
        assert ticket.created_at.month == 1
        assert ticket.created_at.day == 15

    def test_created_at_timezone_aware(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(FULL_ALARM)
        assert ticket.created_at is not None
        assert ticket.created_at.tzinfo is not None

    def test_no_state_change_time_gives_none(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ALARM)
        assert ticket.created_at is None

    def test_malformed_timestamp_gives_none(self, adapter: CloudWatchAdapter) -> None:
        payload = {**MINIMAL_ALARM, "StateChangeTime": "not-a-date"}
        ticket = adapter.parse(payload)
        assert ticket.created_at is None


class TestCloudWatchAdapterMetadata:
    def test_account_id_in_metadata(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(FULL_ALARM)
        assert ticket.metadata.get("account_id") == "123456789012"

    def test_region_in_metadata(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(FULL_ALARM)
        assert ticket.metadata.get("region") == "US East (N. Virginia)"

    def test_old_state_in_metadata(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(FULL_ALARM)
        assert ticket.metadata.get("old_state") == "OK"

    def test_trigger_in_metadata(self, adapter: CloudWatchAdapter) -> None:
        ticket = adapter.parse(FULL_ALARM)
        assert "trigger" in ticket.metadata


class TestCloudWatchAdapterErrors:
    def test_missing_alarm_name_raises(self, adapter: CloudWatchAdapter) -> None:
        with pytest.raises(KeyError):
            adapter.parse({"NewStateValue": "ALARM"})

    def test_missing_new_state_value_raises(self, adapter: CloudWatchAdapter) -> None:
        with pytest.raises(KeyError):
            adapter.parse({"AlarmName": "CPU-High"})
