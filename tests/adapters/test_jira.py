"""Tests for the Jira webhook adapter."""

import pytest

from ticket_sync.adapters.jira import JiraAdapter
from ticket_sync.models import TicketPriority, TicketSource, TicketStatus

MINIMAL_ISSUE = {
    "issue": {
        "id": "10001",
        "key": "PROJ-123",
        "fields": {
            "summary": "Login button broken on mobile",
        },
    }
}

FULL_ISSUE = {
    "webhookEvent": "jira:issue_created",
    "issue": {
        "id": "10001",
        "key": "PROJ-123",
        "self": "https://jira.example.com/rest/api/2/issue/10001",
        "fields": {
            "summary": "Login button broken on mobile",
            "description": "Steps to reproduce: 1. Open mobile browser...",
            "status": {"name": "To Do"},
            "priority": {"name": "High"},
            "issuetype": {"name": "Bug"},
            "labels": ["frontend", "mobile"],
            "created": "2024-01-15T10:30:00.000+0000",
            "project": {"key": "PROJ", "name": "My Project"},
        },
    },
}

IN_PROGRESS_ISSUE = {
    "webhookEvent": "jira:issue_updated",
    "issue": {
        "key": "PROJ-456",
        "fields": {
            "summary": "Performance regression",
            "status": {"name": "In Progress"},
            "priority": {"name": "Blocker"},
        },
    },
}

CLOSED_ISSUE = {
    "issue": {
        "key": "PROJ-789",
        "fields": {
            "summary": "Old bug fixed",
            "status": {"name": "Done"},
            "priority": {"name": "Low"},
        },
    },
}


@pytest.fixture
def adapter() -> JiraAdapter:
    return JiraAdapter()


class TestJiraAdapterTitle:
    def test_summary_becomes_title(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert ticket.title == "Login button broken on mobile"

    def test_minimal_issue_title(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert ticket.title == "Login button broken on mobile"


class TestJiraAdapterSource:
    def test_source_is_jira(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert ticket.source == TicketSource.JIRA


class TestJiraAdapterStatus:
    def test_to_do_maps_to_open(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert ticket.status == TicketStatus.OPEN

    def test_in_progress_maps_correctly(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(IN_PROGRESS_ISSUE)
        assert ticket.status == TicketStatus.IN_PROGRESS

    def test_done_maps_to_resolved(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(CLOSED_ISSUE)
        assert ticket.status == TicketStatus.RESOLVED

    def test_unknown_status_maps_to_unknown(self, adapter: JiraAdapter) -> None:
        issue = {
            "issue": {
                "key": "X-1",
                "fields": {
                    "summary": "test",
                    "status": {"name": "Some Weird State"},
                },
            }
        }
        ticket = adapter.parse(issue)
        assert ticket.status == TicketStatus.UNKNOWN

    def test_no_status_field_maps_to_unknown(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert ticket.status == TicketStatus.UNKNOWN


class TestJiraAdapterPriority:
    def test_high_priority_maps_to_high(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert ticket.priority == TicketPriority.HIGH

    def test_blocker_maps_to_critical(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(IN_PROGRESS_ISSUE)
        assert ticket.priority == TicketPriority.CRITICAL

    def test_low_maps_to_low(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(CLOSED_ISSUE)
        assert ticket.priority == TicketPriority.LOW

    def test_no_priority_field_maps_to_unknown(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert ticket.priority == TicketPriority.UNKNOWN

    def test_case_insensitive_priority(self, adapter: JiraAdapter) -> None:
        issue = {
            "issue": {
                "key": "X-1",
                "fields": {"summary": "test", "priority": {"name": "CRITICAL"}},
            }
        }
        ticket = adapter.parse(issue)
        assert ticket.priority == TicketPriority.CRITICAL

    def test_medium_priority(self, adapter: JiraAdapter) -> None:
        issue = {
            "issue": {
                "key": "X-1",
                "fields": {"summary": "test", "priority": {"name": "Medium"}},
            }
        }
        ticket = adapter.parse(issue)
        assert ticket.priority == TicketPriority.MEDIUM


class TestJiraAdapterTags:
    def test_jira_labels_become_tags(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert "frontend" in ticket.tags
        assert "mobile" in ticket.tags

    def test_issue_type_in_tags(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert "type:bug" in ticket.tags

    def test_action_tag_from_webhook_event(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert "action:created" in ticket.tags

    def test_updated_event_tag(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(IN_PROGRESS_ISSUE)
        assert "action:updated" in ticket.tags

    def test_no_webhook_event_no_action_tag(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert not any(t.startswith("action:") for t in ticket.tags)


class TestJiraAdapterSourceId:
    def test_source_id_is_issue_key(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert ticket.source_id == "PROJ-123"

    def test_source_id_falls_back_to_id(self, adapter: JiraAdapter) -> None:
        issue = {"issue": {"id": "10001", "fields": {"summary": "test"}}}
        ticket = adapter.parse(issue)
        assert ticket.source_id == "10001"


class TestJiraAdapterCreatedAt:
    def test_created_at_parsed(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert ticket.created_at is not None
        assert ticket.created_at.year == 2024

    def test_created_at_timezone_aware(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert ticket.created_at is not None
        assert ticket.created_at.tzinfo is not None

    def test_no_created_gives_none(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert ticket.created_at is None


class TestJiraAdapterDescription:
    def test_description_field_used(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert "Steps to reproduce" in ticket.description

    def test_no_description_gives_empty(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert ticket.description == ""


class TestJiraAdapterMetadata:
    def test_issue_key_in_metadata(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert ticket.metadata.get("issue_key") == "PROJ-123"

    def test_project_key_in_metadata(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert ticket.metadata.get("project_key") == "PROJ"

    def test_webhook_event_in_metadata(self, adapter: JiraAdapter) -> None:
        ticket = adapter.parse(FULL_ISSUE)
        assert ticket.metadata.get("webhook_event") == "jira:issue_created"


class TestJiraAdapterErrors:
    def test_missing_issue_key_raises(self, adapter: JiraAdapter) -> None:
        with pytest.raises(KeyError):
            adapter.parse({"webhookEvent": "jira:issue_created"})

    def test_missing_summary_raises(self, adapter: JiraAdapter) -> None:
        with pytest.raises(KeyError):
            adapter.parse({"issue": {"key": "X-1", "fields": {}}})
