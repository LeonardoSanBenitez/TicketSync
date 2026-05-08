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
        # In-memory dedup set: (source_system, source_id) pairs already written.
        self._written_ids: set[tuple[str, str]] = set()

    def run(self, since: datetime | None = None) -> SyncResult:
        """Execute one sync cycle.

        Parameters
        ----------
        since:
            Fetch tickets updated after this timestamp.  If ``None``, the
            engine computes the cutoff from ``config.lookback_hours``.
            ``lookback_hours=0`` means fetch everything.

        Returns
        -------
        SyncResult
            Counts and any per-ticket errors.
        """
        result = SyncResult()

        if since is None and self._config.lookback_hours > 0:
            since = datetime.now(timezone.utc) - timedelta(
                hours=self._config.lookback_hours
            )

        logger.debug(
            "SyncEngine.run: source=%s dest=%s since=%s",
            self._source.system_name,
            self._dest.system_name,
            since,
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

            if self._config.deduplication and dedup_key in self._written_ids:
                result.skipped_duplicate += 1
                logger.debug("Skipping duplicate: %s", dedup_key)
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

        return result

    def reset_dedup_cache(self) -> None:
        """Clear the in-memory deduplication set.

        Useful when the same engine instance is reused across multiple runs
        and deduplication should only apply within a single run.
        """
        self._written_ids.clear()
