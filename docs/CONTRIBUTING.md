# Contributing — How to Write a New Adapter

This document explains how to add a new TicketSync adapter.  It covers the
Protocol contract, the test pattern, how to register the adapter, and what
must be true before a PR is considered ready.

It is written for two audiences:
- **Humans** who want to contribute a new system integration
- **LLMs** being asked to extend TicketSync with a new adapter

---

## Step 0 — understand the Protocol

Every adapter must implement the `TicketAdapter` Protocol from
`ticketsync/adapter.py`:

```python
class TicketAdapter(Protocol):
    system_name: str          # e.g. "pagerduty", "jira", "splunk"

    def to_ticket(self, raw: dict[str, object]) -> Ticket: ...
    def from_ticket(self, ticket: Ticket) -> dict[str, object]: ...
    def fetch_new(self, since: datetime | None = None) -> list[dict[str, object]]: ...
    def write(self, ticket: Ticket) -> str: ...
```

Read `docs/ARCHITECTURE.md` for the full Ticket IR field reference and the
contract for each method before writing any code.

---

## Step 1 — create the adapter file

Create `ticketsync/adapters/<system_name>.py`.

### Minimal skeleton

```python
"""<System name> adapter.

<One paragraph describing what system this connects to, how it authenticates,
and what it reads/writes.>

The client must expose:
    client.<method>(...)  ->  <return type>
    ...

Field mapping
-------------

Vendor concept           -> Ticket IR field
-----------------------  ----------------------------
<vendor field>             <ticket field> (+ notes)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ticketsync.models import Ticket


class <SystemName>Adapter:
    """Adapter for <system name>.

    Parameters
    ----------
    client:
        A <vendor SDK> client (or compatible stub).  Pass a real client for
        production; pass a MagicMock for unit tests.
    system_name:
        The ``source_system`` label embedded in produced tickets.
    """

    system_name: str

    def __init__(
        self,
        client: Any,
        system_name: str = "<system_name>",
    ) -> None:
        self._client = client
        self.system_name = system_name

    def to_ticket(self, raw: dict[str, object]) -> Ticket:
        """Map a vendor payload to a Ticket IR."""
        finding: dict[str, Any] = dict(raw)
        # TODO: map fields
        return Ticket.model_validate({
            "source_system": self.system_name,
            "source_id": str(finding.get("Id", "")),
            "title": str(finding.get("Title", "") or "<default title>"),
            "description": str(finding.get("Description", "") or ""),
            "severity": "informational",   # TODO: map properly
            "raw": finding,
        })

    def from_ticket(self, ticket: Ticket) -> dict[str, object]:
        """Map a Ticket IR to a vendor-native payload."""
        return {
            "Title": ticket.title,
            "Description": ticket.description,
        }

    def fetch_new(self, since: datetime | None = None) -> list[dict[str, object]]:
        """Return raw vendor payloads updated after ``since``."""
        # TODO: implement pagination + since filter
        response = self._client.list_items()
        return list(response.get("Items", []))

    def write(self, ticket: Ticket) -> str:
        """Write a ticket and return the vendor-assigned ID.

        Raise NotImplementedError if the system does not support writes.
        """
        raise NotImplementedError("<SystemName> is read-only")
```

### Rules for the implementation

1. **Inject the client** — never import boto3 / httpx / requests at the
   module level with a hard dependency.  Accept a client in `__init__`.
   This keeps unit tests fast and credential-free.

2. **Never mutate `raw`** — `to_ticket` receives a dict; copy it before
   any modifications.  The caller may reuse the same dict.

3. **Preserve `raw`** — always set `"raw": finding` so callers have access
   to fields the IR does not model.

4. **Use UTC datetimes** — parse all timestamps to timezone-aware UTC
   `datetime` objects.  A helper pattern:

   ```python
   def _parse_utc(ts: str | None) -> datetime:
       if not ts:
           return datetime.now(timezone.utc)
       normalised = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
       dt = datetime.fromisoformat(normalised)
       return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
   ```

5. **Paginate** — never assume the first API response contains all items.
   Follow `NextToken` / `pageToken` / offset pagination until exhausted.

6. **Read-only? Raise, don't silently drop** — if the system cannot be
   written to, raise `NotImplementedError` with a helpful message.

7. **Type everything** — all functions must be typed for mypy `--strict`.
   No `Any` return types on public methods.

---

## Step 2 — write unit tests (no real credentials)

Create `tests/test_adapters_<system_name>.py`.

Unit tests must cover:

| Category | Tests to write |
|----------|---------------|
| `to_ticket` — field mapping | Every field in the Ticket IR that the vendor populates |
| `to_ticket` — severity mapping | All severity bands / labels |
| `to_ticket` — status mapping | All status values the vendor can return |
| `to_ticket` — tags and entities | At least one positive and one absent-field case |
| `to_ticket` — edge cases | Missing required fields, empty responses, None values |
| `from_ticket` | Returns expected dict keys; round-trip fidelity |
| `write` | Raises `NotImplementedError` if read-only; otherwise calls the right API method |
| `fetch_new` | Pagination (2+ pages), `since` filter is passed to API, empty response |

### Test pattern (stub client)

