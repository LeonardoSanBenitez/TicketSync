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
| `informational` | Default for non-security sources or when severity is unknown/unclassified |

**Convention for non-security sources**: adapters for systems that do not have a
severity concept (e.g. GitHub Issues with no `severity:` label, OpsCenter items
without a Severity field) use `"informational"` as the default.  This means
`"informational"` can signify either "explicitly no action needed" or "severity
not provided by source" — callers must not assume it means the event is benign.
If you need to distinguish these cases, check `ticket.raw` for the original source
severity field.

### Status values

| Value | Meaning | Produced by |
|-------|---------|-------------|
| `open` | Newly created, not yet assigned | all adapters |
| `in_progress` | Being worked on | OpsCenter InProgress |
| `resolved` | Work complete, awaiting closure | GitHub closed, OpsCenter Resolved, GuardDuty Archived=False |
| `closed` | Permanently closed | SecurityHub SUPPRESSED workflow status |

**`closed` vs `resolved`**: these are distinct states.  `closed` specifically maps
from SecurityHub's SUPPRESSED workflow status, which means the finding is suppressed
as a false positive or known exception — not the same as a remediated finding.  Most
other adapters use `resolved` for the terminal state.

**OpsCenter write note**: OpsCenter has no `Closed` state; tickets with
`status=closed` are mapped to `Resolved` on write.

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

### OCSF entity temporal context limitation

The `entities` field captures entities referenced *in* the alert (e.g. which account,
host, or IP was involved).  It does **not** capture when those entities were observed
or how their state changed over time.  OCSF defines a richer temporal model
(`observed_at`, entity timeline events) that TicketSync deliberately does not
implement — TicketSync is ticket-centric, not event-centric.

Consequence: if a GuardDuty finding references an IP address observed at a specific
time, the `IpAddressEntity` captures the IP but not the observation timestamp.  That
timestamp is available in `ticket.raw` (the full vendor payload).  Callers who need
temporal entity context should inspect `ticket.raw` directly.

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

### Deduplication strategies

Three strategies are available via `config.dedup_strategy`:

**`time-based`** (default, stateless)
- The engine passes a `since` cutoff from `lookback_hours` to `fetch_new`.
- **Cross-run guarantee: NO.**  The in-process dedup set is not persisted between
  process restarts.  A fresh process will re-fetch and re-write anything within the
  lookback window.  This is process-local deduplication only.
- Best for pipelines where the destination is idempotent (LocalFS, OpsCenter).

**`tag-based`** (write-back to source)
- After a successful write, calls `source.mark_synced(source_id)`.
- **Cross-run guarantee: YES**, if the source adapter supports `mark_synced` and
  the tag/label persists between runs.
- Read-only adapters (CloudWatch, GuardDuty, SecurityHub) do not implement
  `mark_synced`; the engine logs a warning and skips the write-back without
  failing the run.

**`destination-check`** (lookup before write)
- Before each write, calls `dest.find_by_source_coordinates(source_system, source_id)`.
- **Cross-run guarantee: YES**, as long as the destination retains source coordinates.
- Slowest (one extra API call per ticket), but most reliable.

**In-process dedup** (always active)
- Regardless of strategy, the engine tracks `(source_system, source_id)` pairs
  written in the current run.  If `fetch_new` returns the same ticket twice,
  only the first write is executed.
- **This set is cleared at the start of each `run()` call by default.**
- Use `run(clear_cache=False)` to preserve it across calls on the same instance.
- Call `engine.reset_dedup_cache()` to clear manually.

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

sync_mode: pull              # only 'pull' supported currently
dedup_strategy: time-based  # time-based | tag-based | destination-check
lookback_hours: 24           # used when dedup_strategy is time-based
deduplication: true          # in-process dedup (always active)
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

## Triage metadata

The `TriageMetadata` model (`ticketsync.metadata`) captures analyst decisions
made during triage: assignee, priority, severity override, notes, and resolution.

### Design principle

> System-native metadata is primary.  Structured comments are the fallback.

- OpsCenter stores metadata in `OperationalData` (hidden keys under `/ticketsync/triage/*`,
  invisible in the normal console UI, queryable via filters).
- GitHub stores metadata as a structured human-readable comment because GitHub Issues
  has no native hidden field.  The comment format is designed to be understandable
  by any developer who encounters it.
- Never pollute the main description or visible comment thread with raw JSON blobs
  when a hidden field is available.

### Optional extension protocol

```python
class MetadataAdapter(Protocol):
    def write_metadata(self, source_id: str, metadata: TriageMetadata) -> None: ...
    def read_metadata(self, source_id: str) -> TriageMetadata | None: ...
```

Adapters that don't support metadata storage simply don't implement this protocol.
Use `isinstance(adapter, MetadataAdapter)` to check at runtime.

### Adapter support

| Adapter | write_metadata | Storage mechanism |
|---------|---------------|-------------------|
| OpsCenterAdapter | yes | OperationalData (hidden) |
| GitHubIssuesAdapter | yes | Structured comment (human-readable) |
| LocalFilesystemAdapter | no | — |
| CloudWatchAlarmsAdapter | no | read-only |
| GuardDutyFindingsAdapter | no | read-only |
| SecurityHubFindingsAdapter | no | read-only |

### Usage

```python
from ticketsync.metadata import TriageMetadata, write_metadata, read_metadata
from ticketsync.adapters.github_issues import GitHubIssuesAdapter

adapter = GitHubIssuesAdapter(client=client, owner="org", repo="repo")

meta = TriageMetadata(
    assignee="alice@example.com",
    priority=1,
    triage_notes="Confirmed malicious — escalating.",
    resolution="Blocked at perimeter firewall.",
)

# write_metadata returns True if adapter supports it, False otherwise
write_metadata(adapter, source_id="42", metadata=meta)

# read_metadata returns TriageMetadata or None
recovered = read_metadata(adapter, source_id="42")
```

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
