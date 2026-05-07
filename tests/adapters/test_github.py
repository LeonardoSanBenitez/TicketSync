"""Tests for the GitHub issues webhook adapter."""

import pytest

from ticket_sync.adapters.github import GitHubAdapter
from ticket_sync.models import TicketPriority, TicketSource, TicketStatus

MINIMAL_ISSUE = {
    "issue": {
        "number": 42,
        "title": "Something is broken",
        "state": "open",
    }
}

FULL_OPEN_ISSUE = {
    "action": "opened",
    "issue": {
        "number": 42,
        "title": "Feature request: dark mode",
        "body": "Would be great to have dark mode support. Many users have requested this.",
        "state": "open",
        "html_url": "https://github.com/org/repo/issues/42",
        "labels": [
            {"name": "enhancement"},
            {"name": "good first issue"},
            {"name": "high"},
        ],
        "created_at": "2024-01-15T10:30:00Z",
        "user": {"login": "contributor"},
    },
    "repository": {
        "full_name": "org/repo",
        "name": "repo",
    },
}

CLOSED_ISSUE = {
    "action": "closed",
    "issue": {
        "number": 10,
        "title": "Fix memory leak in worker",
        "body": "Found a memory leak...",
        "state": "closed",
        "html_url": "https://github.com/org/repo/issues/10",
        "labels": [{"name": "bug"}, {"name": "critical"}],
        "created_at": "2024-01-10T08:00:00Z",
        "user": {"login": "maintainer"},
    },
    "repository": {"full_name": "org/repo", "name": "repo"},
}

REOPENED_ISSUE = {
    "action": "reopened",
    "issue": {
        "number": 99,
        "title": "Regression: login fails",
        "body": "Regression from v2.3.",
        "state": "open",
        "labels": [{"name": "p0"}],
        "created_at": "2024-01-20T12:00:00Z",
    },
    "repository": {"full_name": "org/repo", "name": "repo"},
}


@pytest.fixture
def adapter() -> GitHubAdapter:
    return GitHubAdapter()


class TestGitHubAdapterTitle:
    def test_issue_title_becomes_title(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert ticket.title == "Feature request: dark mode"

    def test_minimal_issue_title(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert ticket.title == "Something is broken"


class TestGitHubAdapterSource:
    def test_source_is_github(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert ticket.source == TicketSource.GITHUB


class TestGitHubAdapterStatus:
    def test_open_issue_maps_to_open(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert ticket.status == TicketStatus.OPEN

    def test_closed_issue_maps_to_closed(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(CLOSED_ISSUE)
        assert ticket.status == TicketStatus.CLOSED

    def test_reopened_issue_maps_to_open(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(REOPENED_ISSUE)
        assert ticket.status == TicketStatus.OPEN

    def test_default_state_is_open(self, adapter: GitHubAdapter) -> None:
        # MINIMAL_ISSUE has "state": "open"
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert ticket.status == TicketStatus.OPEN


class TestGitHubAdapterPriority:
    def test_high_label_maps_to_high(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert ticket.priority == TicketPriority.HIGH

    def test_critical_label_maps_to_critical(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(CLOSED_ISSUE)
        assert ticket.priority == TicketPriority.CRITICAL

    def test_p0_label_maps_to_critical(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(REOPENED_ISSUE)
        assert ticket.priority == TicketPriority.CRITICAL

    def test_no_priority_label_maps_to_unknown(self, adapter: GitHubAdapter) -> None:
        issue = {
            "issue": {
                "number": 1,
                "title": "x",
                "state": "open",
                "labels": [{"name": "documentation"}],
            }
        }
        ticket = adapter.parse(issue)
        assert ticket.priority == TicketPriority.UNKNOWN

    def test_no_labels_maps_to_unknown(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert ticket.priority == TicketPriority.UNKNOWN

    def test_p1_label_maps_to_high(self, adapter: GitHubAdapter) -> None:
        issue = {
            "issue": {
                "number": 1,
                "title": "x",
                "state": "open",
                "labels": [{"name": "p1"}],
            }
        }
        ticket = adapter.parse(issue)
        assert ticket.priority == TicketPriority.HIGH

    def test_p2_label_maps_to_medium(self, adapter: GitHubAdapter) -> None:
        issue = {
            "issue": {
                "number": 1,
                "title": "x",
                "state": "open",
                "labels": [{"name": "p2"}],
            }
        }
        ticket = adapter.parse(issue)
        assert ticket.priority == TicketPriority.MEDIUM

    def test_p3_label_maps_to_low(self, adapter: GitHubAdapter) -> None:
        issue = {
            "issue": {
                "number": 1,
                "title": "x",
                "state": "open",
                "labels": [{"name": "p3"}],
            }
        }
        ticket = adapter.parse(issue)
        assert ticket.priority == TicketPriority.LOW


class TestGitHubAdapterTags:
    def test_labels_become_tags(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert "enhancement" in ticket.tags
        assert "good first issue" in ticket.tags

    def test_action_in_tags(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert "action:opened" in ticket.tags

    def test_repo_name_in_tags(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert "repo:repo" in ticket.tags

    def test_no_action_no_action_tag(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert not any(t.startswith("action:") for t in ticket.tags)

    def test_no_repo_no_repo_tag(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert not any(t.startswith("repo:") for t in ticket.tags)


class TestGitHubAdapterSourceId:
    def test_source_id_includes_repo_and_number(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert ticket.source_id == "org/repo#42"

    def test_source_id_falls_back_to_number(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert ticket.source_id == "42"


class TestGitHubAdapterCreatedAt:
    def test_created_at_parsed(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert ticket.created_at is not None
        assert ticket.created_at.year == 2024

    def test_created_at_timezone_aware(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert ticket.created_at is not None
        assert ticket.created_at.tzinfo is not None

    def test_no_created_at_gives_none(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert ticket.created_at is None


class TestGitHubAdapterDescription:
    def test_body_becomes_description(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert "dark mode" in ticket.description

    def test_no_body_gives_empty(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(MINIMAL_ISSUE)
        assert ticket.description == ""

    def test_null_body_gives_empty(self, adapter: GitHubAdapter) -> None:
        issue = {
            "issue": {"number": 1, "title": "x", "state": "open", "body": None}
        }
        ticket = adapter.parse(issue)
        assert ticket.description == ""


class TestGitHubAdapterMetadata:
    def test_issue_number_in_metadata(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert ticket.metadata.get("issue_number") == 42

    def test_html_url_in_metadata(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert "github.com" in str(ticket.metadata.get("html_url"))

    def test_author_in_metadata(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert ticket.metadata.get("author") == "contributor"

    def test_repository_in_metadata(self, adapter: GitHubAdapter) -> None:
        ticket = adapter.parse(FULL_OPEN_ISSUE)
        assert ticket.metadata.get("repository") == "org/repo"


class TestGitHubAdapterErrors:
    def test_missing_issue_raises(self, adapter: GitHubAdapter) -> None:
        with pytest.raises(KeyError):
            adapter.parse({"action": "opened"})

    def test_missing_title_raises(self, adapter: GitHubAdapter) -> None:
        with pytest.raises(KeyError):
            adapter.parse({"issue": {"number": 1, "state": "open"}})
