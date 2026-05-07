"""Tests for the core Ticket data model."""

from datetime import datetime, timezone

import pytest

from ticket_sync.models import (
    Ticket,
    TicketPriority,
    TicketSource,
    TicketStatus,
)


class TestTicketDefaults:
    def test_id_auto_generated(self) -> None:
        t = Ticket(title="test")
        assert t.id
        assert len(t.id) == 36  # UUID4 with hyphens

    def test_two_tickets_have_different_ids(self) -> None:
        t1 = Ticket(title="a")
        t2 = Ticket(title="b")
        assert t1.id != t2.id

    def test_default_priority_unknown(self) -> None:
        assert Ticket(title="x").priority == TicketPriority.UNKNOWN

    def test_default_status_unknown(self) -> None:
        assert Ticket(title="x").status == TicketStatus.UNKNOWN

    def test_default_source_custom(self) -> None:
        assert Ticket(title="x").source == TicketSource.CUSTOM

    def test_synced_at_set_on_creation(self) -> None:
        t = Ticket(title="x")
        assert t.synced_at is not None
        assert t.synced_at.tzinfo is not None  # timezone-aware

    def test_tags_default_empty_list(self) -> None:
        assert Ticket(title="x").tags == []

    def test_metadata_default_empty_dict(self) -> None:
        assert Ticket(title="x").metadata == {}


class TestTicketRoundTrip:
    def test_to_dict_and_back(self) -> None:
        t = Ticket(
            title="disk full",
            description="root partition at 99%",
            source=TicketSource.CLOUDWATCH,
            source_id="alarm-abc123",
            priority=TicketPriority.HIGH,
            status=TicketStatus.OPEN,
            tags=["infra", "storage"],
            metadata={"region": "us-east-1"},
        )
        d = t.to_dict()
        t2 = Ticket.from_dict(d)
        assert t2.title == t.title
        assert t2.description == t.description
        assert t2.source == t.source
        assert t2.source_id == t.source_id
        assert t2.priority == t.priority
        assert t2.status == t.status
        assert t2.tags == t.tags
        assert t2.metadata == t.metadata
        assert t2.id == t.id

    def test_to_dict_source_is_string(self) -> None:
        d = Ticket(title="x", source=TicketSource.JIRA).to_dict()
        assert d["source"] == "jira"

    def test_to_dict_priority_is_string(self) -> None:
        d = Ticket(title="x", priority=TicketPriority.CRITICAL).to_dict()
        assert d["priority"] == "critical"

    def test_from_dict_missing_optional_fields(self) -> None:
        t = Ticket.from_dict({"title": "minimal"})
        assert t.title == "minimal"
        assert t.priority == TicketPriority.UNKNOWN

    def test_from_dict_with_created_at(self) -> None:
        ts = "2024-01-15T10:30:00+00:00"
        t = Ticket.from_dict({"title": "x", "created_at": ts})
        assert t.created_at is not None
        assert t.created_at.year == 2024

    def test_from_dict_created_at_none(self) -> None:
        t = Ticket.from_dict({"title": "x"})
        assert t.created_at is None


class TestTicketEnums:
    def test_all_priorities(self) -> None:
        for p in TicketPriority:
            t = Ticket(title="x", priority=p)
            assert t.priority == p

    def test_all_statuses(self) -> None:
        for s in TicketStatus:
            t = Ticket(title="x", status=s)
            assert t.status == s

    def test_all_sources(self) -> None:
        for src in TicketSource:
            t = Ticket(title="x", source=src)
            assert t.source == src

    def test_invalid_priority_raises(self) -> None:
        with pytest.raises(ValueError):
            Ticket.from_dict({"title": "x", "priority": "super_critical"})

    def test_invalid_source_raises(self) -> None:
        with pytest.raises(ValueError):
            Ticket.from_dict({"title": "x", "source": "myspace"})
