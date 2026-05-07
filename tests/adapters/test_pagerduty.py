"""Tests for the PagerDuty webhook v2 adapter."""

import pytest

from ticket_sync.adapters.pagerduty import PagerDutyAdapter
from ticket_sync.models import TicketPriority, TicketSource, TicketStatus

MINIMAL_INCIDENT = {
    "id": "PT4KHLK",
    "title": "Database latency spike",
    "status": "triggered",
}

FULL_ENVELOPE = {
    "messages": [
        {
            "event": "incident.trigger",
            "incident": {
                "id": "PT4KHLK",
                "incident_number": 1234,
                "title": "Database latency spike",
                "description": "P95 latency exceeded 2s threshold",
                "status": "triggered",
                "urgency": "high",
                "html_url": "https://acme.pagerduty.com/incidents/PT4KHLK",
                "created_at": "2024-01-15T10:30:00Z",
                "service": {
                    "name": "production-db",
                    "summary": "Production Database",
                },
            },
            "created_on": "2024-01-15T10:30:00Z",
        }
    ]
}

ACKNOWLEDGED_ENVELOPE = {
    "messages": [
        {
            "event": "incident.acknowledge",
            "incident": {
                "id": "PT4KHLK",
                "incident_number": 1234,
                "title": "Database latency spike",
                "status": "acknowledged",
                "urgency": "high",
                "created_at": "2024-01-15T10:30:00Z",
                "service": {"name": "production-db"},
            },
        }
    ]
}

RESOLVED_ENVELOPE = {
    "messages": [
        {
            "event": "incident.resolve",
            "incident": {
                "id": "PT4KHLK",
                "incident_number": 1234,
                "title": "Database latency spike",
                "status": "resolved",
                "urgency": "high",
                "created_at": "2024-01-15T10:30:00Z",
                "service": {"name": "production-db"},
            },
        }
    ]
}


@pytest.fixture
def adapter() -> PagerDutyAdapter:
    return PagerDutyAdapter()


class TestPagerDutyAdapterTitle:
    def test_incident_title_becomes_title(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(FULL_ENVELOPE)
        assert ticket.title == "Database latency spike"

    def test_bare_incident_dict(self, adapter: PagerDutyAdapter) -> None:
        # Bare message dict (without envelope)
        bare = {
            "event": "incident.trigger",
            "incident": FULL_ENVELOPE["messages"][0]["incident"],
        }
        ticket = adapter.parse(bare)
        assert ticket.title == "Database latency spike"


class TestPagerDutyAdapterSource:
    def test_source_is_pagerduty(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(FULL_ENVELOPE)
        assert ticket.source == TicketSource.PAGERDUTY


class TestPagerDutyAdapterStatus:
    def test_triggered_maps_to_open(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(FULL_ENVELOPE)
        assert ticket.status == TicketStatus.OPEN

    def test_acknowledged_maps_to_in_progress(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(ACKNOWLEDGED_ENVELOPE)
        assert ticket.status == TicketStatus.IN_PROGRESS

    def test_resolved_maps_to_resolved(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(RESOLVED_ENVELOPE)
        assert ticket.status == TicketStatus.RESOLVED

    def test_event_type_fallback_for_status(self, adapter: PagerDutyAdapter) -> None:
        # Status field absent; fall back to event type
        envelope = {
            "messages": [
                {
                    "event": "incident.resolve",
                    "incident": {
                        "id": "X",
                        "title": "test",
                        # no status field
                    },
                }
            ]
        }
        ticket = adapter.parse(envelope)
        assert ticket.status == TicketStatus.RESOLVED


class TestPagerDutyAdapterPriority:
    def test_high_urgency_maps_to_high(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(FULL_ENVELOPE)
        assert ticket.priority == TicketPriority.HIGH

    def test_low_urgency_maps_to_medium(self, adapter: PagerDutyAdapter) -> None:
        envelope = {
            "messages": [
                {
                    "event": "incident.trigger",
                    "incident": {
                        "id": "X",
                        "title": "minor issue",
                        "status": "triggered",
                        "urgency": "low",
                    },
                }
            ]
        }
        ticket = adapter.parse(envelope)
        assert ticket.priority == TicketPriority.MEDIUM

    def test_no_urgency_maps_to_unknown(self, adapter: PagerDutyAdapter) -> None:
        envelope = {
            "messages": [
                {
                    "event": "incident.trigger",
                    "incident": {
                        "id": "X",
                        "title": "no urgency",
                        "status": "triggered",
                    },
                }
            ]
        }
        ticket = adapter.parse(envelope)
        assert ticket.priority == TicketPriority.UNKNOWN


class TestPagerDutyAdapterTags:
    def test_service_name_in_tags(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(FULL_ENVELOPE)
        assert "service:production-db" in ticket.tags

    def test_event_type_in_tags(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(FULL_ENVELOPE)
        assert "event:incident.trigger" in ticket.tags


class TestPagerDutyAdapterSourceId:
    def test_source_id_uses_incident_number(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(FULL_ENVELOPE)
        assert ticket.source_id == "PD-1234"

    def test_source_id_falls_back_to_incident_id(self, adapter: PagerDutyAdapter) -> None:
        envelope = {
            "messages": [
                {
                    "event": "incident.trigger",
                    "incident": {
                        "id": "PT4KHLK",
                        "title": "test",
                        # no incident_number
                    },
                }
            ]
        }
        ticket = adapter.parse(envelope)
        assert ticket.source_id == "PT4KHLK"


class TestPagerDutyAdapterCreatedAt:
    def test_created_at_parsed(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(FULL_ENVELOPE)
        assert ticket.created_at is not None
        assert ticket.created_at.year == 2024

    def test_created_at_timezone_aware(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(FULL_ENVELOPE)
        assert ticket.created_at is not None
        assert ticket.created_at.tzinfo is not None

    def test_no_created_at_gives_none(self, adapter: PagerDutyAdapter) -> None:
        envelope = {
            "messages": [
                {
                    "event": "incident.trigger",
                    "incident": {"id": "X", "title": "test"},
                }
            ]
        }
        ticket = adapter.parse(envelope)
        assert ticket.created_at is None


class TestPagerDutyAdapterDescription:
    def test_description_from_incident(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(FULL_ENVELOPE)
        assert "P95 latency" in ticket.description

    def test_html_url_appended_to_description(self, adapter: PagerDutyAdapter) -> None:
        ticket = adapter.parse(FULL_ENVELOPE)
        assert "pagerduty.com" in ticket.description


class TestPagerDutyAdapterErrors:
    def test_missing_incident_title_raises(self, adapter: PagerDutyAdapter) -> None:
        envelope = {
            "messages": [{"event": "incident.trigger", "incident": {"id": "X"}}]
        }
        with pytest.raises(KeyError):
            adapter.parse(envelope)
