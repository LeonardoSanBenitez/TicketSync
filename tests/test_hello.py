"""Smoke tests for TicketSync scaffold."""

from ticketsync import hello_ticket, __version__


def test_hello_ticket_returns_string() -> None:
    result = hello_ticket()
    assert isinstance(result, str)


def test_hello_ticket_contains_ticketsync() -> None:
    result = hello_ticket()
    assert "TicketSync" in result


def test_version_is_defined() -> None:
    assert isinstance(__version__, str)
    assert len(__version__) > 0
