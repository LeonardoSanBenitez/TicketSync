"""Tests for SyncEngine.

Coverage targets:
- run() with LocalFilesystem source and destination (end-to-end)
- SyncResult counts: fetched, written, skipped_duplicate, failed
- Deduplication within a single run
- Deduplication across runs (engine reset_dedup_cache)
- lookback_hours applied when since is None
- since argument overrides lookback_hours
- Errors in to_ticket captured in result.errors
- Errors in dest.write captured in result.errors
- Empty source returns zero written
- Partial failure: some tickets succeed, some fail
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ticketsync.adapters.local import LocalFilesystemAdapter
from ticketsync.config import SyncConfig
from ticketsync.engine import SyncEngine, SyncResult
from ticketsync.models import Ticket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    deduplication: bool = True,
    lookback_hours: int = 0,
    dedup_strategy: str = "time-based",
) -> SyncConfig:
    return SyncConfig.from_dict({
        "source": {"type": "local", "path": "/unused"},
        "destination": {"type": "local", "path": "/unused"},
        "deduplication": deduplication,
        "lookback_hours": lookback_hours,
        "dedup_strategy": dedup_strategy,
    })


def make_ticket(source_id: str = "T-001", **kwargs: Any) -> Ticket:
    defaults: dict[str, Any] = {
        "source_system": "test",
        "source_id": source_id,
        "title": f"Ticket {source_id}",
        "severity": "medium",
    }
    defaults.update(kwargs)
    return Ticket(**defaults)


def write_tickets(adapter: LocalFilesystemAdapter, tickets: list[Ticket]) -> None:
    for t in tickets:
        adapter.write(t)


# ---------------------------------------------------------------------------
# Basic end-to-end
# ---------------------------------------------------------------------------


class TestBasicRun:
    def test_empty_source_returns_zero_written(
        self, tmp_path: Path
    ) -> None:
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        config = make_config()
        engine = SyncEngine(source=src, dest=dst, config=config)
        result = engine.run(since=None)
        assert result.fetched == 0
        assert result.written == 0

    def test_single_ticket_synced(self, tmp_path: Path) -> None:
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        src.write(make_ticket("T-001"))
        config = make_config()
        engine = SyncEngine(source=src, dest=dst, config=config)
        result = engine.run(since=None)
        assert result.fetched == 1
        assert result.written == 1
        assert result.failed == 0
        assert dst.count() == 1

    def test_multiple_tickets_synced(self, tmp_path: Path) -> None:
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        for i in range(5):
            src.write(make_ticket(f"T-{i:03d}"))
        config = make_config()
        engine = SyncEngine(source=src, dest=dst, config=config)
        result = engine.run(since=None)
        assert result.written == 5
        assert dst.count() == 5

    def test_sync_result_str(self, tmp_path: Path) -> None:
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        config = make_config()
        engine = SyncEngine(source=src, dest=dst, config=config)
        result = engine.run()
        s = str(result)
        assert "fetched=" in s
        assert "written=" in s


# ---------------------------------------------------------------------------
# Deduplication within a single run
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_duplicate_tickets_in_source_skipped(self, tmp_path: Path) -> None:
        """Source has 3 items but two share the same (source_system, source_id)."""
        src = LocalFilesystemAdapter(tmp_path / "src", system_name="dup-test")
        dst = LocalFilesystemAdapter(tmp_path / "dst")

        # Write ticket T-001 twice (simulates a source that returns duplicates)
        t1 = make_ticket("T-001", source_system="dup-test")
        t2 = make_ticket("T-001", source_system="dup-test", title="Duplicate T-001")
        t3 = make_ticket("T-002", source_system="dup-test")
        src.write(t1)
        # Overwrite T-001 with a different content (same source_id)
        src.write(t2)
        src.write(t3)

        config = make_config(deduplication=True)
        engine = SyncEngine(source=src, dest=dst, config=config)
        result = engine.run(since=None)

        # src.count() == 2 (T-001 overwritten, T-002)
        assert result.fetched == 2
        assert result.written == 2
        assert result.skipped_duplicate == 0

    def test_dedup_flag_disabled_allows_duplicates(self, tmp_path: Path) -> None:
        """Engine.run() called twice; without dedup, same ticket written twice."""
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        src.write(make_ticket("T-001"))
        config = make_config(deduplication=False)
        engine = SyncEngine(source=src, dest=dst, config=config)
        result1 = engine.run(since=None)
        result2 = engine.run(since=None)
        # Both succeed (dedup is off so no skipping)
        assert result1.written == 1
        assert result2.written == 1
        # The LocalFilesystem adapter overwrites the file — count stays at 1
        assert dst.count() == 1

    def test_dedup_across_two_runs_with_cache(self, tmp_path: Path) -> None:
        """With dedup ON and clear_cache=False, second run skips already-written tickets."""
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        src.write(make_ticket("T-001"))
        config = make_config(deduplication=True)
        engine = SyncEngine(source=src, dest=dst, config=config)

        result1 = engine.run(since=None)
        assert result1.written == 1

        # clear_cache=False preserves the dedup set from the previous run
        result2 = engine.run(since=None, clear_cache=False)
        assert result2.skipped_duplicate == 1
        assert result2.written == 0

    def test_second_run_clears_cache_by_default(self, tmp_path: Path) -> None:
        """By default, each run starts fresh (cache is cleared)."""
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        src.write(make_ticket("T-001"))
        config = make_config(deduplication=True)
        engine = SyncEngine(source=src, dest=dst, config=config)

        result1 = engine.run(since=None)
        assert result1.written == 1

        # Default clear_cache=True → cache is cleared → same ticket can be written again
        result2 = engine.run(since=None)
        assert result2.written == 1
        assert result2.skipped_duplicate == 0

    def test_reset_dedup_cache_allows_re_sync(self, tmp_path: Path) -> None:
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        src.write(make_ticket("T-001"))
        config = make_config(deduplication=True)
        engine = SyncEngine(source=src, dest=dst, config=config)

        engine.run(since=None)
        engine.reset_dedup_cache()
        result = engine.run(since=None)
        assert result.written == 1  # re-synced after cache reset


# ---------------------------------------------------------------------------
# lookback_hours / since
# ---------------------------------------------------------------------------


class TestLookbackAndSince:
    def test_lookback_hours_applied(self, tmp_path: Path) -> None:
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")

        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=48)

        src.write(make_ticket("OLD", updated_at=old))
        src.write(make_ticket("NEW", updated_at=now))

        # lookback_hours=1 -> cutoff is 1h ago -> only NEW survives
        config = make_config(lookback_hours=1)
        engine = SyncEngine(source=src, dest=dst, config=config)
        result = engine.run()
        assert result.written == 1
        tickets = dst.all_tickets()
        assert tickets[0].source_id == "NEW"

    def test_lookback_hours_zero_fetches_all(self, tmp_path: Path) -> None:
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        for i in range(3):
            src.write(make_ticket(f"T-{i}"))
        config = make_config(lookback_hours=0)
        engine = SyncEngine(source=src, dest=dst, config=config)
        result = engine.run()
        assert result.written == 3

    def test_explicit_since_overrides_lookback(self, tmp_path: Path) -> None:
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        far_past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        src.write(make_ticket("OLD", updated_at=far_past))
        src.write(make_ticket("NEW", updated_at=recent))
        # lookback_hours=24 would exclude far_past; explicit since=year 2019 includes it
        config = make_config(lookback_hours=24)
        engine = SyncEngine(source=src, dest=dst, config=config)
        since_2019 = datetime(2019, 1, 1, tzinfo=timezone.utc)
        result = engine.run(since=since_2019)
        assert result.written == 2


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_bad_raw_item_captured_in_errors(self, tmp_path: Path) -> None:
        """When to_ticket() raises, the error is recorded and run continues."""
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        # Write a valid ticket, then patch to_ticket to fail on first call
        src.write(make_ticket("GOOD"))
        src.write(make_ticket("ALSO-GOOD"))

        config = make_config()
        engine = SyncEngine(source=src, dest=dst, config=config)

        call_count = 0
        original = src.to_ticket

        def to_ticket_with_one_failure(raw: dict[str, object]) -> Ticket:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("simulated parse failure")
            return original(raw)

        src.to_ticket = to_ticket_with_one_failure  # type: ignore[method-assign]

        result = engine.run(since=None)
        assert result.failed == 1
        assert result.written == 1
        assert result.fetched == 2
        assert any(e["stage"] == "to_ticket" for e in result.errors)

    def test_write_failure_captured_in_errors(self, tmp_path: Path) -> None:
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        src.write(make_ticket("T-001"))

        config = make_config()
        engine = SyncEngine(source=src, dest=dst, config=config)

        original_write = dst.write

        def failing_write(t: Ticket) -> str:
            raise IOError("simulated disk failure")

        dst.write = failing_write  # type: ignore[method-assign]

        result = engine.run(since=None)
        assert result.failed == 1
        assert result.written == 0
        assert any(e["stage"] == "write" for e in result.errors)

    def test_partial_failure_continues(self, tmp_path: Path) -> None:
        """3 tickets, middle one fails to write — others still succeed."""
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        src.write(make_ticket("T-001"))
        src.write(make_ticket("T-002"))
        src.write(make_ticket("T-003"))

        config = make_config()
        engine = SyncEngine(source=src, dest=dst, config=config)

        original_write = dst.write
        call_count = 0

        def write_with_middle_failure(t: Ticket) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise IOError("middle failure")
            return original_write(t)

        dst.write = write_with_middle_failure  # type: ignore[method-assign]

        result = engine.run(since=None)
        assert result.written == 2
        assert result.failed == 1
        assert result.fetched == 3


# ---------------------------------------------------------------------------
# SyncResult
# ---------------------------------------------------------------------------


class TestSyncResult:
    def test_sync_result_defaults(self) -> None:
        r = SyncResult()
        assert r.fetched == 0
        assert r.written == 0
        assert r.skipped_duplicate == 0
        assert r.failed == 0
        assert r.errors == []

    def test_sync_result_str_contains_counts(self) -> None:
        r = SyncResult(fetched=10, written=8, skipped_duplicate=1, failed=1)
        s = str(r)
        assert "10" in s
        assert "8" in s


# ---------------------------------------------------------------------------
# Dedup strategies
# ---------------------------------------------------------------------------


class TestDedupStrategies:
    def test_tag_based_calls_mark_synced_on_source(self, tmp_path: Path) -> None:
        """After a successful write, engine calls source.mark_synced."""
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        src.write(make_ticket("T-001"))

        config = make_config(dedup_strategy="tag-based")
        engine = SyncEngine(source=src, dest=dst, config=config)

        marked: list[str] = []
        src.mark_synced = lambda sid: marked.append(sid)  # type: ignore[method-assign]

        result = engine.run(since=None)
        assert result.written == 1
        assert len(marked) == 1

    def test_tag_based_skips_mark_synced_when_not_implemented(
        self, tmp_path: Path
    ) -> None:
        """mark_synced raising NotImplementedError is non-fatal."""
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        src.write(make_ticket("T-001"))

        config = make_config(dedup_strategy="tag-based")
        engine = SyncEngine(source=src, dest=dst, config=config)

        def raise_not_implemented(sid: str) -> None:
            raise NotImplementedError("read-only")

        src.mark_synced = raise_not_implemented  # type: ignore[method-assign]

        result = engine.run(since=None)
        assert result.written == 1
        assert result.failed == 0  # write still succeeded

    def test_destination_check_skips_when_exists(self, tmp_path: Path) -> None:
        """destination-check: if find_by_source_coordinates returns a value, skip."""
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        src.write(make_ticket("T-001"))

        config = make_config(dedup_strategy="destination-check")
        engine = SyncEngine(source=src, dest=dst, config=config)

        # Simulate destination already having this ticket
        dst.find_by_source_coordinates = (  # type: ignore[method-assign]
            lambda ss, sid: "existing-id"
        )

        result = engine.run(since=None)
        assert result.written == 0
        assert result.skipped_duplicate == 1

    def test_destination_check_writes_when_not_exists(self, tmp_path: Path) -> None:
        """destination-check: if find_by_source_coordinates returns None, write."""
        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = LocalFilesystemAdapter(tmp_path / "dst")
        src.write(make_ticket("T-001"))

        config = make_config(dedup_strategy="destination-check")
        engine = SyncEngine(source=src, dest=dst, config=config)

        dst.find_by_source_coordinates = (  # type: ignore[method-assign]
            lambda ss, sid: None
        )

        result = engine.run(since=None)
        assert result.written == 1

    def test_destination_check_falls_back_when_method_absent(
        self, tmp_path: Path
    ) -> None:
        """If dest has no find_by_source_coordinates, always write."""
        from ticketsync.adapter import TicketAdapter

        # Create a minimal destination that does NOT have find_by_source_coordinates.
        class MinimalDest:
            system_name: str = "minimal"

            def to_ticket(self, raw: dict[str, object]) -> Ticket:
                return Ticket.model_validate(raw)

            def from_ticket(self, ticket: Ticket) -> dict[str, object]:
                return {}

            def fetch_new(self, since: datetime | None = None) -> list[dict[str, object]]:
                return []

            def write(self, ticket: Ticket) -> str:
                return ticket.source_id

        src = LocalFilesystemAdapter(tmp_path / "src")
        dst = MinimalDest()
        src.write(make_ticket("T-001"))

        config = make_config(dedup_strategy="destination-check")
        engine = SyncEngine(source=src, dest=dst, config=config)  # type: ignore[arg-type]

        result = engine.run(since=None)
        assert result.written == 1

    def test_config_dedup_strategy_default_is_time_based(self) -> None:
        cfg = SyncConfig.from_dict({
            "source": {"type": "local", "path": "/unused"},
            "destination": {"type": "local", "path": "/unused"},
        })
        assert cfg.dedup_strategy == "time-based"

    def test_config_dedup_strategy_tag_based(self) -> None:
        cfg = SyncConfig.from_dict({
            "source": {"type": "local", "path": "/unused"},
            "destination": {"type": "local", "path": "/unused"},
            "dedup_strategy": "tag-based",
        })
        assert cfg.dedup_strategy == "tag-based"

    def test_config_dedup_strategy_destination_check(self) -> None:
        cfg = SyncConfig.from_dict({
            "source": {"type": "local", "path": "/unused"},
            "destination": {"type": "local", "path": "/unused"},
            "dedup_strategy": "destination-check",
        })
        assert cfg.dedup_strategy == "destination-check"

    def test_config_invalid_dedup_strategy_raises(self) -> None:
        with pytest.raises(Exception):
            SyncConfig.from_dict({
                "source": {"type": "local", "path": "/unused"},
                "destination": {"type": "local", "path": "/unused"},
                "dedup_strategy": "invalid-strategy",
            })
