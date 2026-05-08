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
