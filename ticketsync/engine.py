"""SyncEngine — orchestrates a pull-mode sync between two adapters.

The engine is intentionally thin.  It does not know anything about adapters
beyond the ``TicketAdapter`` protocol.  All scheduling and retry logic is the
caller's responsibility.

Usage::

    from ticketsync.adapters import ADAPTER_REGISTRY
    from ticketsync.config import SyncConfig
    from ticketsync.engine import SyncEngine

    config = SyncConfig.from_yaml("sync.yaml")

    source_cls = ADAPTER_REGISTRY[config.source.type]
    dest_cls   = ADAPTER_REGISTRY[config.destination.type]

    source = source_cls(**config.source.options)
    dest   = dest_cls(**config.destination.options)

    engine = SyncEngine(source=source, dest=dest, config=config)
    result = engine.run()
    print(result)

``SyncEngine.run()`` returns a ``SyncResult`` dataclass with counts and any
tickets that failed to sync.

Deduplication strategies
------------------------

The strategy is controlled by ``config.dedup_strategy``:

``time-based`` (default)
    The engine passes a ``since`` cutoff to ``source.fetch_new()``.  Items
    are not tracked between runs; the lookback window provides the
    dedup guarantee.

    Cross-run guarantee: **only within the lookback window**.  Tickets older
    than ``lookback_hours`` will not be re-fetched and thus not re-written.
    This is process-local — a fresh process with a wider window will re-sync.

``tag-based``
    After a successful write, the engine calls ``source.mark_synced(source_id)``
    to write a label/tag back to the source item.  On the next run, those
    items can be excluded by configuring the source adapter's fetch to skip
    already-tagged items (adapter-dependent).

    The engine always attempts ``mark_synced``; if the source adapter does not
    implement it, the ``NotImplementedError`` is caught and logged as a warning
    (non-fatal).

    Cross-run guarantee: **yes**, provided the source adapter supports
    ``mark_synced`` and the tags survive between runs.

``destination-check``
    Before every write, the engine calls
    ``dest.find_by_source_coordinates(source_system, source_id)``.  If an
    existing record is found, the write is skipped.  If the destination
    adapter does not implement ``find_by_source_coordinates``, the check is
    skipped and the write proceeds.

    Cross-run guarantee: **yes**, as long as the destination retains the
    source coordinates.  One extra API call per ticket.

In-process dedup
----------------

Regardless of strategy, the engine maintains an in-memory set of
``(source_system, source_id)`` pairs already written in the current run.
This prevents double-writes when the same ticket appears multiple times in a
single ``fetch_new`` response.

**Important**: the in-process dedup set is cleared on each ``run()`` call by
default.  If you reuse an engine instance across multiple runs and want to
avoid re-writing tickets seen in earlier runs, set ``clear_cache=False`` and
call ``reset_dedup_cache()`` manually between runs.

Cross-run dedup caveat
----------------------

The in-process dedup set is **not** persisted between process restarts.
For reliable cross-run deduplication, use ``tag-based`` or
``destination-check`` strategy.  ``time-based`` only prevents re-sync of
tickets that fall outside the lookback window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ticketsync.adapter import TicketAdapter
from ticketsync.config import SyncConfig
from ticketsync.models import Ticket

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Summary of a single engine run."""

    fetched: int = 0
    written: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"SyncResult(fetched={self.fetched}, written={self.written}, "
            f"skipped_duplicate={self.skipped_duplicate}, failed={self.failed})"
        )


