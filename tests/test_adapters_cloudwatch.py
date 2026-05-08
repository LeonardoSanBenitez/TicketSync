"""Tests for CloudWatchAlarmsAdapter.

All tests use a stub client — no real AWS credentials required.
Coverage targets:
- to_ticket(): all state mappings (ALARM/OK/INSUFFICIENT_DATA), title construction,
  tags, timestamps, external_url
- from_ticket(): read-only adapter — write raises NotImplementedError
- fetch_new(): state filtering, time filtering, combined
- Edge cases: missing fields, unknown state, no namespace/metric, all three states
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ticketsync.adapters.cloudwatch_alarms import CloudWatchAlarmsAdapter
from ticketsync.models import Ticket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_stub_client(
    alarms: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Return a MagicMock CloudWatch client whose describe_alarms returns alarms."""
    client = MagicMock()
    client.describe_alarms.return_value = {
        "MetricAlarms": alarms if alarms is not None else []
    }
    return client


def load_fixture(name: str) -> dict[str, Any]:
    fixtures_dir = Path(__file__).parent / "fixtures"
    return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))


def make_adapter(
    client: MagicMock | None = None,
    region: str = "us-east-1",
    state_filter: list[str] | None = None,
) -> CloudWatchAlarmsAdapter:
    return CloudWatchAlarmsAdapter(
        client=client or make_stub_client(),
        region=region,
        state_filter=state_filter,
    )


def make_alarm(
    name: str = "TestAlarm",
    state: str = "ALARM",
    namespace: str = "AWS/EC2",
    metric: str = "CPUUtilization",
    description: str = "Test description",
    arn: str = "arn:aws:cloudwatch:us-east-1:123:alarm:TestAlarm",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "AlarmName": name,
        "AlarmArn": arn,
        "AlarmDescription": description,
        "StateValue": state,
        "Namespace": namespace,
        "MetricName": metric,
        "AlarmConfigurationUpdatedTimestamp": created_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        "StateUpdatedTimestamp": updated_at or datetime(2026, 1, 2, tzinfo=timezone.utc),
    }


# ---------------------------------------------------------------------------
# to_ticket
# ---------------------------------------------------------------------------


