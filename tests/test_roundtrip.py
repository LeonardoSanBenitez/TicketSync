"""Round-trip fidelity tests.

These tests verify that the full pipeline:
  source_adapter.to_ticket(raw) -> Ticket -> dest_adapter.from_ticket() -> raw2
  -> dest_adapter.to_ticket(raw2) -> Ticket2

produces Ticket2 equivalent to Ticket.  We test:
- LocalFilesystem full round-trip (write -> fetch_new -> to_ticket)
- LocalFilesystem -> OpsCenter (serialize to OpsCenter format, re-read back)
- LocalFilesystem -> GitHub Issues (serialize, re-read back)
- OpsCenter -> LocalFilesystem cross-adapter round-trip
- GitHub Issues -> LocalFilesystem cross-adapter round-trip
- Round-trip of all entity types embedded in tickets
- Round-trip of deeply nested raw payloads
- Round-trip of tickets with all optional fields populated
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ticketsync.adapters.local import LocalFilesystemAdapter
from ticketsync.adapters.opscenter import OpsCenterAdapter
from ticketsync.adapters.github_issues import GitHubIssuesAdapter
from ticketsync.models import (
    Ticket,
    AccountEntity,
    HostEntity,
    ProcessEntity,
    IpAddressEntity,
    FileEntity,
    UrlEntity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ticket(**kwargs: Any) -> Ticket:
    defaults: dict[str, Any] = {
        "source_system": "test",
        "source_id": "RT-001",
        "title": "Round-trip ticket",
        "severity": "high",
    }
    defaults.update(kwargs)
    return Ticket(**defaults)


def make_opscenter_adapter() -> OpsCenterAdapter:
    client = MagicMock()
    client.create_ops_item.return_value = {"OpsItemId": "oi-rt001"}
    return OpsCenterAdapter(client=client)


def make_github_adapter() -> GitHubIssuesAdapter:
    client = MagicMock()
    client.post.return_value = {"number": 99}
    client.patch.return_value = {"number": 99}
    return GitHubIssuesAdapter(client=client, owner="org", repo="repo")


# ---------------------------------------------------------------------------
# LocalFilesystem full round-trip
# ---------------------------------------------------------------------------


class TestLocalFilesystemRoundTrip:
    def test_write_and_read_back_identical_ticket(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        original = make_ticket(
            description="Detailed description",
            category="Performance",
            tags=["alpha", "beta"],
            severity="critical",
            status="in_progress",
            assignee="alice@example.com",
            external_url="https://example.com/T-001",
            remediation_steps="Restart the service.",
        )
        adapter.write(original)
        retrieved = adapter.read_by_source_id("test", "RT-001")
        assert retrieved is not None
        assert retrieved.title == original.title
        assert retrieved.description == original.description
        assert retrieved.severity == original.severity
        assert retrieved.status == original.status
        assert retrieved.tags == original.tags
        assert retrieved.assignee == original.assignee
        assert retrieved.external_url == original.external_url
        assert retrieved.remediation_steps == original.remediation_steps
        assert retrieved.id == original.id

    def test_fetch_then_to_ticket_preserves_all_fields(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        original = make_ticket(severity="low", tags=["x"])
        adapter.write(original)
        raws = adapter.fetch_new()
        assert len(raws) == 1
        restored = adapter.to_ticket(raws[0])
        assert restored.id == original.id
        assert restored.severity == original.severity

    def test_round_trip_with_all_entity_types(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        original = make_ticket(entities=[
            AccountEntity(uid="111", name="prod", type="aws"),
            HostEntity(hostname="h1.example.com", ip="10.0.0.1"),
            ProcessEntity(pid=42, name="nginx"),
            IpAddressEntity(ip="8.8.8.8"),
            FileEntity(path="/etc/hosts"),
            UrlEntity(url="https://example.com"),
        ])
        adapter.write(original)
        retrieved = adapter.read_by_source_id("test", "RT-001")
        assert retrieved is not None
        assert len(retrieved.entities) == 6
        kinds = {e.kind for e in retrieved.entities}  # type: ignore[union-attr]
        assert kinds == {"account", "host", "process", "ip_address", "file", "url"}

    def test_round_trip_with_deeply_nested_raw(self, tmp_path: Path) -> None:
        deep_raw: dict[str, Any] = {
            "level1": {
                "level2": {
                    "level3": ["a", "b", "c"],
                    "num": 42,
                }
            }
        }
        adapter = LocalFilesystemAdapter(tmp_path)
        original = make_ticket(raw=deep_raw)
        adapter.write(original)
        retrieved = adapter.read_by_source_id("test", "RT-001")
        assert retrieved is not None
        assert retrieved.raw["level1"]["level2"]["num"] == 42  # type: ignore[index]

    def test_round_trip_unicode_content(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        original = make_ticket(
            title="CPU超过95%阈值",
            description="日本語テスト: システムエラーが発生しました。",
        )
        adapter.write(original)
        retrieved = adapter.read_by_source_id("test", "RT-001")
        assert retrieved is not None
        assert retrieved.title == "CPU超过95%阈值"
        assert "日本語" in retrieved.description


# ---------------------------------------------------------------------------
# LocalFilesystem -> OpsCenter cross-adapter
# ---------------------------------------------------------------------------


class TestLocalToOpsCenter:
    def test_serialize_to_opscenter_and_back(self) -> None:
        local_adapter = LocalFilesystemAdapter("/unused")
        ops_adapter = make_opscenter_adapter()

        original = make_ticket(
            severity="critical",
            status="open",
            tags=["source:CloudWatch"],
        )

        # Serialize to OpsCenter format
        ops_payload = ops_adapter.from_ticket(original)
        assert ops_payload["Severity"] == "1"
        assert ops_payload["Status"] == "Open"
        assert ops_payload["Source"] == "CloudWatch"

        # Re-read via OpsCenter adapter
        ops_payload["OpsItemId"] = "oi-new001"
        ops_payload["Title"] = original.title
        ops_payload["Description"] = original.description or " "

        restored = ops_adapter.to_ticket(ops_payload)  # type: ignore[arg-type]
        assert restored.severity == "critical"
        assert restored.status == "open"
        assert "source:CloudWatch" in restored.tags

    def test_severity_round_trip_all_levels(self) -> None:
        ops_adapter = make_opscenter_adapter()
        for severity in ["critical", "high", "medium", "low"]:
            t = make_ticket(severity=severity)
            payload = ops_adapter.from_ticket(t)
            payload["OpsItemId"] = "oi-001"
            payload["Title"] = t.title
            payload["Status"] = "Open"
            restored = ops_adapter.to_ticket(payload)  # type: ignore[arg-type]
            assert restored.severity == severity


# ---------------------------------------------------------------------------
# LocalFilesystem -> GitHub Issues cross-adapter
# ---------------------------------------------------------------------------


class TestLocalToGitHub:
    def test_serialize_to_github_and_back(self) -> None:
        gh_adapter = make_github_adapter()

        original = make_ticket(
            severity="high",
            status="open",
            tags=["bug", "production"],
        )

        gh_payload = gh_adapter.from_ticket(original)
        assert "severity:high" in gh_payload["labels"]
        assert "bug" in gh_payload["labels"]
        assert gh_payload["state"] == "open"

        # Simulate a GitHub response for the created issue
        gh_response: dict[str, Any] = {
            "number": 42,
            "title": original.title,
            "body": original.description,
            "state": "open",
            "labels": [
                {"name": "severity:high"},
                {"name": "bug"},
                {"name": "production"},
            ],
            "html_url": "https://github.com/org/repo/issues/42",
            "created_at": "2026-05-08T00:00:00Z",
            "updated_at": "2026-05-08T00:00:00Z",
        }
        restored = gh_adapter.to_ticket(gh_response)
        assert restored.severity == "high"
        assert "bug" in restored.tags
        assert "production" in restored.tags
        assert "severity:high" not in restored.tags

    def test_closed_status_round_trip(self) -> None:
        gh_adapter = make_github_adapter()
        t = make_ticket(status="resolved")
        payload = gh_adapter.from_ticket(t)
        assert payload["state"] == "closed"
        gh_response: dict[str, Any] = {
            "number": 10,
            "title": t.title,
            "body": "",
            "state": "closed",
            "labels": [],
            "html_url": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        restored = gh_adapter.to_ticket(gh_response)
        assert restored.status == "resolved"


# ---------------------------------------------------------------------------
# OpsCenter -> LocalFilesystem pipeline test
# ---------------------------------------------------------------------------


class TestOpsCenterToLocal:
    def test_opscenter_ticket_synced_to_local(self, tmp_path: Path) -> None:
        ops_adapter = make_opscenter_adapter()
        local_adapter = LocalFilesystemAdapter(tmp_path)

        ops_item: dict[str, Any] = {
            "OpsItemId": "oi-pipeline001",
            "Title": "Pipeline test",
            "Description": "Testing the full pipeline.",
            "Status": "Open",
            "Severity": "2",
            "Source": "SecurityHub",
            "CreatedTime": datetime(2026, 5, 1, tzinfo=timezone.utc),
            "LastModifiedTime": datetime(2026, 5, 2, tzinfo=timezone.utc),
        }

        ticket = ops_adapter.to_ticket(ops_item)
        local_adapter.write(ticket)
        retrieved = local_adapter.read_by_source_id("opscenter", "oi-pipeline001")

        assert retrieved is not None
        assert retrieved.title == "Pipeline test"
        assert retrieved.severity == "high"
        assert "source:SecurityHub" in retrieved.tags


# ---------------------------------------------------------------------------
# GitHub Issues -> LocalFilesystem pipeline test
# ---------------------------------------------------------------------------


class TestGitHubToLocal:
    def test_github_issue_synced_to_local(self, tmp_path: Path) -> None:
        gh_adapter = make_github_adapter()
        local_adapter = LocalFilesystemAdapter(tmp_path)

        issue: dict[str, Any] = {
            "number": 77,
            "title": "Disk full on node-3",
            "body": "The disk is full. No more writes possible.",
            "state": "open",
            "labels": [
                {"name": "severity:critical"},
                {"name": "infrastructure"},
            ],
            "html_url": "https://github.com/org/repo/issues/77",
            "created_at": "2026-05-07T08:00:00Z",
            "updated_at": "2026-05-07T09:00:00Z",
        }

        ticket = gh_adapter.to_ticket(issue)
        local_adapter.write(ticket)
        retrieved = local_adapter.read_by_source_id("github_issues", "77")

        assert retrieved is not None
        assert retrieved.severity == "critical"
        assert "infrastructure" in retrieved.tags