```python
from unittest.mock import MagicMock
import pytest

from ticketsync.adapters.<system_name> import <SystemName>Adapter


def make_stub_client(items: list[dict] | None = None) -> MagicMock:
    client = MagicMock()
    client.list_items.return_value = {
        "Items": items or [],
        "NextToken": "",
    }
    return client


def make_adapter(client: MagicMock | None = None) -> <SystemName>Adapter:
    return <SystemName>Adapter(client=client or make_stub_client())


class TestToTicket:
    def test_source_id_mapped(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket({"Id": "abc", "Title": "Test", ...})
        assert ticket.source_id == "abc"

    # ... more tests ...


class TestFetchNew:
    def test_pagination_followed(self) -> None:
        client = MagicMock()
        client.list_items.side_effect = [
            {"Items": [{"Id": "1"}], "NextToken": "tok2"},
            {"Items": [{"Id": "2"}], "NextToken": ""},
        ]
        adapter = make_adapter(client=client)
        result = adapter.fetch_new()
        assert client.list_items.call_count == 2
        assert len(result) == 2
```

### Naming conventions

- File: `tests/test_adapters_<system_name>.py`
- Classes: `TestToTicket`, `TestFromTicket`, `TestWrite`, `TestFetchNew`, `TestEdgeCases`
- Each test class groups logically related assertions

---

## Step 3 — write integration tests (real credentials, marked)

Create a class in `tests/test_integration_aws.py` (for AWS adapters) or
`tests/test_integration_github.py` (for GitHub), or a new file for other systems.

Integration tests must:
- Be decorated with `@pytest.mark.integration` (or `pytestmark = [pytest.mark.integration, ...]`)
- Skip gracefully when credentials are absent (use `pytest.mark.skipif`)
- Create any test resources with `ticketsync-test-` prefix
- Tag test resources with `Purpose=test, ManagedBy=zoo-agent`
- Delete / archive / close all test resources in the same test (or in teardown)
- Not be run by default CI (CI uses `-m 'not integration'`)

```python
import pytest
import os

_CREDS_AVAILABLE = bool(os.environ.get("AWS_ACCESS_KEY_ID"))

@pytest.mark.integration
@pytest.mark.skipif(not _CREDS_AVAILABLE, reason="No AWS credentials")
class TestMySystemIntegration:
    def test_fetch_returns_items(self) -> None:
        from ticketsync.adapters.<system_name> import <SystemName>Adapter
        import boto3

        adapter = <SystemName>Adapter(
            client=boto3.client("mysystem", region_name="us-east-1"),
        )
        result = adapter.fetch_new()
        assert isinstance(result, list)
```

---

## Step 4 — register the adapter

Add to `ticketsync/adapters/__init__.py`:

```python
from ticketsync.adapters.<system_name> import <SystemName>Adapter  # noqa: E402

ADAPTER_REGISTRY["<system_name>"] = <SystemName>Adapter

# Also add to __all__:
__all__ = [
    ...
    "<SystemName>Adapter",
]
```

The registry key (e.g. `"<system_name>"`) must match the `type:` value users
put in their YAML config.

---

## Step 5 — document the adapter

Add a section to `docs/ADAPTERS.md` following the existing format:
- System name, direction (read / write / read+write)
- Required IAM actions or auth credentials
- `Config example` YAML block
- `Constructor` Python block
- `Field mapping (to_ticket)` table with all mapped fields
- `Field mapping (from_ticket / write)` table (if write is supported)
- `Invariants` — any behavior that isn't obvious from the tables

---

## Step 6 — verify before submitting

Run the full test suite with mypy:

```bash
# Build and run in Docker (matches CI exactly)
docker build -f Dockerfile.test -t ticketsync-test:latest .
docker run --rm ticketsync-test:latest

# Expected output:
# Success: no issues found in N source files
# N passed, 1 skipped, M deselected in X.Xs
```

Check:
- [ ] All unit tests pass
- [ ] mypy `--strict` reports zero errors
- [ ] No test relies on real credentials (all stubs / mocks)
- [ ] `ADAPTER_REGISTRY` entry added
- [ ] `docs/ADAPTERS.md` section written

---

## Common mistakes

### `system_name` is an instance attribute, not a class attribute

The Protocol requires `system_name: str` as a class-level annotation, but the
actual value should be set in `__init__` so different instances can use different
labels:

```python
class MyAdapter:
    system_name: str   # Protocol requires this annotation

    def __init__(self, ..., system_name: str = "my_system") -> None:
        self.system_name = system_name   # set on instance
```

### `to_ticket` must not raise for absent optional fields

If a vendor payload omits optional fields (description, region, account_id),
the adapter must supply a safe default — not raise `KeyError`.

```python
# Wrong:
description = finding["Description"]

# Correct:
description = str(finding.get("Description", "") or "")
```

### Timestamps without timezone info

Always produce timezone-aware UTC datetimes.  Naive datetimes cause failures
downstream when the engine computes the `since` cutoff.

```python
# Wrong:
datetime.utcnow()

# Correct:
datetime.now(timezone.utc)
```

### Don't hardcode the client import

Unit tests inject stub clients.  If you import `boto3` at the top of the
adapter module (not just in `__init__`), tests will fail unless boto3 is
installed.  Always accept the client as a constructor parameter.
