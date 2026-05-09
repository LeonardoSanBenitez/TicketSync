"""AWS integration tests for TicketSync.

These tests use real boto3 calls against live AWS infrastructure.
They are skipped by default and must be run with:

    pytest tests/test_integration_aws.py -m integration -v

AWS credentials must be available via environment variables:
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_DEFAULT_REGION   (defaults to us-east-1 if not set)

Or via any credential mechanism boto3 supports (IAM role, instance profile, etc.).

Test infrastructure (set up by priya, 2026-05-08):
    Region:           us-east-1
    Account:          725533536670
    CloudWatch alarm: ticketsync-test-alarm-1 (always in ALARM state;
                      targets CPUUtilization on nonexistent EC2 with
                      treat-missing-data=breaching, threshold=0)
    OpsCenter item:   oi-2f7c1ac92df8 (pre-existing, Open/Medium/Availability)
    GuardDuty:        detector f0cec0ab8c944e358f7992966bdb3605 (~80 sample findings)
    Security Hub:     hub arn:aws:securityhub:us-east-1:725533536670:hub/default
                      (enabled 2026-05-08, GuardDuty integration active)
    GitHub:           LeonardoSanBenitez/TicketSync (issues enabled 2026-05-08)
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# boto3 import — entire module is skipped if not installed
# ---------------------------------------------------------------------------

_BOTO3_AVAILABLE: bool = False
try:
    import boto3  # type: ignore[import-untyped]
    _BOTO3_AVAILABLE = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Constants (matching priya's infra setup)
# ---------------------------------------------------------------------------

_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
_ALARM_NAME = "ticketsync-test-alarm-1"
_OPSITEM_ID = "oi-2f7c1ac92df8"
_GUARDDUTY_DETECTOR_ID = "f0cec0ab8c944e358f7992966bdb3605"
_GUARDDUTY_SAMPLE_FINDING_ID = "5bfdff189304423d8afcadfd89c3e1db"
_SECURITYHUB_HUB_ARN = "arn:aws:securityhub:us-east-1:725533536670:hub/default"


# ---------------------------------------------------------------------------
# Helper: check whether boto3 can resolve credentials
# ---------------------------------------------------------------------------


def _has_boto3_credentials() -> bool:
    """Return True if boto3 can find credentials in the current environment."""
    if not _BOTO3_AVAILABLE:
        return False
    try:
        session = boto3.Session(region_name=_REGION)
        creds = session.get_credentials()
        return creds is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Skip markers
# ---------------------------------------------------------------------------

_skip_no_boto3 = pytest.mark.skipif(
    not _BOTO3_AVAILABLE,
    reason="boto3 not installed — install with: pip install boto3",
)

_skip_no_creds = pytest.mark.skipif(
    not _has_boto3_credentials(),
    reason=(
        "No AWS credentials available. "
        "Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION "
        "or configure an IAM role / AWS profile."
    ),
)

# All tests in this file require the integration marker
pytestmark = [pytest.mark.integration, _skip_no_boto3, _skip_no_creds]


# ---------------------------------------------------------------------------
# Client factories
# ---------------------------------------------------------------------------


def _cw_client() -> Any:
    """Return a real boto3 CloudWatch client for the test region."""
    return boto3.client("cloudwatch", region_name=_REGION)


def _ssm_client() -> Any:
    """Return a real boto3 SSM client for the test region."""
    return boto3.client("ssm", region_name=_REGION)


def _gd_client() -> Any:
    """Return a real boto3 GuardDuty client for the test region."""
    return boto3.client("guardduty", region_name=_REGION)


def _sh_client() -> Any:
    """Return a real boto3 Security Hub client for the test region."""
    return boto3.client("securityhub", region_name=_REGION)


# ---------------------------------------------------------------------------
# Test: CloudWatchAlarmsAdapter — read-only fetch
# ---------------------------------------------------------------------------


class TestCloudWatchAlarmFetch:
    """Verify CloudWatchAlarmsAdapter can read the test alarm from live AWS."""

    def test_alarm_exists_and_is_in_alarm_state(self) -> None:
        """ticketsync-test-alarm-1 must be visible in ALARM state."""
        from ticketsync.adapters.cloudwatch_alarms import CloudWatchAlarmsAdapter

        adapter = CloudWatchAlarmsAdapter(
            client=_cw_client(),
            region=_REGION,
            state_filter=["ALARM"],
        )

        raw_alarms = adapter.fetch_new()
        alarm_names = [str(a.get("AlarmName", "")) for a in raw_alarms]

        assert _ALARM_NAME in alarm_names, (
            f"Expected '{_ALARM_NAME}' in ALARM state. "
            f"Found alarms: {alarm_names}"
        )

    def test_alarm_maps_to_ticket_with_correct_fields(self) -> None:
        """The test alarm must map to severity=high, status=open."""
        from ticketsync.adapters.cloudwatch_alarms import CloudWatchAlarmsAdapter

        cw = _cw_client()

        # Fetch the specific alarm by name
        response = cw.describe_alarms(
            AlarmNames=[_ALARM_NAME],
            AlarmTypes=["MetricAlarm"],
        )
        metric_alarms: list[dict[str, Any]] = response.get("MetricAlarms", [])
        assert len(metric_alarms) == 1, (
            f"Expected exactly one alarm named '{_ALARM_NAME}', "
            f"got {len(metric_alarms)}"
        )

        adapter = CloudWatchAlarmsAdapter(client=cw, region=_REGION)
        ticket = adapter.to_ticket(metric_alarms[0])

        assert ticket.source_id == _ALARM_NAME
        assert ticket.severity == "high", (
            f"Expected 'high' (ALARM state), got '{ticket.severity}'"
        )
        assert ticket.status == "open", (
            f"Expected 'open' (ALARM state), got '{ticket.status}'"
        )
        assert ticket.source_system == "cloudwatch_alarms"
        assert ticket.title != "", "Ticket title must not be empty"


# ---------------------------------------------------------------------------
# Test: CloudWatch -> OpsCenter update
# ---------------------------------------------------------------------------


class TestCloudWatchToOpsCenter:
    """Fetch alarm from CloudWatch and sync into the pre-existing OpsCenter OpsItem."""

    def test_sync_alarm_to_opscenter_opsitem(self) -> None:
        """
        Fetch ticketsync-test-alarm-1, convert to Ticket IR, and update
        OpsItem oi-2f7c1ac92df8 in OpsCenter.  Verify via get_ops_item.
        """
        from ticketsync.adapters.cloudwatch_alarms import CloudWatchAlarmsAdapter
        from ticketsync.adapters.opscenter import OpsCenterAdapter

        cw = _cw_client()
        ssm = _ssm_client()

        # Fetch the specific alarm
        response = cw.describe_alarms(
            AlarmNames=[_ALARM_NAME],
            AlarmTypes=["MetricAlarm"],
        )
        metric_alarms: list[dict[str, Any]] = response.get("MetricAlarms", [])
        assert len(metric_alarms) == 1, (
            f"Could not find '{_ALARM_NAME}' in CloudWatch. "
            "Ensure the alarm exists in the target account/region."
        )

        cw_adapter = CloudWatchAlarmsAdapter(client=cw, region=_REGION)
        ticket = cw_adapter.to_ticket(metric_alarms[0])

        # Override source_id so OpsCenterAdapter does an update, not a create
        ticket = ticket.model_copy(update={"source_id": _OPSITEM_ID})

        ops_adapter = OpsCenterAdapter(client=ssm, region=_REGION)
        returned_id = ops_adapter.write(ticket)

        assert returned_id == _OPSITEM_ID, (
            f"Expected returned ID '{_OPSITEM_ID}', got '{returned_id}'"
        )

        # Read back and verify
        get_response = ssm.get_ops_item(OpsItemId=_OPSITEM_ID)
        ops_item: dict[str, Any] = get_response.get("OpsItem", {})

        assert ops_item.get("Title") == ticket.title, (
            f"OpsItem title mismatch: expected '{ticket.title}', "
            f"got '{ops_item.get('Title')}'"
        )
        # high -> "2" in OpsCenter severity mapping
        assert ops_item.get("Severity") == "2", (
            f"OpsItem severity mismatch: expected '2' (high), "
            f"got '{ops_item.get('Severity')}'"
        )


# ---------------------------------------------------------------------------
# Test: SyncEngine end-to-end with real CloudWatch source + LocalFS destination
# ---------------------------------------------------------------------------


class TestSyncEngineWithRealSource:
    """Use SyncEngine to sync real CloudWatch alarms into a temp LocalFS dir."""

    def test_sync_engine_pipeline(self) -> None:
        """
        Full pipeline: CloudWatchAlarmsAdapter (real) -> SyncEngine -> LocalFilesystemAdapter.
        At least one ticket (the test alarm) must land in the destination.
        """
        from ticketsync.adapters.cloudwatch_alarms import CloudWatchAlarmsAdapter
        from ticketsync.adapters.local import LocalFilesystemAdapter
        from ticketsync.config import SyncConfig
        from ticketsync.engine import SyncEngine

        source = CloudWatchAlarmsAdapter(
            client=_cw_client(),
            region=_REGION,
            state_filter=["ALARM"],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = LocalFilesystemAdapter(path=Path(tmpdir))

            config = SyncConfig.from_dict(
                {
                    "source": {"type": "cloudwatch_alarms"},
                    "destination": {"type": "local", "path": tmpdir},
                    "deduplication": True,
                    "lookback_hours": 0,  # fetch all alarms regardless of age
                }
            )

            engine = SyncEngine(source=source, dest=dest, config=config)
            result = engine.run(since=None)

            assert result.written > 0, (
                f"SyncEngine wrote 0 tickets. fetched={result.fetched}, "
                f"errors={result.errors}"
            )

            # Verify the test alarm is in LocalFS
            all_tickets = dest.all_tickets()
            ticket_ids = [t.source_id for t in all_tickets]
            assert _ALARM_NAME in ticket_ids, (
                f"Expected '{_ALARM_NAME}' in synced tickets. "
                f"Found: {ticket_ids}"
            )


# ---------------------------------------------------------------------------
# Test: GuardDutyFindingsAdapter — read-only fetch + mapping
# ---------------------------------------------------------------------------


class TestGuardDutyFindingsFetch:
    """Verify GuardDutyFindingsAdapter can read sample findings from live AWS."""

    def test_fetch_new_returns_findings(self) -> None:
        """list_findings + get_findings must return at least one sample finding."""
        from ticketsync.adapters.guardduty import GuardDutyFindingsAdapter

        adapter = GuardDutyFindingsAdapter(
            client=_gd_client(),
            detector_id=_GUARDDUTY_DETECTOR_ID,
            region=_REGION,
        )
        findings = adapter.fetch_new()
        assert len(findings) > 0, (
            f"Expected at least one GuardDuty finding in detector "
            f"'{_GUARDDUTY_DETECTOR_ID}', got 0"
        )

    def test_sample_finding_maps_to_ticket(self) -> None:
        """A known sample finding must map to a valid Ticket IR."""
        from ticketsync.adapters.guardduty import GuardDutyFindingsAdapter

        gd = _gd_client()
        adapter = GuardDutyFindingsAdapter(
            client=gd,
            detector_id=_GUARDDUTY_DETECTOR_ID,
            region=_REGION,
        )

        # Fetch the specific known sample finding
        response = gd.get_findings(
            DetectorId=_GUARDDUTY_DETECTOR_ID,
            FindingIds=[_GUARDDUTY_SAMPLE_FINDING_ID],
        )
        raw_findings: list[dict[str, Any]] = response.get("Findings", [])
        assert len(raw_findings) == 1, (
            f"Expected 1 finding for ID '{_GUARDDUTY_SAMPLE_FINDING_ID}', "
            f"got {len(raw_findings)}"
        )

        ticket = adapter.to_ticket(raw_findings[0])

        assert ticket.source_id == _GUARDDUTY_SAMPLE_FINDING_ID
        assert ticket.source_system == "guardduty"
        assert ticket.title, "Ticket title must not be empty"
        assert ticket.severity in ("critical", "high", "medium", "low"), (
            f"Unexpected severity: {ticket.severity!r}"
        )
        assert ticket.status in ("open", "closed"), (
            f"Unexpected status: {ticket.status!r}"
        )

    def test_severity_score_in_valid_range(self) -> None:
        """All fetched findings must produce valid severity values."""
        from ticketsync.adapters.guardduty import GuardDutyFindingsAdapter

        adapter = GuardDutyFindingsAdapter(
            client=_gd_client(),
            detector_id=_GUARDDUTY_DETECTOR_ID,
            region=_REGION,
        )
        findings = adapter.fetch_new()
        for raw in findings:
            ticket = adapter.to_ticket(raw)
            assert ticket.severity in ("critical", "high", "medium", "low", "informational"), (
                f"Invalid severity {ticket.severity!r} for finding {raw.get('Id')}"
            )


# ---------------------------------------------------------------------------
# Test: SecurityHubFindingsAdapter — read-only fetch + mapping
# ---------------------------------------------------------------------------


class TestSecurityHubFindingsFetch:
    """Verify SecurityHubFindingsAdapter can read findings from live AWS Security Hub.

    Note: GuardDuty findings propagate to Security Hub within ~15 minutes of
    initial activation.  If Security Hub was just enabled, this test may see
    0 findings and skip gracefully.
    """

    def test_fetch_new_returns_list(self) -> None:
        """get_findings must return a list (possibly empty during propagation window)."""
        from ticketsync.adapters.securityhub import SecurityHubFindingsAdapter

        adapter = SecurityHubFindingsAdapter(
            client=_sh_client(),
            region=_REGION,
        )
        findings = adapter.fetch_new()
        assert isinstance(findings, list), "fetch_new() must return a list"
        # We don't assert len > 0 because findings may not have propagated yet.

    def test_all_findings_map_to_valid_tickets(self) -> None:
        """Every finding returned must produce a valid Ticket IR without error."""
        from ticketsync.adapters.securityhub import SecurityHubFindingsAdapter

        adapter = SecurityHubFindingsAdapter(
            client=_sh_client(),
            region=_REGION,
        )
        findings = adapter.fetch_new()
        if not findings:
            pytest.skip(
                "No Security Hub findings available yet — GuardDuty findings "
                "propagate within ~15 minutes of enabling the integration."
            )

        for raw in findings:
            ticket = adapter.to_ticket(raw)
            assert ticket.title, f"Empty title for finding {raw.get('Id')}"
            assert ticket.severity in (
                "critical", "high", "medium", "low", "informational"
            ), f"Invalid severity for finding {raw.get('Id')}: {ticket.severity!r}"
            assert ticket.status in (
                "open", "in_progress", "resolved", "closed"
            ), f"Invalid status for finding {raw.get('Id')}: {ticket.status!r}"
            assert ticket.source_system == "securityhub"
