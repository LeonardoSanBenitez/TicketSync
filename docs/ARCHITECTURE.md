# Architecture

This document describes TicketSync's internal design: the Ticket IR, the adapter
Protocol, how SyncEngine runs, and how deduplication works.

It is written for two audiences:
- **Humans** building pipelines or debugging data loss
- **LLMs** being asked to extend TicketSync with new adapters or fix bugs

---

## Overview

```
Source system                TicketSync                   Destination system
────────────────    ──────────────────────────────────    ──────────────────
CloudWatch Alarms ──► CloudWatchAlarmsAdapter.fetch_new()
                  ──► .to_ticket()  ──► Ticket IR ──► .write()  ──► OpsCenterAdapter
GuardDuty Findings──► GuardDutyFindingsAdapter.fetch_new()       ──► GitHubIssuesAdapter
Security Hub      ──► SecurityHubFindingsAdapter.fetch_new()     ──► LocalFilesystemAdapter
GitHub Issues     ──► GitHubIssuesAdapter.fetch_new()
```

TicketSync is a **library, not a platform**. There is no daemon, no config server,
no message queue. You call `SyncEngine.run()` from your own scheduler (cron, Lambda,
Airflow, etc.).

---

## Ticket IR field reference

Every adapter maps its vendor payload into this canonical structure.  All fields are
pydantic v2 validated on construction.

```python
class Ticket(BaseModel):
    # --- Identity ---
    id: str                   # TicketSync-owned UUID (auto-generated, stable across syncs)
    source_system: str        # Short adapter name, e.g. "guardduty", "github_issues"
    source_id: str            # Vendor-native ID, preserved verbatim

    # --- Core content ---
    title: str                # One-line summary (required, min_length=1, stripped)
    description: str          # Longer body (default "")

    # --- Classification ---
    severity: SeverityLevel   # "critical" | "high" | "medium" | "low" | "informational"
    status: TicketStatus      # "open" | "in_progress" | "resolved" | "closed"
    category: str             # Free-text category, e.g. "Recon:EC2/Portscan"
    tags: list[str]           # Deduplicated, order-preserving

    # --- Timestamps (always UTC-aware) ---
    created_at: datetime      # When created in source system (auto-UTC on construction)
    updated_at: datetime      # When last modified in source (auto-UTC on construction)

    # --- Optional enrichment ---
    entities: list[Entity]    # OCSF-inspired entity references (see below)
    remediation_steps: str    # Free-text guidance (default "")
    external_url: str         # Deep-link back into source system (default "")
    assignee: str             # Username or email (default "")

    # --- Passthrough ---
    raw: dict[str, object]    # Full vendor payload, opaque to TicketSync
```

### Severity levels

| Value | Meaning |
|-------|---------|
| `critical` | Immediate action required |
| `high` | Urgent, resolve within hours |
| `medium` | Important, resolve within days |
| `low` | Informational, schedule when convenient |
| `informational` | No action needed; often used as default when severity is absent |

### Status values

| Value | Meaning |
|-------|---------|
| `open` | Newly created, not yet assigned |
| `in_progress` | Being worked on |
| `resolved` | Work complete, awaiting closure |
| `closed` | Permanently closed (may differ from resolved) |

### Entity types

Entities follow the OCSF vocabulary (loosely). The `entities` field is a
discriminated union on the `kind` field:

| `kind` | Required fields | Optional fields |
|--------|-----------------|-----------------|
| `account` | `uid` | `name`, `type` (e.g. "aws", "gcp") |
| `host` | `hostname` | `ip`, `os` |
| `process` | `pid` | `name`, `cmd_line` |
| `ip_address` | `ip` | `version` ("v4"/"v6") |
| `file` | `path` | `hash_sha256` |
| `url` | `url` | `domain` |

Example (Python):
```python
from ticketsync.models import AccountEntity, Ticket

ticket = Ticket(
    source_system="guardduty",
    source_id="abc123",
    title="Unusual API call",
    severity="high",
    entities=[
        AccountEntity(uid="123456789012", name="prod-account", type="aws")
    ],
)
```

### Validators applied on construction

- `title` is stripped of leading/trailing whitespace; blank is rejected
- `tags` is deduplicated (first occurrence wins), all elements must be `str`
- `created_at` / `updated_at`: naive `datetime` → UTC-aware; ISO-8601 strings
  with `Z` suffix are normalised to `+00:00` for Python 3.10 compatibility

### Convenience methods

```python
ticket.is_open()                    # True if status in ("open", "in_progress")
ticket.with_status("resolved")      # Returns new Ticket (immutable update)
ticket.with_assignee("alice@co.com")# Returns new Ticket with new assignee
```

---

## The TicketAdapter Protocol

```python
class TicketAdapter(Protocol):
    system_name: str          # Source/dest label — set on instance, not class

    def to_ticket(self, raw: dict[str, object]) -> Ticket: ...
    def from_ticket(self, ticket: Ticket) -> dict[str, object]: ...
    def fetch_new(self, since: datetime | None = None) -> list[dict[str, object]]: ...
    def write(self, ticket: Ticket) -> str: ...   # returns vendor ID
```

The Protocol is `runtime_checkable`, so you can use `isinstance(adapter, TicketAdapter)`.
Structural (duck-type) conformance is enforced — no inheritance from a base class is needed.

### Contract details

