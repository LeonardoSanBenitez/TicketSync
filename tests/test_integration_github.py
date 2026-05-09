"""GitHub integration tests for TicketSync.

These tests use real GitHub API calls against LeonardoSanBenitez/TicketSync.
They are skipped by default and must be run with:

    pytest tests/test_integration_github.py -m integration -v

A GitHub PAT must be available via environment variable:
    GITHUB_PAT

The PAT must have 'issues:write' scope on the test repository.

Test infrastructure:
    Repository: LeonardoSanBenitez/TicketSync
    Issues enabled: yes (enabled 2026-05-08)
    Label created: severity:high (created 2026-05-08)

What these tests do:
1. Create a test issue via write()
2. Fetch issues via fetch_new() and assert the new issue appears
3. Verify round-trip: to_ticket() maps the issue to the correct IR fields
4. Close the test issue via write() with status=resolved
5. Verify the closed issue has status=resolved via a second fetch
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# httpx import — entire module is skipped if not installed
# ---------------------------------------------------------------------------

_HTTPX_AVAILABLE: bool = False
try:
    import httpx  # type: ignore[import-untyped]
    _HTTPX_AVAILABLE = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OWNER = "LeonardoSanBenitez"
_REPO = "TicketSync"
_PAT_ENV_VAR = "GITHUB_PAT"


# ---------------------------------------------------------------------------
# HTTP client wrapper
# ---------------------------------------------------------------------------


class _GitHubClient:
    """Thin httpx wrapper with GitHub auth headers matching the adapter interface."""

    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        r = httpx.get(url, params=params, headers=self._headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def post(self, url: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        r = httpx.post(url, json=json, headers=self._headers, timeout=30)
        r.raise_for_status()
        return dict(r.json())

    def patch(self, url: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        r = httpx.patch(url, json=json, headers=self._headers, timeout=30)
        r.raise_for_status()
        return dict(r.json())


# ---------------------------------------------------------------------------
# Skip markers
# ---------------------------------------------------------------------------


def _get_pat() -> str:
    return os.environ.get(_PAT_ENV_VAR, "")


_skip_no_httpx = pytest.mark.skipif(
    not _HTTPX_AVAILABLE,
    reason="httpx not installed — install with: pip install httpx",
)

_skip_no_pat = pytest.mark.skipif(
    not _get_pat(),
    reason=(
        f"No GitHub PAT available. "
        f"Set {_PAT_ENV_VAR} env var to a PAT with issues:write scope."
    ),
)

pytestmark = [pytest.mark.integration, _skip_no_httpx, _skip_no_pat]


# ---------------------------------------------------------------------------
# Helper: get a real client
# ---------------------------------------------------------------------------


def _client() -> _GitHubClient:
    return _GitHubClient(token=_get_pat())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGitHubIssuesRoundTrip:
    """Full round-trip: write issue -> fetch -> to_ticket -> verify fields -> close."""

    def test_create_fetch_and_close_issue(self) -> None:
        """
        1. Create a test issue via write()
        2. Fetch all issues via fetch_new() — test issue must appear
        3. Map to Ticket via to_ticket() — verify severity, status, title
        4. Close the issue via write() with status=resolved
        5. Verify closed state via fetch_new() with state=closed
        """
        from ticketsync.adapters.github_issues import GitHubIssuesAdapter
        from ticketsync.models import Ticket

        client = _client()
        adapter = GitHubIssuesAdapter(client=client, owner=_OWNER, repo=_REPO)

        # Step 1: Create a test issue
        test_title = "[TicketSync integration test] GitHub round-trip verification"
        source_ticket = Ticket(
            source_system="github_issues",
            source_id="",  # blank => POST (create new)
            title=test_title,
            description="Created by TicketSync integration test. Safe to close.",
            severity="high",
            tags=["integration-test"],
        )
        issue_number = adapter.write(source_ticket)
        assert issue_number, "write() must return a non-empty issue number"
        assert issue_number.isdigit(), (
            f"Issue number should be numeric, got: {issue_number!r}"
        )

        # Step 2: Fetch all open issues — test issue must appear.
        # GitHub's list-issues endpoint has an eventual-consistency delay of
        # a few seconds after a create.  Retry up to 5 times with a 2-second
        # back-off to make the test reliable without an unconditional sleep.
        our_raw: list[dict[str, object]] = []
        all_raw: list[dict[str, object]] = []
        for _attempt in range(5):
            all_raw = adapter.fetch_new()
            assert isinstance(all_raw, list), "fetch_new() must return a list"
            our_raw = [r for r in all_raw if str(r.get("number")) == issue_number]
            if our_raw:
                break
            time.sleep(2)
        assert len(our_raw) == 1, (
            f"Expected issue #{issue_number} in fetch_new() results after retries. "
            f"Got numbers: {[r.get('number') for r in all_raw]}"
        )

        # Step 3: Map to Ticket IR — verify fields
        ticket = adapter.to_ticket(our_raw[0])
        assert ticket.source_id == issue_number, (
            f"source_id mismatch: expected {issue_number!r} got {ticket.source_id!r}"
        )
        assert ticket.title == test_title, (
            f"title mismatch: expected {test_title!r} got {ticket.title!r}"
        )
        assert ticket.severity == "high", (
            f"severity mismatch: expected 'high' (from severity:high label) "
            f"got {ticket.severity!r}"
        )
        assert ticket.status == "open", (
            f"status mismatch: expected 'open' got {ticket.status!r}"
        )
        assert ticket.source_system == "github_issues"
        assert ticket.external_url.startswith("https://github.com"), (
            f"external_url must be a GitHub URL, got: {ticket.external_url!r}"
        )

        # Step 4: Close the issue via write() with status=resolved
        closed_ticket = ticket.with_status("resolved")
        returned_id = adapter.write(closed_ticket)
        assert returned_id == issue_number, (
            f"write(closed) returned {returned_id!r}, expected {issue_number!r}"
        )

        # Step 5: Verify closed state.
        # Apply the same retry pattern as step 2 — GitHub list endpoints have
        # an eventual-consistency delay of a few seconds after state changes.
        base_url = f"https://api.github.com/repos/{_OWNER}/{_REPO}/issues"
        closed_raw_list: list[dict[str, object]] = []
        for _attempt in range(5):
            raw = client.get(base_url, params={"state": "closed", "per_page": 50})
            assert isinstance(raw, list)
            closed_raw_list = list(raw)
            closed_numbers = [str(r.get("number")) for r in closed_raw_list]
            if issue_number in closed_numbers:
                break
            time.sleep(2)
        closed_numbers = [str(r.get("number")) for r in closed_raw_list]
        assert issue_number in closed_numbers, (
            f"Expected issue #{issue_number} to be closed after retries. "
            f"Closed issue numbers: {closed_numbers}"
        )

        # Map the closed issue and check status
        matching_closed = next(
            (r for r in closed_raw_list if str(r.get("number")) == issue_number),
            None,
        )
        assert matching_closed is not None
        closed_ticket_ir = adapter.to_ticket(matching_closed)
        assert closed_ticket_ir.status == "resolved", (
            f"Expected 'resolved' after closing, got {closed_ticket_ir.status!r}"
        )


class TestGitHubFetchNew:
    """Verify fetch_new() returns a list and respects the since parameter."""

    def test_fetch_returns_list(self) -> None:
        from ticketsync.adapters.github_issues import GitHubIssuesAdapter

        adapter = GitHubIssuesAdapter(client=_client(), owner=_OWNER, repo=_REPO)
        result = adapter.fetch_new()
        assert isinstance(result, list), "fetch_new() must return a list"

    def test_fetch_since_past_datetime_empty_or_list(self) -> None:
        """fetch_new(since=far_future) should return empty (no issues since then)."""
        from datetime import datetime, timezone

        from ticketsync.adapters.github_issues import GitHubIssuesAdapter

        adapter = GitHubIssuesAdapter(client=_client(), owner=_OWNER, repo=_REPO)
        # Far future — nothing should be updated after this
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        result = adapter.fetch_new(since=far_future)
        assert isinstance(result, list)
        assert len(result) == 0, (
            f"Expected 0 issues updated after far future date, got {len(result)}"
        )