class TestToTicket:
    def test_fixture_mapping(self) -> None:
        """Basic smoke test using the JSON fixture (no datetimes in fixture)."""
        adapter = make_adapter()
        raw = load_fixture("cloudwatch_alarm.json")
        t = adapter.to_ticket(raw)

        assert t.source_id == "HighCPU-prod-web-01"
        assert t.title == "[AWS/EC2] CPUUtilization: HighCPU-prod-web-01"
        assert t.severity == "high"   # ALARM -> high
        assert t.status == "open"     # ALARM -> open
        assert "namespace:AWS/EC2" in t.tags
        assert "metric:CPUUtilization" in t.tags
        assert t.external_url == "arn:aws:cloudwatch:us-east-1:123456789012:alarm:HighCPU-prod-web-01"

    @pytest.mark.parametrize(
        "state,expected_severity,expected_status",
        [
            ("ALARM", "high", "open"),
            ("OK", "informational", "resolved"),
            ("INSUFFICIENT_DATA", "medium", "open"),
        ],
    )
    def test_state_to_severity_and_status(
        self, state: str, expected_severity: str, expected_status: str
    ) -> None:
        adapter = make_adapter()
        alarm = make_alarm(state=state)
        t = adapter.to_ticket(alarm)
        assert t.severity == expected_severity
        assert t.status == expected_status

    def test_unknown_state_falls_back_to_medium_open(self) -> None:
        adapter = make_adapter()
        alarm = make_alarm(state="BANANA")
        t = adapter.to_ticket(alarm)
        assert t.severity == "medium"
        assert t.status == "open"

    def test_title_with_namespace_and_metric(self) -> None:
        adapter = make_adapter()
        alarm = make_alarm(namespace="AWS/RDS", metric="DatabaseConnections", name="MyAlarm")
        t = adapter.to_ticket(alarm)
        assert t.title == "[AWS/RDS] DatabaseConnections: MyAlarm"

    def test_title_without_namespace(self) -> None:
        adapter = make_adapter()
        alarm = make_alarm(namespace="", metric="CPUUtilization", name="MyAlarm")
        t = adapter.to_ticket(alarm)
        assert t.title == "CPUUtilization: MyAlarm"

    def test_title_without_namespace_or_metric(self) -> None:
        adapter = make_adapter()
        alarm: dict[str, Any] = {"AlarmName": "JustName", "StateValue": "ALARM"}
        t = adapter.to_ticket(alarm)
        assert t.title == "JustName"

    def test_namespace_and_metric_as_tags(self) -> None:
        adapter = make_adapter()
        alarm = make_alarm(namespace="AWS/EC2", metric="NetworkIn")
        t = adapter.to_ticket(alarm)
        assert "namespace:AWS/EC2" in t.tags
        assert "metric:NetworkIn" in t.tags

    def test_no_namespace_no_namespace_tag(self) -> None:
        adapter = make_adapter()
        alarm: dict[str, Any] = {
            "AlarmName": "X",
            "StateValue": "ALARM",
            "MetricName": "Latency",
        }
        t = adapter.to_ticket(alarm)
        assert all(not tag.startswith("namespace:") for tag in t.tags)
        assert "metric:Latency" in t.tags

    def test_description_preserved(self) -> None:
        adapter = make_adapter()
        alarm = make_alarm(description="CPU exceeded threshold for 5 minutes.")
        t = adapter.to_ticket(alarm)
        assert t.description == "CPU exceeded threshold for 5 minutes."

    def test_empty_description_becomes_empty_string(self) -> None:
        adapter = make_adapter()
        alarm: dict[str, Any] = {"AlarmName": "X", "StateValue": "ALARM", "AlarmDescription": ""}
        t = adapter.to_ticket(alarm)
        assert t.description == ""

    def test_none_description_becomes_empty_string(self) -> None:
        adapter = make_adapter()
        alarm: dict[str, Any] = {"AlarmName": "X", "StateValue": "ALARM", "AlarmDescription": None}
        t = adapter.to_ticket(alarm)
        assert t.description == ""

    def test_arn_as_external_url(self) -> None:
        adapter = make_adapter()
        alarm = make_alarm(arn="arn:aws:cloudwatch:eu-west-1:999:alarm:MyAlarm")
        t = adapter.to_ticket(alarm)
        assert t.external_url == "arn:aws:cloudwatch:eu-west-1:999:alarm:MyAlarm"

    def test_timestamps_from_alarm(self) -> None:
        created = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
        updated = datetime(2026, 4, 20, 8, 30, tzinfo=timezone.utc)
        adapter = make_adapter()
        alarm = make_alarm(created_at=created, updated_at=updated)
        t = adapter.to_ticket(alarm)
        assert t.created_at == created
        assert t.updated_at == updated

    def test_missing_timestamps_fallback_to_now(self) -> None:
        adapter = make_adapter()
        alarm: dict[str, Any] = {"AlarmName": "X", "StateValue": "ALARM"}
        before = datetime.now(timezone.utc)
        t = adapter.to_ticket(alarm)
        after = datetime.now(timezone.utc)
        assert before <= t.created_at <= after
        assert before <= t.updated_at <= after

    def test_raw_preserved(self) -> None:
        adapter = make_adapter()
        alarm = make_alarm(name="RawTest")
        t = adapter.to_ticket(alarm)
        assert t.raw["AlarmName"] == "RawTest"

    def test_source_system_default(self) -> None:
        adapter = make_adapter()
        alarm = make_alarm()
        t = adapter.to_ticket(alarm)
        assert t.source_system == "cloudwatch_alarms"

    def test_custom_system_name(self) -> None:
        client = make_stub_client()
        adapter = CloudWatchAlarmsAdapter(client=client, system_name="prod-cw")
        alarm = make_alarm()
        t = adapter.to_ticket(alarm)
        assert t.source_system == "prod-cw"


