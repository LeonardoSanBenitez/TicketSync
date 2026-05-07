"""Tests for the adapter factory function."""

import pytest

from ticket_sync.adapters import (
    CloudWatchAdapter,
    GitHubAdapter,
    JiraAdapter,
    PagerDutyAdapter,
    get_adapter,
)
from ticket_sync.adapters.base import BaseAdapter


class TestGetAdapter:
    def test_cloudwatch(self) -> None:
        adapter = get_adapter("cloudwatch")
        assert isinstance(adapter, CloudWatchAdapter)

    def test_pagerduty(self) -> None:
        adapter = get_adapter("pagerduty")
        assert isinstance(adapter, PagerDutyAdapter)

    def test_jira(self) -> None:
        adapter = get_adapter("jira")
        assert isinstance(adapter, JiraAdapter)

    def test_github(self) -> None:
        adapter = get_adapter("github")
        assert isinstance(adapter, GitHubAdapter)

    def test_all_adapters_are_base_adapter_instances(self) -> None:
        for name in ("cloudwatch", "pagerduty", "jira", "github"):
            adapter = get_adapter(name)
            assert isinstance(adapter, BaseAdapter)

    def test_case_insensitive(self) -> None:
        assert isinstance(get_adapter("CloudWatch"), CloudWatchAdapter)
        assert isinstance(get_adapter("JIRA"), JiraAdapter)
        assert isinstance(get_adapter("PagerDuty"), PagerDutyAdapter)
        assert isinstance(get_adapter("GitHub"), GitHubAdapter)

    def test_strips_whitespace(self) -> None:
        adapter = get_adapter("  cloudwatch  ")
        assert isinstance(adapter, CloudWatchAdapter)

    def test_unknown_source_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown adapter"):
            get_adapter("myspace")

    def test_error_message_lists_known_adapters(self) -> None:
        try:
            get_adapter("bogus")
        except ValueError as exc:
            assert "cloudwatch" in str(exc)
            assert "pagerduty" in str(exc)
            assert "jira" in str(exc)
            assert "github" in str(exc)
        else:
            pytest.fail("Expected ValueError")


class TestTopLevelImports:
    def test_all_adapters_importable_from_ticket_sync(self) -> None:
        from ticket_sync import (
            CloudWatchAdapter,
            GitHubAdapter,
            JiraAdapter,
            PagerDutyAdapter,
            get_adapter,
        )
        assert get_adapter("cloudwatch") is not None
        assert CloudWatchAdapter is not None
        assert GitHubAdapter is not None
        assert JiraAdapter is not None
        assert PagerDutyAdapter is not None
