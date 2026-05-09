"""Tests for OpsCenterAdapter.

All tests use a stub client — no real AWS credentials required.
Coverage targets:
- to_ticket(): all severity mappings, all status mappings, source tag, timestamps
- from_ticket(): severity/status reverse mapping, source tag extraction
- fetch_new(): with and without since filter
- write(): create path vs update path (oi- prefix detection)
- Edge cases: unknown severity/status values, missing fields, empty Source
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ticketsync.adapters.opscenter import OpsCenterAdapter
from ticketsync.models import Ticket


# ---------------------------------------------------------------------------
# Stub client
# ---------------------------------------------------------------------------


def make_stub_client(
    describe_response: dict[str, Any] | None = None,
    create_response: dict[str, Any] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.describe_ops_items.return_value = describe_response or {"OpsItemSummaries": []}
    client.create_ops_item.return_value = create_response or {"OpsItemId": "oi-new001"}
    client.update_ops_item.return_value = {}
    return client


def load_fixture(name: str) -> dict[str, Any]:
    fixtures_dir = Path(__file__).parent / "fixtures"
    return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# to_ticket
# ---------------------------------------------------------------------------


class TestToTicket:
    def test_basic_mapping_from_fixture(self) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        raw = load_fixture("opscenter_opsitem.json")
        # Inject datetime objects (boto3 returns datetimes, not strings)
        raw["CreatedTime"] = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)
        raw["LastModifiedTime"] = datetime(2026, 5, 1, 8, 15, 0, tzinfo=timezone.utc)

        t = adapter.to_ticket(raw)
        assert t.source_id == "oi-abc123def456"
        assert t.title == "High CPU on prod-web-01"
        assert t.severity == "high"  # Severity "2" -> high
        assert t.status == "open"
        assert "source:CloudWatch" in t.tags
        assert t.source_system == "opscenter"

    @pytest.mark.parametrize(
        "severity_num,expected",
        [
            ("1", "critical"),
            ("2", "high"),
            ("3", "medium"),
            ("4", "low"),
            ("5", "informational"),   # out-of-range -> informational
            ("99", "informational"),
            ("", "informational"),
        ],
    )
    def test_severity_mapping(self, severity_num: str, expected: str) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        raw: dict[str, Any] = {
            "OpsItemId": "oi-001",
            "Title": "Test",
            "Description": "",
            "Status": "Open",
            "Severity": severity_num,
            "Source": "",
        }
        t = adapter.to_ticket(raw)
        assert t.severity == expected

    @pytest.mark.parametrize(
        "status_str,expected",
        [
            ("Open", "open"),
            ("InProgress", "in_progress"),
            ("Resolved", "resolved"),
            ("Unknown", "open"),  # fallback
        ],
    )
    def test_status_mapping(self, status_str: str, expected: str) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        raw: dict[str, Any] = {
            "OpsItemId": "oi-001",
            "Title": "Test",
            "Status": status_str,
            "Severity": "3",
            "Source": "",
        }
        t = adapter.to_ticket(raw)
        assert t.status == expected

    def test_source_tag_added(self) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        raw: dict[str, Any] = {
            "OpsItemId": "oi-001",
            "Title": "T",
            "Status": "Open",
            "Severity": "3",
            "Source": "Security Hub",
        }
        t = adapter.to_ticket(raw)
        assert "source:Security Hub" in t.tags

    def test_empty_source_no_tag(self) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        raw: dict[str, Any] = {
            "OpsItemId": "oi-001",
            "Title": "T",
            "Status": "Open",
            "Severity": "3",
            "Source": "",
        }
        t = adapter.to_ticket(raw)
        source_tags = [tag for tag in t.tags if tag.startswith("source:")]
        assert len(source_tags) == 0

    def test_raw_preserved(self) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        raw: dict[str, Any] = {
            "OpsItemId": "oi-001",
            "Title": "T",
            "Status": "Open",
            "Severity": "3",
            "Source": "",
            "OperationalData": {"key": "value"},
        }
        t = adapter.to_ticket(raw)
        assert t.raw.get("OperationalData") == {"key": "value"}

    def test_missing_ops_item_id_becomes_empty_source_id(self) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        raw: dict[str, Any] = {"Title": "T", "Status": "Open", "Severity": "3", "Source": ""}
        t = adapter.to_ticket(raw)
        assert t.source_id == ""

    def test_custom_system_name(self) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client(), system_name="prod-opscenter")
        raw: dict[str, Any] = {
            "OpsItemId": "oi-001",
            "Title": "T",
            "Status": "Open",
            "Severity": "3",
            "Source": "",
        }
        t = adapter.to_ticket(raw)
        assert t.source_system == "prod-opscenter"


# ---------------------------------------------------------------------------
# from_ticket
# ---------------------------------------------------------------------------


class TestFromTicket:
    def make_ticket(self, **kwargs: Any) -> Ticket:
        defaults: dict[str, Any] = {
            "source_system": "opscenter",
            "source_id": "oi-001",
            "title": "Test ticket",
            "severity": "medium",
        }
        defaults.update(kwargs)
        return Ticket(**defaults)

    def test_basic_reverse_mapping(self) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        t = self.make_ticket()
        payload = adapter.from_ticket(t)
        assert payload["Title"] == "Test ticket"
        assert payload["Severity"] == "3"  # medium -> 3
        assert payload["Status"] == "Open"

    @pytest.mark.parametrize(
        "severity,expected_num",
        [
            ("critical", "1"),
            ("high", "2"),
            ("medium", "3"),
            ("low", "4"),
            ("informational", "4"),  # fallback
        ],
    )
    def test_severity_reverse_mapping(self, severity: str, expected_num: str) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        t = self.make_ticket(severity=severity)
        payload = adapter.from_ticket(t)
        assert payload["Severity"] == expected_num

    @pytest.mark.parametrize(
        "status,expected_ops_status",
        [
            ("open", "Open"),
            ("in_progress", "InProgress"),
            ("resolved", "Resolved"),
            ("closed", "Resolved"),  # OpsCenter has no "closed"
        ],
    )
    def test_status_reverse_mapping(self, status: str, expected_ops_status: str) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        t = self.make_ticket(status=status)
        payload = adapter.from_ticket(t)
        assert payload["Status"] == expected_ops_status

    def test_source_tag_extracted(self) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        t = self.make_ticket(tags=["source:Security Hub", "other-tag"])
        payload = adapter.from_ticket(t)
        assert payload["Source"] == "Security Hub"

    def test_no_source_tag_uses_default(self) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        t = self.make_ticket(tags=[])
        payload = adapter.from_ticket(t)
        assert payload["Source"] == "ticketsync"

    def test_empty_description_gets_placeholder(self) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        t = self.make_ticket(description="")
        payload = adapter.from_ticket(t)
        assert payload["Description"] != ""  # OpsCenter rejects empty


# ---------------------------------------------------------------------------
# fetch_new
# ---------------------------------------------------------------------------


class TestFetchNew:
    def test_fetch_new_returns_all_when_no_since(self) -> None:
        items = [
            {"OpsItemId": "oi-001", "Title": "T1", "Status": "Open", "Severity": "3"},
            {"OpsItemId": "oi-002", "Title": "T2", "Status": "Open", "Severity": "2"},
        ]
        client = make_stub_client(describe_response={"OpsItemSummaries": items})
        adapter = OpsCenterAdapter(client=client)
        results = adapter.fetch_new()
        assert len(results) == 2

    def test_fetch_new_filters_by_since(self) -> None:
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        items = [
            {
                "OpsItemId": "oi-001",
                "Title": "Old",
                "Status": "Open",
                "Severity": "3",
                "LastModifiedTime": datetime(2026, 2, 1, tzinfo=timezone.utc),
            },
            {
                "OpsItemId": "oi-002",
                "Title": "New",
                "Status": "Open",
                "Severity": "3",
                "LastModifiedTime": datetime(2026, 4, 1, tzinfo=timezone.utc),
            },
        ]
        client = make_stub_client(describe_response={"OpsItemSummaries": items})
        adapter = OpsCenterAdapter(client=client)
        results = adapter.fetch_new(since=since)
        assert len(results) == 1
        assert results[0]["Title"] == "New"

    def test_fetch_new_empty_response(self) -> None:
        client = make_stub_client(describe_response={"OpsItemSummaries": []})
        adapter = OpsCenterAdapter(client=client)
        assert adapter.fetch_new() == []


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    def make_ticket(self, source_id: str = "oi-existing") -> Ticket:
        return Ticket(
            source_system="opscenter",
            source_id=source_id,
            title="Test",
            severity="medium",
        )

    def test_write_update_when_source_id_starts_with_oi(self) -> None:
        client = make_stub_client()
        adapter = OpsCenterAdapter(client=client)
        t = self.make_ticket(source_id="oi-existing123")
        adapter.write(t)
        client.update_ops_item.assert_called_once()
        client.create_ops_item.assert_not_called()

    def test_write_create_when_source_id_is_empty(self) -> None:
        client = make_stub_client(create_response={"OpsItemId": "oi-new001"})
        adapter = OpsCenterAdapter(client=client)
        t = self.make_ticket(source_id="")
        result = adapter.write(t)
        client.create_ops_item.assert_called_once()
        assert result == "oi-new001"

    def test_write_create_when_source_id_is_non_oi(self) -> None:
        client = make_stub_client(create_response={"OpsItemId": "oi-new002"})
        adapter = OpsCenterAdapter(client=client)
        t = self.make_ticket(source_id="GH-123")
        result = adapter.write(t)
        client.create_ops_item.assert_called_once()
        assert result == "oi-new002"

    def test_write_update_returns_source_id(self) -> None:
        client = make_stub_client()
        adapter = OpsCenterAdapter(client=client)
        t = self.make_ticket(source_id="oi-existing999")
        result = adapter.write(t)
        assert result == "oi-existing999"

    def test_write_includes_operational_data(self) -> None:
        client = make_stub_client(create_response={"OpsItemId": "oi-new001"})
        adapter = OpsCenterAdapter(client=client)
        t = self.make_ticket(source_id="")
        adapter.write(t)
        call_kwargs = client.create_ops_item.call_args[1]
        assert "OperationalData" in call_kwargs

    def test_write_assignee_in_operational_data(self) -> None:
        client = make_stub_client(create_response={"OpsItemId": "oi-new002"})
        adapter = OpsCenterAdapter(client=client)
        t = Ticket(
            source_system="opscenter",
            source_id="",
            title="T",
            severity="high",
            assignee="alice@example.com",
        )
        adapter.write(t)
        call_kwargs = client.create_ops_item.call_args[1]
        od = call_kwargs["OperationalData"]
        assert "/ticketsync/assignee" in od
        assert od["/ticketsync/assignee"]["Value"] == "alice@example.com"

    def test_write_no_assignee_no_key(self) -> None:
        client = make_stub_client(create_response={"OpsItemId": "oi-new003"})
        adapter = OpsCenterAdapter(client=client)
        t = self.make_ticket(source_id="")
        adapter.write(t)
        call_kwargs = client.create_ops_item.call_args[1]
        od = call_kwargs["OperationalData"]
        assert "/ticketsync/assignee" not in od


# ---------------------------------------------------------------------------
# Assignee round-trip
# ---------------------------------------------------------------------------


class TestAssigneeRoundTrip:
    def test_assignee_roundtrip_via_operational_data(self) -> None:
        """to_ticket reads assignee from OperationalData key."""
        adapter = OpsCenterAdapter(client=make_stub_client())
        raw: dict[str, Any] = {
            "OpsItemId": "oi-001",
            "Title": "T",
            "Status": "Open",
            "Severity": "3",
            "Source": "",
            "OperationalData": {
                "/ticketsync/assignee": {"Value": "bob@example.com", "Type": "SearchableString"},
            },
        }
        t = adapter.to_ticket(raw)
        assert t.assignee == "bob@example.com"

    def test_no_assignee_key_gives_empty_string(self) -> None:
        adapter = OpsCenterAdapter(client=make_stub_client())
        raw: dict[str, Any] = {
            "OpsItemId": "oi-001",
            "Title": "T",
            "Status": "Open",
            "Severity": "3",
            "Source": "",
        }
        t = adapter.to_ticket(raw)
        assert t.assignee == ""


# ---------------------------------------------------------------------------
# Tag-based sync and destination-check
# ---------------------------------------------------------------------------


class TestTagBasedSync:
    def test_mark_synced_calls_update(self) -> None:
        client = make_stub_client()
        adapter = OpsCenterAdapter(client=client)
        adapter.mark_synced("oi-abc123")
        client.update_ops_item.assert_called_once()
        call_kwargs = client.update_ops_item.call_args[1]
        assert "/ticketsync/synced" in call_kwargs["OperationalData"]

    def test_find_by_source_coordinates_returns_id(self) -> None:
        summary = {"OpsItemId": "oi-found001"}
        client = make_stub_client(describe_response={"OpsItemSummaries": [summary]})
        adapter = OpsCenterAdapter(client=client)
        result = adapter.find_by_source_coordinates("guardduty", "gd-123abc")
        assert result == "oi-found001"

    def test_find_by_source_coordinates_returns_none_when_not_found(self) -> None:
        client = make_stub_client(describe_response={"OpsItemSummaries": []})
        adapter = OpsCenterAdapter(client=client)
        result = adapter.find_by_source_coordinates("guardduty", "gd-999")
        assert result is None


# ---------------------------------------------------------------------------
# Triage metadata
# ---------------------------------------------------------------------------


class TestTriageMetadata:
    def test_write_metadata_calls_update_ops_item(self) -> None:
        from ticketsync.metadata import TriageMetadata

        client = make_stub_client()
        adapter = OpsCenterAdapter(client=client)
        meta = TriageMetadata(
            assignee="alice@example.com",
            priority=2,
            triage_notes="Escalate to IR.",
        )
        adapter.write_metadata("oi-abc123", meta)
        client.update_ops_item.assert_called_once()
        call_kwargs = client.update_ops_item.call_args[1]
        od = call_kwargs["OperationalData"]
        assert "/ticketsync/triage/assignee" in od
        assert "/ticketsync/triage/priority" in od
        assert od["/ticketsync/triage/assignee"]["Value"] == "alice@example.com"

    def test_write_metadata_omits_empty_fields(self) -> None:
        from ticketsync.metadata import TriageMetadata

        client = make_stub_client()
        adapter = OpsCenterAdapter(client=client)
        meta = TriageMetadata()  # all defaults
        adapter.write_metadata("oi-abc123", meta)
        # No fields set → update_ops_item not called (nothing to write)
        client.update_ops_item.assert_not_called()

    def test_read_metadata_returns_none_when_no_triage_data(self) -> None:
        client = MagicMock()
        client.get_ops_item.return_value = {
            "OpsItem": {
                "OpsItemId": "oi-001",
                "OperationalData": {},
            }
        }
        adapter = OpsCenterAdapter(client=client)
        result = adapter.read_metadata("oi-001")
        assert result is None

    def test_read_metadata_returns_triage_data(self) -> None:
        from ticketsync.metadata import TriageMetadata

        client = MagicMock()
        client.get_ops_item.return_value = {
            "OpsItem": {
                "OpsItemId": "oi-001",
                "OperationalData": {
                    "/ticketsync/triage/assignee": {
                        "Value": "bob@example.com",
                        "Type": "SearchableString",
                    },
                    "/ticketsync/triage/priority": {
                        "Value": "1",
                        "Type": "SearchableString",
                    },
                    "/ticketsync/triage/notes": {
                        "Value": "Confirmed.",
                        "Type": "SearchableString",
                    },
                },
            }
        }
        adapter = OpsCenterAdapter(client=client)
        result = adapter.read_metadata("oi-001")
        assert isinstance(result, TriageMetadata)
        assert result.assignee == "bob@example.com"
        assert result.priority == 1
        assert result.triage_notes == "Confirmed."

    def test_is_metadata_adapter(self) -> None:
        from ticketsync.metadata import MetadataAdapter

        adapter = OpsCenterAdapter(client=make_stub_client())
        assert isinstance(adapter, MetadataAdapter)
