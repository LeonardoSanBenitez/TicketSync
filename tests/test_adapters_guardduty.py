"""Tests for GuardDutyFindingsAdapter.

All tests use stub boto3 clients — no real AWS credentials required.
Coverage targets:
- to_ticket(): severity mapping (all score bands), status from Archived,
  tags (region/count), entities (AccountEntity), timestamps, ARN, category
- from_ticket(): returns minimal dict with FindingId
- fetch_new(): list_findings pagination, get_findings batching, since filter
- write(): raises NotImplementedError
- Edge cases: missing fields, zero severity, empty response, large batch
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from ticketsync.adapters.guardduty import GuardDutyFindingsAdapter
from ticketsync.models import Ticket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DETECTOR_ID = "f0cec0ab8c944e358f7992966bdb3605"


def make_finding(
    finding_id: str = "abc123",
    title: str = "UnauthorizedAccess:IAMUser/ConsoleLoginSuccess.B",
    description: str = "An unusual console sign-in was detected.",
    severity: float = 5.0,
    archived: bool = False,
    account_id: str = "123456789012",
    region: str = "us-east-1",
    finding_type: str = "UnauthorizedAccess:IAMUser/ConsoleLoginSuccess.B",
    created_at: str = "2026-05-01T10:00:00Z",
    updated_at: str = "2026-05-01T11:00:00Z",
    arn: str = "arn:aws:guardduty:us-east-1:123456789012:detector/abc/finding/abc123",
    count: int = 3,
) -> dict[str, Any]:
    return {
        "Id": finding_id,
        "Title": title,
        "Description": description,
        "Severity": severity,
        "Type": finding_type,
        "CreatedAt": created_at,
        "UpdatedAt": updated_at,
        "Arn": arn,
        "AccountId": account_id,
        "Region": region,
        "Service": {
            "Archived": archived,
            "Count": count,
        },
    }


def make_stub_client(
    finding_ids: list[str] | None = None,
    findings: list[dict[str, Any]] | None = None,
    next_token: str = "",
) -> MagicMock:
    """Return a MagicMock GuardDuty client."""
    client = MagicMock()
    ids = finding_ids or []
    client.list_findings.return_value = {
        "FindingIds": ids,
        "NextToken": next_token,
    }
    client.get_findings.return_value = {
        "Findings": findings or [make_finding(finding_id=fid) for fid in ids],
    }
    return client


def make_adapter(
    client: MagicMock | None = None,
    detector_id: str = _DETECTOR_ID,
    batch_size: int = 50,
) -> GuardDutyFindingsAdapter:
    return GuardDutyFindingsAdapter(
        client=client or make_stub_client(),
        detector_id=detector_id,
        batch_size=batch_size,
    )


# ---------------------------------------------------------------------------
# to_ticket — severity mapping
# ---------------------------------------------------------------------------


class TestSeverityMapping:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (10.0, "critical"),
            (8.0, "critical"),
            (7.9, "high"),
            (5.0, "high"),
            (4.9, "medium"),
            (2.0, "medium"),
            (1.9, "low"),
            (1.0, "low"),
            (0.0, "low"),   # below minimum — still maps to low
        ],
    )
    def test_severity_bands(self, score: float, expected: str) -> None:
        adapter = make_adapter()
        raw = make_finding(severity=score)
        ticket = adapter.to_ticket(raw)
        assert ticket.severity == expected, (
            f"score={score} expected {expected!r} got {ticket.severity!r}"
        )


# ---------------------------------------------------------------------------
# to_ticket — status mapping
# ---------------------------------------------------------------------------


class TestStatusMapping:
    def test_active_finding_is_open(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket(make_finding(archived=False))
        assert ticket.status == "open"

    def test_archived_finding_is_closed(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket(make_finding(archived=True))
        assert ticket.status == "closed"


# ---------------------------------------------------------------------------
# to_ticket — field mapping
# ---------------------------------------------------------------------------


class TestFieldMapping:
    def test_source_id_is_finding_id(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket(make_finding(finding_id="myid-abc"))
        assert ticket.source_id == "myid-abc"

    def test_source_system(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket(make_finding())
        assert ticket.source_system == "guardduty"

    def test_custom_system_name(self) -> None:
        client = make_stub_client()
        adapter = GuardDutyFindingsAdapter(
            client=client,
            detector_id=_DETECTOR_ID,
            system_name="gd-staging",
        )
        ticket = adapter.to_ticket(make_finding())
        assert ticket.source_system == "gd-staging"

    def test_title_and_description(self) -> None:
        adapter = make_adapter()
        raw = make_finding(title="Alert: something bad", description="Details here")
        ticket = adapter.to_ticket(raw)
        assert ticket.title == "Alert: something bad"
        assert ticket.description == "Details here"

    def test_category_from_type(self) -> None:
        adapter = make_adapter()
        raw = make_finding(finding_type="Recon:EC2/Portscan")
        ticket = adapter.to_ticket(raw)
        assert ticket.category == "Recon:EC2/Portscan"

    def test_external_url_from_arn(self) -> None:
        adapter = make_adapter()
        raw = make_finding(arn="arn:aws:guardduty:us-east-1:123:detector/d/finding/f")
        ticket = adapter.to_ticket(raw)
        assert ticket.external_url == "arn:aws:guardduty:us-east-1:123:detector/d/finding/f"

    def test_timestamps_parsed_correctly(self) -> None:
        adapter = make_adapter()
        raw = make_finding(
            created_at="2026-01-15T08:00:00Z",
            updated_at="2026-01-16T12:30:00Z",
        )
        ticket = adapter.to_ticket(raw)
        assert ticket.created_at == datetime(2026, 1, 15, 8, 0, 0, tzinfo=timezone.utc)
        assert ticket.updated_at == datetime(2026, 1, 16, 12, 30, 0, tzinfo=timezone.utc)

    def test_raw_preserved(self) -> None:
        adapter = make_adapter()
        raw = make_finding(finding_id="preserve-me")
        ticket = adapter.to_ticket(raw)
        assert ticket.raw["Id"] == "preserve-me"

    def test_account_entity_added(self) -> None:
        adapter = make_adapter()
        raw = make_finding(account_id="999888777666")
        ticket = adapter.to_ticket(raw)
        assert len(ticket.entities) == 1
        entity = ticket.entities[0]
        assert entity.kind == "account"  # type: ignore[union-attr]
        assert entity.uid == "999888777666"  # type: ignore[union-attr]

    def test_no_account_entity_when_missing(self) -> None:
        adapter = make_adapter()
        raw = make_finding(account_id="")
        ticket = adapter.to_ticket(raw)
        assert ticket.entities == []


# ---------------------------------------------------------------------------
# to_ticket — tags
# ---------------------------------------------------------------------------


class TestTags:
    def test_region_tag(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket(make_finding(region="eu-west-1"))
        assert "region:eu-west-1" in ticket.tags

    def test_count_tag(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket(make_finding(count=7))
        assert "count:7" in ticket.tags

    def test_zero_count_not_in_tags(self) -> None:
        adapter = make_adapter()
        raw = make_finding(count=0)
        ticket = adapter.to_ticket(raw)
        assert not any(t.startswith("count:") for t in ticket.tags)

    def test_missing_region_no_tag(self) -> None:
        adapter = make_adapter()
        raw = make_finding(region="")
        ticket = adapter.to_ticket(raw)
        assert not any(t.startswith("region:") for t in ticket.tags)


# ---------------------------------------------------------------------------
# to_ticket — edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_missing_title_gets_default(self) -> None:
        adapter = make_adapter()
        raw: dict[str, Any] = {
            "Id": "x",
            "Severity": 3.0,
            "Service": {"Archived": False, "Count": 0},
        }
        ticket = adapter.to_ticket(raw)
        assert ticket.title == "GuardDuty Finding"

    def test_missing_service_block(self) -> None:
        adapter = make_adapter()
        raw: dict[str, Any] = {
            "Id": "x",
            "Title": "Some finding",
            "Severity": 5.0,
        }
        ticket = adapter.to_ticket(raw)
        assert ticket.status == "open"  # defaults to open when no Service block


# ---------------------------------------------------------------------------
# from_ticket
# ---------------------------------------------------------------------------


class TestFromTicket:
    def test_returns_finding_id(self) -> None:
        adapter = make_adapter()
        ticket = Ticket(
            source_system="guardduty",
            source_id="find-42",
            title="Test finding",
            severity="high",
        )
        result = adapter.from_ticket(ticket)
        assert result["FindingId"] == "find-42"
        assert result["Title"] == "Test finding"

    def test_description_included(self) -> None:
        adapter = make_adapter()
        ticket = Ticket(
            source_system="guardduty",
            source_id="x",
            title="T",
            description="Desc here",
            severity="low",
        )
        result = adapter.from_ticket(ticket)
        assert result["Description"] == "Desc here"


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_raises(self) -> None:
        adapter = make_adapter()
        ticket = Ticket(
            source_system="guardduty",
            source_id="x",
            title="T",
            severity="low",
        )
        with pytest.raises(NotImplementedError):
            adapter.write(ticket)


# ---------------------------------------------------------------------------
# fetch_new — list/get plumbing
# ---------------------------------------------------------------------------


class TestFetchNew:
    def test_empty_list_returns_empty(self) -> None:
        client = make_stub_client(finding_ids=[], findings=[])
        adapter = make_adapter(client=client)
        result = adapter.fetch_new()
        assert result == []

    def test_single_finding_fetched(self) -> None:
        finding = make_finding(finding_id="id-001")
        client = make_stub_client(finding_ids=["id-001"], findings=[finding])
        adapter = make_adapter(client=client)
        result = adapter.fetch_new()
        assert len(result) == 1
        assert result[0]["Id"] == "id-001"

    def test_list_findings_called_with_detector_id(self) -> None:
        client = make_stub_client()
        adapter = make_adapter(client=client)
        adapter.fetch_new()
        client.list_findings.assert_called_once()
        kwargs = client.list_findings.call_args.kwargs
        assert kwargs.get("DetectorId") == _DETECTOR_ID

    def test_get_findings_called_with_ids(self) -> None:
        ids = ["id-a", "id-b"]
        findings = [make_finding(finding_id=i) for i in ids]
        client = make_stub_client(finding_ids=ids, findings=findings)
        adapter = make_adapter(client=client)
        adapter.fetch_new()
        client.get_findings.assert_called_once()
        kwargs = client.get_findings.call_args.kwargs
        assert kwargs["DetectorId"] == _DETECTOR_ID
        assert set(kwargs["FindingIds"]) == set(ids)

    def test_batching_splits_large_id_lists(self) -> None:
        """When > batch_size IDs exist, get_findings is called multiple times."""
        ids = [f"id-{i:03d}" for i in range(75)]
        findings = [make_finding(finding_id=i) for i in ids]

        # list_findings returns all 75 ids in one shot (no pagination needed here)
        client = MagicMock()
        client.list_findings.return_value = {"FindingIds": ids, "NextToken": ""}
        # get_findings always returns a chunk (we don't care about content exactness)
        client.get_findings.side_effect = lambda **kw: {
            "Findings": [make_finding(finding_id=i) for i in kw["FindingIds"]]
        }

        adapter = make_adapter(client=client, batch_size=50)
        result = adapter.fetch_new()

        # Two batches: 50 + 25
        assert client.get_findings.call_count == 2
        assert len(result) == 75

    def test_pagination_on_list_findings(self) -> None:
        """list_findings NextToken is followed until exhausted."""
        client = MagicMock()
        client.list_findings.side_effect = [
            {"FindingIds": ["id-1", "id-2"], "NextToken": "page2"},
            {"FindingIds": ["id-3"], "NextToken": ""},
        ]
        client.get_findings.return_value = {
            "Findings": [
                make_finding(finding_id="id-1"),
                make_finding(finding_id="id-2"),
                make_finding(finding_id="id-3"),
            ]
        }

        adapter = make_adapter(client=client)
        result = adapter.fetch_new()

        assert client.list_findings.call_count == 2
        assert len(result) == 3

    def test_since_adds_finding_criteria(self) -> None:
        """fetch_new(since=...) passes FindingCriteria with updatedAt filter."""
        client = make_stub_client(finding_ids=[], findings=[])
        adapter = make_adapter(client=client)
        since = datetime(2026, 5, 1, tzinfo=timezone.utc)
        adapter.fetch_new(since=since)
        kwargs = client.list_findings.call_args.kwargs
        assert "FindingCriteria" in kwargs
        criterion = kwargs["FindingCriteria"]["Criterion"]
        assert "updatedAt" in criterion
        assert criterion["updatedAt"]["GreaterThan"] == int(since.timestamp() * 1000)

    def test_since_none_no_finding_criteria(self) -> None:
        """fetch_new() with no since does not add FindingCriteria."""
        client = make_stub_client(finding_ids=[], findings=[])
        adapter = make_adapter(client=client)
        adapter.fetch_new()
        kwargs = client.list_findings.call_args.kwargs
        assert "FindingCriteria" not in kwargs
