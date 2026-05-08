"""LocalFilesystem adapter.

Stores tickets as individual JSON files under a root directory.  Each ticket
lives at ``<root>/<source_system>/<source_id>.json``.

This adapter is the primary vehicle for:
- Full end-to-end round-trip tests in CI (no cloud creds required)
- Local development without needing any external system
- Testing the SyncEngine logic against a real filesystem

Directory layout::

    <root>/
        <source_system>/
            <source_id>.json   # one file per ticket, full Ticket JSON
        _meta/
            cursor.json        # persists the last-seen updated_at timestamp

The ``cursor.json`` file is optional.  If absent, ``fetch_new(since=None)``
returns all tickets in the directory.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ticketsync.models import Ticket


# Characters that are safe in filenames on all platforms (Windows + POSIX)
_SAFE_FILENAME_RE = re.compile(r"[^\w\-.]")


def _sanitize_filename(value: str) -> str:
    """Replace unsafe characters in a string so it can be used as a filename."""
    sanitized = _SAFE_FILENAME_RE.sub("_", value)
    # Truncate to 200 chars to avoid OS path-length limits
    return sanitized[:200] or "_empty_"


class LocalFilesystemAdapter:
    """Read/write tickets as JSON files on the local filesystem.

    Parameters
    ----------
    path:
        Root directory.  Created on first write if it does not exist.
    system_name:
        The ``source_system`` value embedded in tickets produced by this
        adapter.  Defaults to ``"local"``.
    """

    system_name: str

    def __init__(self, path: str | Path, system_name: str = "local") -> None:
        self._root = Path(path)
        self.system_name = system_name

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ticket_path(self, source_system: str, source_id: str) -> Path:
        system_dir = self._root / _sanitize_filename(source_system)
        return system_dir / (_sanitize_filename(source_id) + ".json")

    def _cursor_path(self) -> Path:
        return self._root / "_meta" / "cursor.json"

    def _load_cursor(self) -> datetime | None:
        cp = self._cursor_path()
        if not cp.exists():
            return None
        data = json.loads(cp.read_text(encoding="utf-8"))
        ts = data.get("last_seen")
        if ts is None:
            return None
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _save_cursor(self, ts: datetime) -> None:
        cp = self._cursor_path()
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(
            json.dumps({"last_seen": ts.isoformat()}), encoding="utf-8"
        )

    def _all_ticket_files(self) -> list[Path]:
        if not self._root.exists():
            return []
        files: list[Path] = []
        for system_dir in self._root.iterdir():
            if system_dir.is_dir() and system_dir.name != "_meta":
                for f in system_dir.glob("*.json"):
                    files.append(f)
        return files

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def to_ticket(self, raw: dict[str, object]) -> Ticket:
        """Deserialize a raw JSON dict (as stored on disk) to a Ticket."""
        return Ticket.model_validate(raw)

    def from_ticket(self, ticket: Ticket) -> dict[str, object]:
        """Serialize a Ticket to a plain dict suitable for JSON storage."""
        return ticket.model_dump(mode="json")

    def fetch_new(self, since: datetime | None = None) -> list[dict[str, object]]:
        """Return all tickets updated after ``since``.

        If ``since`` is ``None``, the adapter checks for a persisted cursor.
        If no cursor exists, all tickets are returned.
        """
        cutoff = since or self._load_cursor()
        results: list[dict[str, object]] = []
        for fpath in self._all_ticket_files():
            raw_text = fpath.read_text(encoding="utf-8")
            raw: dict[str, object] = json.loads(raw_text)
            if cutoff is not None:
                updated_str = raw.get("updated_at")
                if isinstance(updated_str, str):
                    updated = datetime.fromisoformat(updated_str)
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    if updated <= cutoff:
                        continue
            results.append(raw)
        return results

    def write(self, ticket: Ticket) -> str:
        """Write a ticket to disk and return the filename (as the vendor ID)."""
        fpath = self._ticket_path(ticket.source_system, ticket.source_id)
        fpath.parent.mkdir(parents=True, exist_ok=True)
        payload = self.from_ticket(ticket)
        fpath.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return str(fpath)

    def update_cursor(self, ts: datetime) -> None:
        """Persist ``ts`` as the new fetch cursor."""
        self._save_cursor(ts)

    def count(self) -> int:
        """Return the total number of ticket files stored."""
        return len(self._all_ticket_files())

    def clear(self) -> None:
        """Delete all ticket files (leaves the root directory intact).

        Useful in tests to reset state between runs.
        """
        for fpath in self._all_ticket_files():
            fpath.unlink()
        cursor = self._cursor_path()
        if cursor.exists():
            cursor.unlink()

    def read_by_source_id(
        self, source_system: str, source_id: str
    ) -> Ticket | None:
        """Retrieve a single ticket by its source coordinates, or None."""
        fpath = self._ticket_path(source_system, source_id)
        if not fpath.exists():
            return None
        raw: dict[str, object] = json.loads(fpath.read_text(encoding="utf-8"))
        return self.to_ticket(raw)

    def all_tickets(self) -> list[Ticket]:
        """Return all stored tickets as Ticket objects."""
        tickets: list[Ticket] = []
        for fpath in self._all_ticket_files():
            raw: dict[str, object] = json.loads(fpath.read_text(encoding="utf-8"))
            tickets.append(self.to_ticket(raw))
        return tickets
