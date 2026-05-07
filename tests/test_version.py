"""Smoke tests — verify the package is importable and returns expected values."""

import ticket_sync
from ticket_sync import version
from ticket_sync.version import __version__


def test_version_returns_hello_ticket() -> None:
    """The version() function must return the expected greeting string."""
    assert version() == "hello ticket"


def test_version_string_is_semver() -> None:
    """Package version should follow semver X.Y.Z format."""
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_top_level_import() -> None:
    """All public symbols must be importable from the top-level package."""
    assert hasattr(ticket_sync, "Ticket")
    assert hasattr(ticket_sync, "TicketPriority")
    assert hasattr(ticket_sync, "TicketStatus")
    assert hasattr(ticket_sync, "TicketSource")
    assert hasattr(ticket_sync, "version")