# ---------------------------------------------------------------------------
# from_ticket (read-only, partial mapping)
# ---------------------------------------------------------------------------


class TestFromTicket:
    def test_returns_dict_with_name_and_description(self) -> None:
        adapter = make_adapter()
        t = Ticket(
            source_system="cloudwatch_alarms",
            source_id="MyAlarm",
            title="My Alarm Title",
            description="Some description",
            severity="high",
        )
        payload = adapter.from_ticket(t)
        assert payload["AlarmName"] == "MyAlarm"
        assert payload["AlarmDescription"] == "Some description"

    def test_uses_title_when_source_id_empty(self) -> None:
        adapter = make_adapter()
        t = Ticket(
            source_system="cloudwatch_alarms",
            source_id="",
            title="Fallback Title",
            severity="medium",
        )
        payload = adapter.from_ticket(t)
        assert payload["AlarmName"] == "Fallback Title"


# ---------------------------------------------------------------------------
# write (not supported)
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_raises_not_implemented(self) -> None:
        adapter = make_adapter()
        t = Ticket(
            source_system="cloudwatch_alarms",
            source_id="SomeAlarm",
            title="Read-only alarm",
            severity="high",
        )
        with pytest.raises(NotImplementedError):
            adapter.write(t)


# ---------------------------------------------------------------------------
# fetch_new — state filtering
# ---------------------------------------------------------------------------


class TestFetchNewStateFilter:
    def test_default_filter_excludes_ok_alarms(self) -> None:
        alarms = [
            make_alarm(name="A1", state="ALARM"),
            make_alarm(name="A2", state="OK"),
            make_alarm(name="A3", state="INSUFFICIENT_DATA"),
        ]
        client = make_stub_client(alarms=alarms)
        adapter = make_adapter(client=client)  # default: ALARM + INSUFFICIENT_DATA
        result = adapter.fetch_new()
        names = [r["AlarmName"] for r in result]
        assert "A1" in names
        assert "A3" in names
        assert "A2" not in names

    def test_custom_filter_alarm_only(self) -> None:
        alarms = [
            make_alarm(name="A1", state="ALARM"),
            make_alarm(name="A2", state="OK"),
            make_alarm(name="A3", state="INSUFFICIENT_DATA"),
        ]
        client = make_stub_client(alarms=alarms)
        adapter = make_adapter(client=client, state_filter=["ALARM"])
        result = adapter.fetch_new()
        names = [r["AlarmName"] for r in result]
        assert names == ["A1"]

    def test_custom_filter_ok_only(self) -> None:
        alarms = [
            make_alarm(name="A1", state="ALARM"),
            make_alarm(name="A2", state="OK"),
        ]
        client = make_stub_client(alarms=alarms)
        adapter = make_adapter(client=client, state_filter=["OK"])
        result = adapter.fetch_new()
        names = [r["AlarmName"] for r in result]
        assert names == ["A2"]

    def test_default_filter_is_alarm_and_insufficient_data(self) -> None:
        """Passing state_filter=None to the constructor uses the built-in default."""
        alarms = [
            make_alarm(name="A1", state="ALARM"),
            make_alarm(name="A2", state="OK"),
            make_alarm(name="A3", state="INSUFFICIENT_DATA"),
        ]
        client = make_stub_client(alarms=alarms)
        # state_filter=None in constructor -> uses _DEFAULT_STATE_FILTER = [ALARM, INSUFFICIENT_DATA]
        adapter = CloudWatchAlarmsAdapter(client=client, state_filter=None)
        result = adapter.fetch_new()
        names = [r["AlarmName"] for r in result]
        assert "A1" in names
        assert "A3" in names
        assert "A2" not in names  # OK excluded by default

    def test_empty_alarm_list_returns_empty(self) -> None:
        client = make_stub_client(alarms=[])
        adapter = make_adapter(client=client)
        assert adapter.fetch_new() == []

    def test_filter_is_case_sensitive(self) -> None:
        alarms = [make_alarm(name="A1", state="alarm")]  # lower-case — should not match
        client = make_stub_client(alarms=alarms)
        adapter = make_adapter(client=client, state_filter=["ALARM"])
        result = adapter.fetch_new()
        assert result == []