class SyncEngine:
    """Orchestrates a single pull-mode sync between source and destination.

    Parameters
    ----------
    source:
        The adapter to fetch tickets from.
    dest:
        The adapter to write tickets to.
    config:
        ``SyncConfig`` governing deduplication and lookback window.
    """

    def __init__(
        self,
        source: TicketAdapter,
        dest: TicketAdapter,
        config: SyncConfig,
    ) -> None:
        self._source = source
        self._dest = dest
        self._config = config
        # In-memory dedup set: (source_system, source_id) pairs already written
        # in this run.  Not persisted between process restarts.
        self._written_ids: set[tuple[str, str]] = set()

    def run(
        self,
        since: datetime | None = None,
        clear_cache: bool = True,
    ) -> SyncResult:
        """Execute one sync cycle.

        Parameters
        ----------
        since:
            Fetch tickets updated after this timestamp.  If ``None``, the
            engine computes the cutoff from ``config.lookback_hours``.
            ``lookback_hours=0`` means fetch everything.
        clear_cache:
            If ``True`` (default), the in-process dedup cache is cleared at
            the start of this run.  Set to ``False`` if you want to prevent
            re-writing tickets already written in a previous ``run()`` call
            on the same engine instance.

        Returns
        -------
        SyncResult
            Counts and any per-ticket errors.
        """
        if clear_cache:
            self._written_ids.clear()

        result = SyncResult()
        strategy = self._config.dedup_strategy

        if since is None and self._config.lookback_hours > 0:
            since = datetime.now(timezone.utc) - timedelta(
                hours=self._config.lookback_hours
            )

        logger.debug(
            "SyncEngine.run: source=%s dest=%s since=%s strategy=%s",
            self._source.system_name,
            self._dest.system_name,
            since,
            strategy,
        )

        raw_items = self._source.fetch_new(since=since)
        result.fetched = len(raw_items)

        for raw in raw_items:
            try:
                ticket = self._source.to_ticket(raw)
            except Exception as exc:
                result.failed += 1
                result.errors.append(
                    {
                        "stage": "to_ticket",
                        "raw": raw,
                        "error": str(exc),
                    }
                )
                logger.warning("Failed to convert raw item to Ticket: %s", exc)
                continue

            dedup_key = (ticket.source_system, ticket.source_id)

            # In-process dedup (always active)
            if self._config.deduplication and dedup_key in self._written_ids:
                result.skipped_duplicate += 1
                logger.debug("Skipping in-process duplicate: %s", dedup_key)
                continue

            # Destination-check dedup: one API call per ticket
            if strategy == "destination-check":
                if self._check_dest_exists(ticket):
                    result.skipped_duplicate += 1
                    self._written_ids.add(dedup_key)
                    logger.debug(
                        "Skipping destination-check duplicate: %s", dedup_key
                    )
                    continue

            try:
                self._dest.write(ticket)
                self._written_ids.add(dedup_key)
                result.written += 1
            except Exception as exc:
                result.failed += 1
                result.errors.append(
                    {
                        "stage": "write",
                        "ticket_id": ticket.id,
                        "source_id": ticket.source_id,
                        "error": str(exc),
                    }
                )
                logger.warning(
                    "Failed to write ticket %s: %s", ticket.source_id, exc
                )
                continue

            # Tag-based write-back: mark source item as synced
            if strategy == "tag-based":
                self._mark_synced(ticket)

        return result

    # ------------------------------------------------------------------
    # Dedup helpers
    # ------------------------------------------------------------------

    def _check_dest_exists(self, ticket: Ticket) -> bool:
        """Return True if a record for this ticket already exists at dest.

        Calls ``dest.find_by_source_coordinates`` if available.  If the
        method is absent (adapter does not support it), returns False
        (always write).
        """
        finder = getattr(self._dest, "find_by_source_coordinates", None)
        if finder is None or not callable(finder):
            return False
        try:
            result = finder(ticket.source_system, ticket.source_id)
            return result is not None
        except Exception as exc:
            logger.warning(
                "destination-check failed for %s/%s: %s",
                ticket.source_system,
                ticket.source_id,
                exc,
            )
            return False

    def _mark_synced(self, ticket: Ticket) -> None:
        """Call ``source.mark_synced`` if available; log warning on failure.

        Read-only adapters (CloudWatch, GuardDuty, SecurityHub) raise
        ``NotImplementedError`` — this is caught and logged as a non-fatal
        warning.
        """
        marker = getattr(self._source, "mark_synced", None)
        if marker is None or not callable(marker):
            logger.debug(
                "tag-based: source adapter %s does not implement mark_synced; "
                "write-back skipped",
                self._source.system_name,
            )
            return
        try:
            marker(ticket.source_id)
        except NotImplementedError:
            logger.warning(
                "tag-based: source adapter %s does not support mark_synced "
                "(read-only adapter); write-back skipped for %s",
                self._source.system_name,
                ticket.source_id,
            )
        except Exception as exc:
            logger.warning(
                "tag-based: mark_synced failed for %s/%s: %s",
                ticket.source_system,
                ticket.source_id,
                exc,
            )

    def reset_dedup_cache(self) -> None:
        """Clear the in-memory deduplication set.

        Useful when the same engine instance is reused across multiple runs
        and deduplication should only apply within a single run.
        """
        self._written_ids.clear()
