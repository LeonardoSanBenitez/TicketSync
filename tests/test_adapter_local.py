"""Tests for LocalFilesystemAdapter.

Coverage targets:
- write() creates file in correct location
- to_ticket() and from_ticket() round-trip fidelity
- fetch_new() with and without since cutoff
- fetch_new() with persisted cursor
- count() accuracy
- clear() removes files
- read_by_source_id() lookup
- all_tickets() enumeration
- Sanitized filenames (special chars in source_id)
- Large batch writes (100+ tickets)
- Concurrent-safe write (same ticket written twice => idempotent)
- Empty root directory
- Deeply nested source_system names
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from ticketsync.adapters.local import LocalFilesystemAdapter, _sanitize_filename
from ticketsync.models import Ticket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ticket(
    source_id: str = "T-001",
    source_system: str = "test",
    **kwargs: object,
) -> Ticket:
    defaults: dict[str, object] = {
        "source_system": source_system,
        "source_id": source_id,
        "title": f"Ticket {source_id}",
        "severity": "medium",
    }
    defaults.update(kwargs)
    return Ticket(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _sanitize_filename
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    def test_safe_string_unchanged(self) -> None:
        assert _sanitize_filename("abc-123") == "abc-123"

    def test_slashes_replaced(self) -> None:
        result = _sanitize_filename("a/b\\c")
        assert "/" not in result
        assert "\\" not in result

    def test_spaces_replaced(self) -> None:
        result = _sanitize_filename("hello world")
        assert " " not in result

    def test_colons_replaced(self) -> None:
        result = _sanitize_filename("oi-abc:123")
        assert ":" not in result

    def test_empty_string_becomes_empty_placeholder(self) -> None:
        result = _sanitize_filename("")
        assert result == "_empty_"

    def test_long_string_truncated(self) -> None:
        long_str = "x" * 300
        result = _sanitize_filename(long_str)
        assert len(result) == 200


# ---------------------------------------------------------------------------
# Basic write / read
# ---------------------------------------------------------------------------


class TestWriteAndRead:
    def test_write_creates_file(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        t = make_ticket()
        adapter.write(t)
        files = list(tmp_path.rglob("*.json"))
        assert len(files) == 1

    def test_write_returns_path_string(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        t = make_ticket()
        result = adapter.write(t)
        assert result.endswith(".json")
        assert Path(result).exists()

    def test_written_file_contains_valid_json(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        t = make_ticket()
        path_str = adapter.write(t)
        data = json.loads(Path(path_str).read_text(encoding="utf-8"))
        assert data["title"] == t.title

    def test_read_by_source_id(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        t = make_ticket(source_id="REF-99")
        adapter.write(t)
        retrieved = adapter.read_by_source_id("test", "REF-99")
        assert retrieved is not None
        assert retrieved.title == t.title

    def test_read_by_source_id_not_found(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        result = adapter.read_by_source_id("test", "nonexistent")
        assert result is None

    def test_overwrite_same_ticket_idempotent(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        t = make_ticket(source_id="DUP-1", title="Original title")
        adapter.write(t)
        t2 = make_ticket(source_id="DUP-1", title="Updated title")
        adapter.write(t2)
        retrieved = adapter.read_by_source_id("test", "DUP-1")
        assert retrieved is not None
        assert retrieved.title == "Updated title"
        assert adapter.count() == 1


# ---------------------------------------------------------------------------
# Filename sanitization in practice
# ---------------------------------------------------------------------------


class TestFilenameEdgeCases:
    def test_source_id_with_slashes(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        t = make_ticket(source_id="oi/abc/def")
        adapter.write(t)
        assert adapter.count() == 1

    def test_source_id_with_colons(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        t = make_ticket(source_id="GH:123:issue")
        adapter.write(t)
        assert adapter.count() == 1

    def test_source_system_with_spaces(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path, system_name="my system")
        t = make_ticket(source_system="my system", source_id="T-1")
        adapter.write(t)
        assert adapter.count() == 1

    def test_unicode_source_id(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        t = make_ticket(source_id="ticket_中文_ID")
        adapter.write(t)
        assert adapter.count() == 1


# ---------------------------------------------------------------------------
# count() / clear() / all_tickets()
# ---------------------------------------------------------------------------


class TestCountClearAllTickets:
    def test_count_empty_directory(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        assert adapter.count() == 0

    def test_count_after_writes(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        for i in range(5):
            adapter.write(make_ticket(source_id=f"T-{i}"))
        assert adapter.count() == 5

    def test_clear_removes_all(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        for i in range(3):
            adapter.write(make_ticket(source_id=f"T-{i}"))
        adapter.clear()
        assert adapter.count() == 0

    def test_all_tickets_empty(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        assert adapter.all_tickets() == []

    def test_all_tickets_returns_all(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        for i in range(4):
            adapter.write(make_ticket(source_id=f"T-{i}"))
        tickets = adapter.all_tickets()
        assert len(tickets) == 4
        assert all(isinstance(t, Ticket) for t in tickets)


# ---------------------------------------------------------------------------
# fetch_new()
# ---------------------------------------------------------------------------


class TestFetchNew:
    def test_fetch_new_no_since_returns_all(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        for i in range(3):
            adapter.write(make_ticket(source_id=f"T-{i}"))
        raws = adapter.fetch_new(since=None)
        assert len(raws) == 3

    def test_fetch_new_filters_by_since(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        new_time = datetime(2026, 5, 1, tzinfo=timezone.utc)
        cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)

        adapter.write(make_ticket(source_id="OLD", updated_at=old_time))
        adapter.write(make_ticket(source_id="NEW", updated_at=new_time))

        raws = adapter.fetch_new(since=cutoff)
        assert len(raws) == 1
        assert raws[0]["source_id"] == "NEW"

    def test_fetch_new_since_at_boundary_excluded(self, tmp_path: Path) -> None:
        """Tickets with updated_at == cutoff should NOT be returned."""
        adapter = LocalFilesystemAdapter(tmp_path)
        cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)
        adapter.write(make_ticket(source_id="EXACT", updated_at=cutoff))
        raws = adapter.fetch_new(since=cutoff)
        assert len(raws) == 0

    def test_fetch_new_empty_root(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        raws = adapter.fetch_new()
        assert raws == []

    def test_fetch_new_uses_cursor_if_no_since(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc)
        adapter.update_cursor(cutoff)

        adapter.write(
            make_ticket(source_id="BEFORE", updated_at=datetime(2026, 3, 1, tzinfo=timezone.utc))
        )
        adapter.write(
            make_ticket(source_id="AFTER", updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
        )

        raws = adapter.fetch_new()
        assert len(raws) == 1
        assert raws[0]["source_id"] == "AFTER"

    def test_fetch_new_explicit_since_overrides_cursor(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        # Cursor is April; explicit since is February
        cursor = datetime(2026, 4, 1, tzinfo=timezone.utc)
        explicit = datetime(2026, 2, 1, tzinfo=timezone.utc)
        adapter.update_cursor(cursor)

        adapter.write(
            make_ticket(source_id="MARCH", updated_at=datetime(2026, 3, 1, tzinfo=timezone.utc))
        )

        # With explicit since=February, March ticket should appear
        raws = adapter.fetch_new(since=explicit)
        assert len(raws) == 1


# ---------------------------------------------------------------------------
# Cursor persistence
# ---------------------------------------------------------------------------


class TestCursorPersistence:
    def test_cursor_persisted_and_loaded(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        ts = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
        adapter.update_cursor(ts)

        adapter2 = LocalFilesystemAdapter(tmp_path)
        loaded = adapter2._load_cursor()
        assert loaded is not None
        assert loaded == ts

    def test_cursor_cleared_by_clear(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        ts = datetime(2026, 5, 8, tzinfo=timezone.utc)
        adapter.update_cursor(ts)
        adapter.clear()
        assert adapter._load_cursor() is None


# ---------------------------------------------------------------------------
# Large batch
# ---------------------------------------------------------------------------


class TestLargeBatch:
    def test_write_100_tickets(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        for i in range(100):
            t = make_ticket(source_id=f"T-{i:04d}", severity="low")
            adapter.write(t)
        assert adapter.count() == 100

    def test_all_tickets_correct_count_after_100_writes(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        for i in range(100):
            adapter.write(make_ticket(source_id=f"X-{i}"))
        tickets = adapter.all_tickets()
        assert len(tickets) == 100

    def test_fetch_new_filters_correctly_on_large_batch(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(50):
            t = make_ticket(
                source_id=f"T-{i}",
                updated_at=base + timedelta(days=i),
            )
            adapter.write(t)
        # Cutoff at day 25 — tickets 25..49 are after, so 24 remain (> day 25)
        cutoff = base + timedelta(days=25)
        raws = adapter.fetch_new(since=cutoff)
        assert len(raws) == 24  # days 26..49


# ---------------------------------------------------------------------------
# to_ticket / from_ticket round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_to_ticket_from_ticket_preserves_all_fields(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        original = Ticket(
            source_system="test",
            source_id="RT-1",
            title="Round-trip test",
            description="A thorough test",
            severity="critical",
            status="in_progress",
            category="Performance",
            tags=["alpha", "beta"],
            assignee="bob@example.com",
            external_url="https://example.com/ticket/1",
            remediation_steps="Restart the service.",
            raw={"vendor_id": 42},
        )
        raw_dict = adapter.from_ticket(original)
        restored = adapter.to_ticket(raw_dict)

        assert restored.title == original.title
        assert restored.description == original.description
        assert restored.severity == original.severity
        assert restored.status == original.status
        assert restored.category == original.category
        assert restored.tags == original.tags
        assert restored.assignee == original.assignee
        assert restored.external_url == original.external_url
        assert restored.remediation_steps == original.remediation_steps
        assert restored.raw["vendor_id"] == 42

    def test_written_ticket_matches_read_back(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        original = make_ticket(source_id="RW-1", severity="high", tags=["x", "y"])
        adapter.write(original)
        retrieved = adapter.read_by_source_id("test", "RW-1")
        assert retrieved is not None
        assert retrieved.id == original.id
        assert retrieved.tags == original.tags