# ---------------------------------------------------------------------------
# fetch_new — time filtering
# ---------------------------------------------------------------------------


class TestFetchNewTimeFilter:
    def test_since_excludes_old_alarms(self) -> None:
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        alarms = [
            make_alarm(
                name="Old",
                state="ALARM",
                updated_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            ),
            make_alarm(
                name="New",
                state="ALARM",
                updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            ),
        ]
        client = make_stub_client(alarms=alarms)
        adapter = make_adapter(client=client, state_filter=["ALARM"])
        result = adapter.fetch_new(since=since)
        names = [r["AlarmName"] for r in result]
        assert "New" in names
        assert "Old" not in names

    def test_since_exact_boundary_excluded(self) -> None:
        """Alarms updated exactly at since are excluded (strictly after)."""
        ts = datetime(2026, 3, 1, tzinfo=timezone.utc)
        alarms = [make_alarm(name="Boundary", state="ALARM", updated_at=ts)]
        client = make_stub_client(alarms=alarms)
        adapter = make_adapter(client=client, state_filter=["ALARM"])
        result = adapter.fetch_new(since=ts)
        assert result == []

    def test_since_none_returns_all_matching_state(self) -> None:
        alarms = [
            make_alarm(name="A1", state="ALARM", updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            make_alarm(name="A2", state="ALARM", updated_at=datetime(2025, 6, 1, tzinfo=timezone.utc)),
        ]
        client = make_stub_client(alarms=alarms)
        adapter = make_adapter(client=client, state_filter=["ALARM"])
        result = adapter.fetch_new(since=None)
        assert len(result) == 2

    def test_alarm_without_timestamp_excluded_when_since_provided(self) -> None:
        """Alarms missing StateUpdatedTimestamp are skipped when since is given."""
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        alarm: dict[str, Any] = {"AlarmName": "NoTimestamp", "StateValue": "ALARM"}
        client = make_stub_client(alarms=[alarm])
        adapter = make_adapter(client=client, state_filter=["ALARM"])
        result = adapter.fetch_new(since=since)
        assert result == []

    def test_combined_state_and_time_filter(self) -> None:
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        alarms = [
            make_alarm(name="OldAlarm", state="ALARM",
                       updated_at=datetime(2026, 2, 1, tzinfo=timezone.utc)),
            make_alarm(name="NewAlarm", state="ALARM",
                       updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc)),
            make_alarm(name="NewOK", state="OK",
                       updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc)),
        ]
        client = make_stub_client(alarms=alarms)
        adapter = make_adapter(client=client, state_filter=["ALARM"])
        result = adapter.fetch_new(since=since)
        names = [r["AlarmName"] for r in result]
        assert names == ["NewAlarm"]

    def test_since_naive_datetime_treated_as_utc(self) -> None:
        """A naive since datetime should still work (treated as UTC)."""
        since_naive = datetime(2026, 3, 1)  # no tzinfo
        alarms = [
            make_alarm(name="New", state="ALARM",
                       updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc)),
        ]
        client = make_stub_client(alarms=alarms)
        adapter = make_adapter(client=client, state_filter=["ALARM"])
        result = adapter.fetch_new(since=since_naive)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# describe_alarms API call verification
# ---------------------------------------------------------------------------


class TestDescribeAlarmsCall:
    def test_describe_alarms_is_called(self) -> None:
        client = make_stub_client(alarms=[])
        adapter = make_adapter(client=client)
        adapter.fetch_new()
        client.describe_alarms.assert_called_once()

    def test_max_records_passed(self) -> None:
        client = make_stub_client(alarms=[])
        adapter = make_adapter(client=client)
        adapter.fetch_new()
        call_kwargs = client.describe_alarms.call_args
        kwargs = call_kwargs[1] if call_kwargs[1] else {}
        assert kwargs.get("MaxRecords") == 100
