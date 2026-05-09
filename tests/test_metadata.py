"""Tests for the TriageMetadata model and MetadataAdapter protocol.

Coverage targets:
- TriageMetadata: field defaults, priority bounds, with_timestamp, to_human_readable
- MetadataAdapter protocol: isinstance check on implementing adapters
- write_metadata / read_metadata convenience helpers
- Round-trip: write then read recovers the same values
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from ticketsync.metadata import (
    MetadataAdapter,
    TriageMetadata,
    read_metadata,
    write_metadata,
)


# ---------------------------------------------------------------------------
# TriageMetadata model
# ---------------------------------------------------------------------------


class TestTriageMetadataModel:
    def test_default_fields(self) -> None:
        meta = TriageMetadata()
        assert meta.assignee == ""
        assert meta.priority is None
        assert meta.triage_notes == ""
        assert meta.severity_override == ""
        assert meta.resolution == ""
        assert meta.triaged_at is None

    def test_priority_bounds(self) -> None:
        meta = TriageMetadata(priority=1)
        assert meta.priority == 1
        meta2 = TriageMetadata(priority=5)
        assert meta2.priority == 5

    def test_priority_out_of_bounds_raises(self) -> None:
        with pytest.raises(Exception):
            TriageMetadata(priority=0)
        with pytest.raises(Exception):
            TriageMetadata(priority=6)

    def test_with_timestamp_sets_triaged_at(self) -> None:
        meta = TriageMetadata()
        assert meta.triaged_at is None
        stamped = meta.with_timestamp()
        assert stamped.triaged_at is not None
        assert stamped.triaged_at.tzinfo is not None

    def test_with_timestamp_noop_when_already_set(self) -> None:
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        meta = TriageMetadata(triaged_at=dt)
        stamped = meta.with_timestamp()
        assert stamped.triaged_at == dt

    def test_to_human_readable_contains_assignee(self) -> None:
        meta = TriageMetadata(assignee="alice@example.com")
        text = meta.to_human_readable()
        assert "alice@example.com" in text
        assert "TicketSync triage metadata" in text

    def test_to_human_readable_contains_priority(self) -> None:
        meta = TriageMetadata(priority=2)
        text = meta.to_human_readable()
        assert "Priority: 2" in text

    def test_to_human_readable_omits_empty_fields(self) -> None:
        meta = TriageMetadata()
        text = meta.to_human_readable()
        # Only the marker comment should appear; no field lines
        lines = [l for l in text.splitlines() if not l.startswith("<!--")]
        assert len(lines) == 0

    def test_to_human_readable_all_fields(self) -> None:
        meta = TriageMetadata(
            assignee="bob",
            priority=3,
            triage_notes="Needs IR review",
            severity_override="critical",
            resolution="Contained",
            triaged_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )
        text = meta.to_human_readable()
        assert "bob" in text
        assert "3" in text
        assert "Needs IR review" in text
        assert "critical" in text
        assert "Contained" in text
        assert "2026-05-09" in text


# ---------------------------------------------------------------------------
# MetadataAdapter protocol check
# ---------------------------------------------------------------------------


class TestMetadataAdapterProtocol:
    def test_class_without_metadata_methods_is_not_metadata_adapter(self) -> None:
        class NoMeta:
            pass
        assert not isinstance(NoMeta(), MetadataAdapter)

    def test_class_with_metadata_methods_is_metadata_adapter(self) -> None:
        class YesMeta:
            def write_metadata(self, source_id: str, metadata: TriageMetadata) -> None:
                pass
            def read_metadata(self, source_id: str) -> TriageMetadata | None:
                return None
        assert isinstance(YesMeta(), MetadataAdapter)

    def test_class_with_only_write_is_not_metadata_adapter(self) -> None:
        class HalfMeta:
            def write_metadata(self, source_id: str, metadata: TriageMetadata) -> None:
                pass
        assert not isinstance(HalfMeta(), MetadataAdapter)

    def test_opscenter_adapter_is_metadata_adapter(self) -> None:
        from ticketsync.adapters.opscenter import OpsCenterAdapter
        adapter = OpsCenterAdapter(client=MagicMock())
        assert isinstance(adapter, MetadataAdapter)

    def test_github_adapter_is_metadata_adapter(self) -> None:
        from ticketsync.adapters.github_issues import GitHubIssuesAdapter
        adapter = GitHubIssuesAdapter(
            client=MagicMock(), owner="org", repo="repo"
        )
        assert isinstance(adapter, MetadataAdapter)

    def test_local_adapter_is_not_metadata_adapter(self) -> None:
        from pathlib import Path
        from ticketsync.adapters.local import LocalFilesystemAdapter
        adapter = LocalFilesystemAdapter(path=Path("/tmp"))
        assert not isinstance(adapter, MetadataAdapter)

    def test_cloudwatch_adapter_is_not_metadata_adapter(self) -> None:
        from ticketsync.adapters.cloudwatch_alarms import CloudWatchAlarmsAdapter
        adapter = CloudWatchAlarmsAdapter(client=MagicMock())
        assert not isinstance(adapter, MetadataAdapter)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


class TestConvenienceHelpers:
    def test_write_metadata_returns_true_for_metadata_adapter(self) -> None:
        class StubAdapter:
            def write_metadata(self, source_id: str, metadata: TriageMetadata) -> None:
                pass
            def read_metadata(self, source_id: str) -> TriageMetadata | None:
                return None
        adapter = StubAdapter()
        result = write_metadata(adapter, "42", TriageMetadata(assignee="alice"))
        assert result is True

    def test_write_metadata_returns_false_for_non_metadata_adapter(self) -> None:
        class Plain:
            pass
        result = write_metadata(Plain(), "42", TriageMetadata())
        assert result is False

    def test_read_metadata_returns_none_for_non_metadata_adapter(self) -> None:
        class Plain:
            pass
        result = read_metadata(Plain(), "42")
        assert result is None

    def test_read_metadata_calls_adapter(self) -> None:
        expected = TriageMetadata(assignee="carol", priority=1)

        class StubAdapter:
            def write_metadata(self, source_id: str, metadata: TriageMetadata) -> None:
                pass
            def read_metadata(self, source_id: str) -> TriageMetadata | None:
                return expected

        result = read_metadata(StubAdapter(), "99")
        assert result is expected


# ---------------------------------------------------------------------------
# GitHub adapter round-trip via structured comment
# ---------------------------------------------------------------------------


class TestGitHubMetadataRoundTrip:
    def _make_adapter(self, comments: list[dict[str, Any]]) -> Any:
        from ticketsync.adapters.github_issues import GitHubIssuesAdapter
        from unittest.mock import MagicMock

        client = MagicMock()
        client.get.return_value = comments
        client.post.return_value = {"id": 1}
        return GitHubIssuesAdapter(client=client, owner="org", repo="repo")

    def test_write_then_read_recovers_fields(self) -> None:
        from ticketsync.adapters.github_issues import GitHubIssuesAdapter

        # Capture the posted comment body
        posted_bodies: list[str] = []

        def mock_post(url: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
            if json and "body" in json:
                posted_bodies.append(json["body"])
            return {"id": 1}

        def mock_get(url: str, params: Any = None) -> Any:
            # Return comments with the posted body
            if posted_bodies:
                return [{"body": posted_bodies[-1]}]
            return []

        client = MagicMock()
        client.post.side_effect = mock_post
        client.get.side_effect = mock_get

        adapter = GitHubIssuesAdapter(client=client, owner="org", repo="repo")

        meta = TriageMetadata(
            assignee="dave@example.com",
            priority=2,
            triage_notes="Escalated to IR team.",
            severity_override="critical",
            resolution="Remediated via patch.",
        )
        adapter.write_metadata("42", meta)
        recovered = adapter.read_metadata("42")

        assert recovered is not None
        assert recovered.assignee == "dave@example.com"
        assert recovered.priority == 2
        assert recovered.triage_notes == "Escalated to IR team."
        assert recovered.severity_override == "critical"
        assert recovered.resolution == "Remediated via patch."
