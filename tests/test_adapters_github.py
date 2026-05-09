"""Tests for GitHubIssuesAdapter.

All tests use a stub HTTP client — no real GitHub token required.
Coverage targets:
- to_ticket(): severity from label, status from state, tags, timestamps, html_url
- from_ticket(): label building, state mapping, PATCH vs POST
- fetch_new(): with and without since
- write(): create (POST) vs update (PATCH) routing
- Edge cases: no labels, multiple severity labels, null body, non-numeric source_id
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ticketsync.adapters.github_issues import GitHubIssuesAdapter
from ticketsync.models import Ticket


# ---------------------------------------------------------------------------
# Stub client
# ---------------------------------------------------------------------------


def make_stub_client(
    get_response: list[Any] | dict[str, Any] | None = None,
    post_response: dict[str, Any] | None = None,
    patch_response: dict[str, Any] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.get.return_value = get_response if get_response is not None else []
    client.post.return_value = post_response or {"number": 99}
    client.patch.return_value = patch_response or {"number": 42}
    return client


def load_fixture(name: str) -> dict[str, Any]:
    fixtures_dir = Path(__file__).parent / "fixtures"
    return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))


def make_adapter(
    client: MagicMock | None = None,
    owner: str = "example-org",
    repo: str = "example-repo",
) -> GitHubIssuesAdapter:
    return GitHubIssuesAdapter(
        client=client or make_stub_client(),
        owner=owner,
        repo=repo,
    )


def make_ticket(source_id: str = "42", **kwargs: Any) -> Ticket:
    defaults: dict[str, Any] = {
        "source_system": "github_issues",
        "source_id": source_id,
        "title": "Test issue",
        "severity": "medium",
    }
    defaults.update(kwargs)
    return Ticket(**defaults)


# ---------------------------------------------------------------------------
# to_ticket
# ---------------------------------------------------------------------------


class TestToTicket:
    def test_fixture_mapping(self) -> None:
        adapter = make_adapter()
        raw = load_fixture("github_issue.json")
        t = adapter.to_ticket(raw)

        assert t.source_id == "42"
        assert t.title == "Memory leak in worker pool"
        assert t.severity == "high"
        assert t.status == "open"
        assert "bug" in t.tags
        assert "worker-pool" in t.tags
        # Severity label must NOT appear in tags
        assert "severity:high" not in t.tags
        assert t.external_url == "https://github.com/example-org/example-repo/issues/42"

    def test_no_labels_gives_informational(self) -> None:
        adapter = make_adapter()
        raw: dict[str, Any] = {
            "number": 1,
            "title": "No labels",
            "body": "",
            "state": "open",
            "labels": [],
            "html_url": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        t = adapter.to_ticket(raw)
        assert t.severity == "informational"

    @pytest.mark.parametrize(
        "label,expected",
        [
            ("severity:critical", "critical"),
            ("severity:high", "high"),
            ("severity:medium", "medium"),
            ("severity:low", "low"),
            ("severity:informational", "informational"),
        ],
    )
    def test_severity_label_extraction(self, label: str, expected: str) -> None:
        adapter = make_adapter()
        raw: dict[str, Any] = {
            "number": 5,
            "title": "T",
            "body": "",
            "state": "open",
            "labels": [{"name": label}],
            "html_url": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        t = adapter.to_ticket(raw)
        assert t.severity == expected

    def test_unknown_severity_label_gives_informational(self) -> None:
        adapter = make_adapter()
        raw: dict[str, Any] = {
            "number": 5,
            "title": "T",
            "body": "",
            "state": "open",
            "labels": [{"name": "severity:SUPER_HIGH"}],
            "html_url": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        t = adapter.to_ticket(raw)
        assert t.severity == "informational"

    def test_closed_issue_maps_to_resolved(self) -> None:
        adapter = make_adapter()
        raw: dict[str, Any] = {
            "number": 10,
            "title": "Fixed bug",
            "body": "",
            "state": "closed",
            "labels": [],
            "html_url": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }
        t = adapter.to_ticket(raw)
        assert t.status == "resolved"

    def test_null_body_becomes_empty_string(self) -> None:
        adapter = make_adapter()
        raw: dict[str, Any] = {
            "number": 11,
            "title": "No body",
            "body": None,
            "state": "open",
            "labels": [],
            "html_url": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        t = adapter.to_ticket(raw)
        assert t.description == ""

    def test_multiple_non_severity_labels_become_tags(self) -> None:
        adapter = make_adapter()
        raw: dict[str, Any] = {
            "number": 12,
            "title": "T",
            "body": "",
            "state": "open",
            "labels": [
                {"name": "severity:low"},
                {"name": "bug"},
                {"name": "help wanted"},
                {"name": "good first issue"},
            ],
            "html_url": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        t = adapter.to_ticket(raw)
        assert "bug" in t.tags
        assert "help wanted" in t.tags
        assert "good first issue" in t.tags
        assert "severity:low" not in t.tags

    def test_raw_preserved(self) -> None:
        adapter = make_adapter()
        raw = load_fixture("github_issue.json")
        t = adapter.to_ticket(raw)
        assert t.raw["number"] == 42

    def test_custom_system_name(self) -> None:
        adapter = GitHubIssuesAdapter(
            client=make_stub_client(),
            owner="org",
            repo="repo",
            system_name="my-gh",
        )
        raw: dict[str, Any] = {
            "number": 1,
            "title": "T",
            "body": "",
            "state": "open",
            "labels": [],
            "html_url": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        t = adapter.to_ticket(raw)
        assert t.source_system == "my-gh"


# ---------------------------------------------------------------------------
# from_ticket
# ---------------------------------------------------------------------------


class TestFromTicket:
    def test_basic_payload(self) -> None:
        adapter = make_adapter()
        t = make_ticket(severity="high", tags=["bug"])
        payload = adapter.from_ticket(t)
        assert payload["title"] == "Test issue"
        assert "severity:high" in payload["labels"]
        assert "bug" in payload["labels"]
        assert payload["state"] == "open"

    def test_informational_severity_no_label_added(self) -> None:
        adapter = make_adapter()
        t = make_ticket(severity="informational")
        payload = adapter.from_ticket(t)
        labels = payload["labels"]
        assert not any(str(l).startswith("severity:") for l in labels)

    @pytest.mark.parametrize("status", ["resolved", "closed"])
    def test_resolved_and_closed_map_to_closed_state(self, status: str) -> None:
        adapter = make_adapter()
        t = make_ticket(status=status)
        payload = adapter.from_ticket(t)
        assert payload["state"] == "closed"

    def test_open_and_in_progress_map_to_open_state(self) -> None:
        adapter = make_adapter()
        for status in ("open", "in_progress"):
            t = make_ticket(status=status)
            payload = adapter.from_ticket(t)
            assert payload["state"] == "open"


# ---------------------------------------------------------------------------
# fetch_new
# ---------------------------------------------------------------------------


class TestFetchNew:
    def test_returns_list_from_client(self) -> None:
        issues = [{"number": 1}, {"number": 2}]
        client = make_stub_client(get_response=issues)
        adapter = make_adapter(client=client)
        result = adapter.fetch_new()
        assert len(result) == 2

    def test_since_passed_to_client(self) -> None:
        client = make_stub_client(get_response=[])
        adapter = make_adapter(client=client)
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        adapter.fetch_new(since=since)
        call_kwargs = client.get.call_args
        params = call_kwargs[1]["params"] if call_kwargs[1] else call_kwargs[0][1]
        assert "since" in params

    def test_no_since_no_since_param(self) -> None:
        client = make_stub_client(get_response=[])
        adapter = make_adapter(client=client)
        adapter.fetch_new(since=None)
        call_kwargs = client.get.call_args
        params = call_kwargs[1]["params"] if call_kwargs[1] else call_kwargs[0][1]
        assert "since" not in params

    def test_non_list_response_returns_empty(self) -> None:
        client = make_stub_client(get_response={"error": "not a list"})
        adapter = make_adapter(client=client)
        result = adapter.fetch_new()
        assert result == []


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_creates_issue_when_no_source_id(self) -> None:
        client = make_stub_client(post_response={"number": 101})
        adapter = make_adapter(client=client)
        t = make_ticket(source_id="")
        result = adapter.write(t)
        client.post.assert_called_once()
        client.patch.assert_not_called()
        assert result == "101"

    def test_write_patches_issue_when_tracking_label_found_numeric(self) -> None:
        """When tracking label exists, adapter PATCHes regardless of source_id format."""
        client = make_stub_client(
            get_response=[{"number": 42}],  # label search returns existing issue
            patch_response={"number": 42},
        )
        adapter = make_adapter(client=client)
        t = make_ticket(source_id="42")  # numeric source_id
        result = adapter.write(t)
        client.patch.assert_called_once()
        assert result == "42"

    def test_write_creates_when_tracking_label_not_found(self) -> None:
        """When tracking label is absent, adapter POSTs regardless of source_id format."""
        client = make_stub_client(
            get_response=[],  # label search returns nothing
            post_response={"number": 200},
        )
        adapter = make_adapter(client=client)
        t = make_ticket(source_id="oi-123abc")
        result = adapter.write(t)
        client.post.assert_called()
        assert result == "200"

    def test_patch_url_includes_issue_number(self) -> None:
        """When existing issue found, PATCH URL contains its number."""
        client = make_stub_client(
            get_response=[{"number": 55}],  # label search finds issue 55
            patch_response={"number": 55},
        )
        adapter = make_adapter(client=client, owner="myorg", repo="myrepo")
        t = make_ticket(source_id="55")
        adapter.write(t)
        call_args = client.patch.call_args[0]
        url = call_args[0]
        assert "myorg/myrepo/issues/55" in url

    def test_post_url_correct(self) -> None:
        client = make_stub_client(post_response={"number": 1})
        adapter = make_adapter(client=client, owner="myorg", repo="myrepo")
        t = make_ticket(source_id="")
        adapter.write(t)
        post_calls = [c for c in client.post.call_args_list]
        assert any("myorg/myrepo/issues" in str(c) for c in post_calls)

    def test_write_uses_label_lookup_not_isdigit(self) -> None:
        """Cross-adapter correctness: numeric source_id must NOT trigger PATCH.

        Old code: source_id.isdigit() → PATCH (wrong for OpsCenter → GitHub pipelines)
        New code: label search → determines POST vs PATCH
        """
        # No existing issue found (label search returns empty)
        client = make_stub_client(
            get_response=[],  # label search returns empty list
            post_response={"number": 99},
        )
        adapter = make_adapter(client=client)
        # Ticket from OpsCenter (numeric source_id)
        t = make_ticket(source_id="1", source_system="opscenter")
        result = adapter.write(t)
        # Must POST (create), not PATCH — the label search found nothing
        client.post.assert_called()
        assert result == "99"

    def test_write_patches_when_tracking_label_found(self) -> None:
        """If tracking label already exists in GitHub, adapter should PATCH."""
        existing_issue = [{"number": 42}]
        client = make_stub_client(
            get_response=existing_issue,  # label search finds existing issue
            patch_response={"number": 42},
        )
        adapter = make_adapter(client=client)
        t = make_ticket(source_id="oi-abc123", source_system="opscenter")
        result = adapter.write(t)
        client.patch.assert_called()
        assert result == "42"

    def test_write_includes_tracking_label(self) -> None:
        """Every created issue must carry the tracking label."""
        client = make_stub_client(
            get_response=[],
            post_response={"number": 5},
        )
        adapter = make_adapter(client=client)
        t = make_ticket(source_id="gd-findingABC")
        adapter.write(t)
        call_kwargs = client.post.call_args[1]
        payload = call_kwargs["json"]
        labels = payload.get("labels", [])
        assert any("ticketsync:source_id=" in str(lbl) for lbl in labels)

    def test_write_assignee_included_when_set(self) -> None:
        """from_ticket includes assignees list when ticket.assignee is set."""
        client = make_stub_client(
            get_response=[],
            post_response={"number": 10},
        )
        adapter = make_adapter(client=client)
        t = make_ticket(source_id="", assignee="alice")
        adapter.write(t)
        call_kwargs = client.post.call_args[1]
        payload = call_kwargs["json"]
        assert payload.get("assignees") == ["alice"]

    def test_write_no_assignee_when_empty(self) -> None:
        """from_ticket omits assignees key when ticket.assignee is empty."""
        adapter = make_adapter()
        t = make_ticket(source_id="", assignee="")
        payload = adapter.from_ticket(t)
        assert "assignees" not in payload


# ---------------------------------------------------------------------------
# Assignee round-trip
# ---------------------------------------------------------------------------


class TestAssigneeRoundTrip:
    def test_to_ticket_reads_first_assignee(self) -> None:
        adapter = make_adapter()
        raw: dict[str, Any] = {
            "number": 1,
            "title": "T",
            "body": "",
            "state": "open",
            "labels": [],
            "html_url": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "assignees": [{"login": "alice"}, {"login": "bob"}],
        }
        t = adapter.to_ticket(raw)
        assert t.assignee == "alice"

    def test_to_ticket_empty_assignee_when_no_assignees(self) -> None:
        adapter = make_adapter()
        raw: dict[str, Any] = {
            "number": 1,
            "title": "T",
            "body": "",
            "state": "open",
            "labels": [],
            "html_url": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "assignees": [],
        }
        t = adapter.to_ticket(raw)
        assert t.assignee == ""

    def test_tracking_labels_filtered_from_tags(self) -> None:
        """Tracking labels (ticketsync:source_id=*) must not appear in ticket.tags."""
        adapter = make_adapter()
        raw: dict[str, Any] = {
            "number": 5,
            "title": "T",
            "body": "",
            "state": "open",
            "labels": [
                {"name": "bug"},
                {"name": "ticketsync:source_id=opscenter/oi-001"},
            ],
            "html_url": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        t = adapter.to_ticket(raw)
        assert "bug" in t.tags
        assert not any("ticketsync:source_id=" in tag for tag in t.tags)


# ---------------------------------------------------------------------------
# Tag-based sync write-back and destination-check
# ---------------------------------------------------------------------------


class TestTagBasedSync:
    def test_mark_synced_posts_label(self) -> None:
        client = make_stub_client(post_response={"number": 42})
        adapter = make_adapter(client=client)
        adapter.mark_synced("42")
        client.post.assert_called()
        call_args = client.post.call_args
        assert "labels" in str(call_args)

    def test_find_by_source_coordinates_returns_number(self) -> None:
        client = make_stub_client(get_response=[{"number": 55}])
        adapter = make_adapter(client=client)
        result = adapter.find_by_source_coordinates("guardduty", "gd-abc123")
        assert result == "55"

    def test_find_by_source_coordinates_returns_none_when_empty(self) -> None:
        client = make_stub_client(get_response=[])
        adapter = make_adapter(client=client)
        result = adapter.find_by_source_coordinates("guardduty", "gd-abc999")
        assert result is None


# ---------------------------------------------------------------------------
# Triage metadata
# ---------------------------------------------------------------------------


class TestTriageMetadata:
    def test_write_metadata_posts_comment(self) -> None:
        from ticketsync.metadata import TriageMetadata

        client = make_stub_client(post_response={"id": 999})
        adapter = make_adapter(client=client)
        meta = TriageMetadata(assignee="alice", priority=1, triage_notes="OK")
        adapter.write_metadata("42", meta)
        # Should POST to the comments endpoint
        client.post.assert_called()
        call_args = client.post.call_args
        assert "comments" in str(call_args)

    def test_read_metadata_returns_none_when_no_comments(self) -> None:
        client = make_stub_client(get_response=[])
        adapter = make_adapter(client=client)
        result = adapter.read_metadata("42")
        assert result is None

    def test_read_metadata_parses_structured_comment(self) -> None:
        from ticketsync.metadata import TriageMetadata

        comment_body = (
            "<!-- TicketSync triage metadata (do not edit) -->\n"
            "Assignee: bob@example.com\n"
            "Priority: 2\n"
            "Triage notes: Confirmed malicious.\n"
        )
        client = make_stub_client(get_response=[{"body": comment_body}])
        adapter = make_adapter(client=client)
        result = adapter.read_metadata("42")
        assert isinstance(result, TriageMetadata)
        assert result.assignee == "bob@example.com"
        assert result.priority == 2
        assert result.triage_notes == "Confirmed malicious."

    def test_read_metadata_returns_none_for_non_metadata_comments(self) -> None:
        client = make_stub_client(get_response=[{"body": "Just a regular comment."}])
        adapter = make_adapter(client=client)
        result = adapter.read_metadata("42")
        assert result is None

    def test_is_metadata_adapter(self) -> None:
        from ticketsync.metadata import MetadataAdapter

        adapter = make_adapter()
        assert isinstance(adapter, MetadataAdapter)