| Method | Must never... | May... |
|--------|--------------|-------|
| `to_ticket` | Mutate `raw` | Include all `raw` keys verbatim in `ticket.raw` |
| `from_ticket` | Throw for unknown fields | Silently drop fields the vendor doesn't support |
| `fetch_new` | Return `None` | Return `[]` if nothing is new |
| `write` | Create duplicates silently | Raise `NotImplementedError` if the system is read-only |

**Read-only adapters** (CloudWatch, GuardDuty, Security Hub) raise `NotImplementedError`
in `write`. This is intentional — these systems manage their own data.

---

## SyncEngine

```
SyncEngine.run(since=None)
  │
  ├─ Compute cutoff: since or (now - lookback_hours), or None
  │
  ├─ source.fetch_new(since=cutoff) → list[raw]
  │    result.fetched = len(raw)
  │
  └─ For each raw item:
       ├─ source.to_ticket(raw) → Ticket
       │    on failure → result.failed++, result.errors.append(...)
       │
       ├─ Dedup check: (source_system, source_id) already in _written_ids?
       │    yes → result.skipped_duplicate++, continue
       │
       └─ dest.write(ticket) → id
            on failure → result.failed++, result.errors.append(...)
            on success → _written_ids.add(...), result.written++
```

### SyncResult fields

```python
@dataclass
class SyncResult:
    fetched: int             # Total raw items returned by fetch_new
    written: int             # Successfully written to destination
    skipped_duplicate: int   # Skipped by in-memory dedup
    failed: int              # Errors in to_ticket or write
    errors: list[dict]       # Per-error dicts with stage/raw/error keys
```

### Deduplication

The engine maintains an in-memory set of `(source_system, source_id)` pairs seen
in the current run. A ticket is skipped if it was already written in the **same run**.

This is **not** cross-run deduplication. The destination adapter is responsible for
idempotent writes across runs (e.g. `LocalFilesystemAdapter` overwrites in place,
`OpsCenterAdapter` updates existing OpsItems).

Call `engine.reset_dedup_cache()` between runs if reusing the same engine instance.

### Lookback window

If `since=None` is passed to `run()` and `config.lookback_hours > 0`, the engine
computes: `since = now - timedelta(hours=lookback_hours)`.

Set `lookback_hours=0` to disable the window entirely (fetch all available data).

---

## Adapter registry

```python
from ticketsync.adapters import ADAPTER_REGISTRY

# Maps type-name strings to adapter classes
ADAPTER_REGISTRY["cloudwatch_alarms"] = CloudWatchAlarmsAdapter
ADAPTER_REGISTRY["guardduty"]         = GuardDutyFindingsAdapter
ADAPTER_REGISTRY["securityhub"]       = SecurityHubFindingsAdapter
ADAPTER_REGISTRY["opscenter"]         = OpsCenterAdapter
ADAPTER_REGISTRY["github_issues"]     = GitHubIssuesAdapter
ADAPTER_REGISTRY["local"]             = LocalFilesystemAdapter
```

The registry is used by the YAML-config path:

```python
source_cls = ADAPTER_REGISTRY[config.source.type]
source = source_cls(**config.source.options)
```

Register your own adapters:
```python
from ticketsync.adapters import ADAPTER_REGISTRY
ADAPTER_REGISTRY["my_system"] = MySystemAdapter
```

---

## Config system

`SyncConfig` is a pydantic model loaded from a YAML string, file path, or dict.

```yaml
source:
  type: cloudwatch_alarms   # ADAPTER_REGISTRY key
  region: us-east-1         # passed as kwargs to adapter constructor

destination:
  type: opscenter
  region: us-east-1

deduplication: true
lookback_hours: 24
```

```python
from ticketsync.config import SyncConfig

# From YAML file
config = SyncConfig.from_yaml("/etc/ticketsync/sync.yaml")

# From dict
config = SyncConfig.from_dict({"source": {...}, "destination": {...}})

# From YAML string
config = SyncConfig.from_yaml_string(yaml_text)
```

`config.source.options` and `config.destination.options` are `dict[str, Any]` — all
YAML keys other than `type` are passed verbatim to the adapter constructor.

---

## Error handling philosophy

- **Per-ticket failures are non-fatal.** `SyncEngine` catches exceptions from
  `to_ticket` and `write` separately, records them in `result.errors`, and
  continues processing remaining tickets.
- **A failed run never silently swallows errors.** Callers can inspect
  `result.failed` and `result.errors` and surface them to their own monitoring.
- **Network failures mid-fetch are not caught.** If `fetch_new` raises (e.g.
  due to a transient network error), the entire run fails. Retry at the scheduler
  level.

---

## Data flow diagram (detailed)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Source system                 SyncEngine                 Dest system │
│                                                                      │
│  vendor API                                              vendor API  │
│     │                                                       ▲        │
│     │ raw JSON/dict                                         │ write  │
│     ▼                                                       │        │
│  fetch_new() ──► [raw, raw, raw...]                         │        │
│                         │                                   │        │
│                  for each raw:                              │        │
│                         │                                   │        │
│                  to_ticket(raw) ──► Ticket IR               │        │
│                         │             │                     │        │
│                         │       dedup check                 │        │
│                         │             │ (pass)              │        │
│                         │             ▼                     │        │
│                         │       dest.write(ticket) ─────────┘        │
│                         │                                            │
│                         │       result.written++                     │
└──────────────────────────────────────────────────────────────────────┘
```
