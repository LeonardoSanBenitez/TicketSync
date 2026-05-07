"""Smoke tests for TicketSync scaffold."""

from ticket_sync import hello_ticket, __version__


def test_hello_ticket_returns_string() -> None:
    result = hello_ticket()
    assert isinstance(result, str)


def test_hello_ticket_contains_ticket_sync() -> None:
    result = hello_ticket()
    assert "TicketSync" in result


def test_version_is_defined() -> None:
    assert isinstance(__version__, str)
    assert len(__version__) > 0
