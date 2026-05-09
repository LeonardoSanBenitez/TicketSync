# Adapter Reference

This document describes every built-in TicketSync adapter: what system it connects
to, what IAM permissions or credentials it requires, how it maps vendor fields to the
Ticket IR, and how to configure it.

---

## LocalFilesystemAdapter

**System**: Local filesystem (JSON files)
**Direction**: read / write
**Auth**: none

Stores each ticket as a JSON file in a directory.  Useful for testing, debugging, and
as a temporary destination while building a pipeline.

### Config example

```yaml
type: local
path: /var/ticketsync/local-dest
```

### Constructor

```python
from pathlib import Path
from ticketsync.adapters.local import LocalFilesystemAdapter

adapter = LocalFilesystemAdapter(path=Path("/var/ticketsync/local-dest"))
```

### Field mapping (to_ticket)

| File JSON field | Ticket IR field | Notes |
|-----------------|-----------------|-------|
| `source_system` | `source_system` | preserved verbatim |
| `source_id` | `source_id` | preserved verbatim |
| `title` | `title` | preserved verbatim |
| All other Ticket fields | same | round-trip lossless |

### Invariants

- Filenames are derived from `source_system` + `source_id` with unsafe characters
  replaced by underscores.  Filenames are capped at 200 characters.
- `write` is idempotent: writing the same ticket twice overwrites the file in place.
- `fetch_new(since=dt)` filters on the `updated_at` field stored in the JSON.

---

## OpsCenterAdapter

**System**: AWS Systems Manager OpsCenter
**Direction**: read / write
**Auth**: IAM permissions

### Required IAM actions

```json
{
  "Effect": "Allow",
  "Action": [
    "ssm:GetOpsItem",
    "ssm:CreateOpsItem",
    "ssm:UpdateOpsItem",
    "ssm:DescribeOpsItems"
  ],
  "Resource": "*"
}
```

### Config example

```yaml
type: opscenter
region: us-east-1
```

### Constructor

```python
import boto3
from ticketsync.adapters.opscenter import OpsCenterAdapter

adapter = OpsCenterAdapter(
    client=boto3.client("ssm", region_name="us-east-1"),
    region="us-east-1",
    system_name="opscenter",   # overrides source_system label
)
```

### Field mapping (to_ticket)

| OpsCenter field | Ticket IR field | Notes |
|-----------------|-----------------|-------|
| `OpsItemId` | `source_id` | e.g. `oi-abc123` |
| `Title` | `title` | |
| `Description` | `description` | |
| `Severity` ("1"–"4") | `severity` | "1"=critical, "2"=high, "3"=medium, "4"=low |
| `Status` | `status` | Open→open, InProgress→in_progress, Resolved→resolved |
| `CreatedTime` | `created_at` | |
| `LastModifiedTime` | `updated_at` | |
| `Source` | `tags` | prepended as `"source:<value>"` |
| `OperationalData["/ticketsync/assignee"]` | `assignee` | stored as hidden SearchableString |

### Field mapping (from_ticket / write)

| Ticket IR field | OpsCenter field | Notes |
|-----------------|-----------------|-------|
| `title` | `Title` | |
| `description` | `Description` | empty string → `" "` (OpsCenter rejects empty) |
| `severity` | `Severity` | critical→"1", high→"2", medium→"3", low→"4", informational→"4" |
| `status` | `Status` | open→Open, in_progress→InProgress, resolved/closed→Resolved |
| `tags[source:*]` | `Source` | first `source:` tag extracted; defaults to `"ticketsync"` |
| `assignee` | `OperationalData["/ticketsync/assignee"]` | hidden SearchableString; empty → omitted |
| `source_system` | `OperationalData["/ticketsync/source_system"]` | hidden; for dedup lookup |
| `source_id` | `OperationalData["/ticketsync/source_id"]` | hidden; for dedup lookup |

### Invariants

- `write` routes to `CreateOpsItem` when `source_id` does not start with `oi-`; to
  `UpdateOpsItem` when `source_id` is a valid OpsCenter ID (`oi-*`).
- Returns the OpsItem ID string.
- OpsCenter has no "closed" state — tickets with `status=closed` are mapped to
  `Resolved`.

### Assignee field

OpsCenter has no first-class assignee field.  The adapter stores `ticket.assignee`
in `OperationalData` under the key `/ticketsync/assignee` as a `SearchableString`.
This field is invisible in the normal OpsCenter console view but is readable
programmatically and via `to_ticket`.

### Sync write-back (tag-based dedup)

`mark_synced(source_id)` adds `/ticketsync/synced = "true"` to `OperationalData`.

### Destination-check dedup

`find_by_source_coordinates(source_system, source_id)` queries
`describe_ops_items` with `OperationalData` filters on the source coordinates.

---

## GitHubIssuesAdapter

**System**: GitHub Issues (REST API v3)
**Direction**: read / write
**Auth**: GitHub Personal Access Token with `repo` scope

The adapter uses an injected HTTP client (not a hard dependency on httpx or requests)
so you can bring your own HTTP library.  The client must implement:

```python
def get(self, url: str, params: dict | None = None) -> list | dict: ...
def post(self, url: str, json: dict | None = None) -> dict: ...
def patch(self, url: str, json: dict | None = None) -> dict: ...
```

### Config example

```yaml
type: github_issues
owner: your-org
repo: your-repo
```

### Constructor

```python
from ticketsync.adapters.github_issues import GitHubIssuesAdapter

adapter = GitHubIssuesAdapter(
    client=your_http_client,
    owner="your-org",
    repo="your-repo",
    system_name="github_issues",
)
```

### Field mapping (to_ticket)

| GitHub Issue field | Ticket IR field | Notes |
|-------------------|-----------------|-------|
| `number` | `source_id` | stored as string |
| `title` | `title` | |
| `body` | `description` | null body becomes empty string |
| `labels[].name` matching `severity:<level>` | `severity` | first match wins; default "informational" |
| `labels[].name` not matching `severity:*` or `ticketsync:*` | `tags` | tracking labels are filtered out |
| `state` ("open"/"closed") | `status` | open→open, closed→resolved |
| `created_at` | `created_at` | ISO-8601 string |
| `updated_at` | `updated_at` | ISO-8601 string |
| `html_url` | `external_url` | |
| `assignees[0].login` | `assignee` | first assignee only; empty if none |

### Field mapping (from_ticket / write)

| Ticket IR field | GitHub payload field | Notes |
|-----------------|---------------------|-------|
| `title` | `title` | |
| `description` | `body` | |
| `severity` (if not informational) | `labels` | adds `severity:<level>` label |
| `tags` | `labels` | added verbatim |
| `status` (resolved/closed) | `state` | "closed"; otherwise "open" |
| `assignee` | `assignees` | single-element list; GitHub silently ignores non-collaborators |

### write routing

The adapter uses a **label-based lookup** to decide whether to create or update:

1. Search for an issue with label `ticketsync:source_id=<source_system>/<source_id>`.
2. If found: `PATCH /issues/{number}` (update).
3. If not found: `POST /issues` (create), and the tracking label is included.

This is correct for cross-adapter pipelines.  The previous `source_id.isdigit()`
heuristic was wrong: a numeric OpsCenter ID (e.g. `"1"`) would incorrectly PATCH
GitHub issue #1.

### Assignee field

`ticket.assignee` is included as `assignees: [username]` in the create/update
payload.  GitHub silently ignores assignees who are not collaborators on the repo;
TicketSync does not verify membership before writing.

### Invariants

- Labels must exist on the repo before `write` can apply them; create them via the
  GitHub API first.
- The `since` parameter in `fetch_new(since=dt)` is passed directly to the GitHub API
  `?since=` query parameter (ISO-8601 format).
- Tracking labels (`ticketsync:source_id=*`) are stripped from `tags` in `to_ticket`.

### Sync write-back (tag-based dedup)

`mark_synced(source_id)` adds the label `ticketsync:synced` to the issue via the
Labels API.

### Destination-check dedup

`find_by_source_coordinates(source_system, source_id)` searches issues by the
tracking label `ticketsync:source_id=<system>/<id>`.

### Triage metadata

`write_metadata(source_id, metadata)` posts a structured human-readable comment
(GitHub Issues has no native hidden field store).  The comment is prefixed with
an HTML comment marker so tooling can locate it without confusing human readers.
`read_metadata(source_id)` scans issue comments for the most recent metadata comment.

---

## CloudWatchAlarmsAdapter

**System**: AWS CloudWatch Alarms
**Direction**: read only (write raises `NotImplementedError`)
**Auth**: IAM permissions

CloudWatch alarm state is managed by AWS metric evaluation.  TicketSync reads alarms
but does not modify them.

### Required IAM actions

```json
{
  "Effect": "Allow",
  "Action": ["cloudwatch:DescribeAlarms"],
  "Resource": "*"
}
```

### Config example

```yaml
type: cloudwatch_alarms
region: us-east-1
state_filter:
  - ALARM
  - INSUFFICIENT_DATA
```

### Constructor

```python
import boto3
from ticketsync.adapters.cloudwatch_alarms import CloudWatchAlarmsAdapter

adapter = CloudWatchAlarmsAdapter(
    client=boto3.client("cloudwatch", region_name="us-east-1"),
    region="us-east-1",
    state_filter=["ALARM", "INSUFFICIENT_DATA"],  # None = all states
)
```

### Field mapping (to_ticket)

| CloudWatch field | Ticket IR field | Notes |
|-----------------|-----------------|-------|
| `AlarmName` | `source_id` | |
| `[Namespace] MetricName: AlarmName` | `title` | constructed from metadata |
| `AlarmDescription` | `description` | |
| `StateValue` | `severity` | ALARM→high, INSUFFICIENT_DATA→medium, OK→informational |
| `StateValue` | `status` | ALARM→open, INSUFFICIENT_DATA→open, OK→resolved |
| `AlarmArn` | `external_url` | |
| `StateUpdatedTimestamp` | `updated_at` | |
| `AlarmConfigurationUpdatedTimestamp` | `created_at` | best available proxy |
| `Namespace` | `tags` | as `namespace:<value>` |
| `MetricName` | `tags` | as `metric:<value>` |

### Invariants

- `describe_alarms` does not support native time filtering; TicketSync applies
  `since` filtering client-side on `StateUpdatedTimestamp`.
- Only `MetricAlarm` types are returned (not composite alarms).

---

## GuardDutyFindingsAdapter

**System**: AWS GuardDuty Findings
**Direction**: read only (write raises `NotImplementedError`)
**Auth**: IAM permissions

GuardDuty findings are immutable via the findings API.  Use `archive_findings` or
`update_findings_feedback` directly to change finding state.

### Required IAM actions

```json
{
  "Effect": "Allow",
  "Action": [
    "guardduty:ListFindings",
    "guardduty:GetFindings"
  ],
  "Resource": "*"
}
```

### Config example

```yaml
type: guardduty
detector_id: f0cec0ab8c944e358f7992966bdb3605
region: us-east-1
batch_size: 50
```

### Constructor

```python
import boto3
from ticketsync.adapters.guardduty import GuardDutyFindingsAdapter

adapter = GuardDutyFindingsAdapter(
    client=boto3.client("guardduty", region_name="us-east-1"),
    detector_id="your-detector-id",
    region="us-east-1",
    batch_size=50,   # max 50 (GuardDuty API limit per get_findings call)
)
```

### Field mapping (to_ticket)

| GuardDuty field | Ticket IR field | Notes |
|----------------|-----------------|-------|
| `Id` | `source_id` | 32-character hex string |
| `Title` | `title` | |
| `Description` | `description` | |
| `Severity` (1.0–10.0 float) | `severity` | 8–10→critical, 5–7.9→high, 2–4.9→medium, 1–1.9→low |
| `Service.Archived` (bool) | `status` | True→closed, False→open |
| `Type` | `category` | e.g. `Recon:EC2/Portscan` |
| `Arn` | `external_url` | |
| `CreatedAt` | `created_at` | ISO-8601 string |
| `UpdatedAt` | `updated_at` | ISO-8601 string |
| `AccountId` | `entities` | `AccountEntity(uid=account_id, type="aws")` |
| `Region` | `tags` | as `region:<value>` |
| `Service.Count` | `tags` | as `count:<N>` (omitted if 0) |

### Severity score mapping

```
GuardDuty score  TicketSync severity
8.0 – 10.0    -> critical
5.0 – 7.9     -> high
2.0 – 4.9     -> medium
1.0 – 1.9     -> low
```

### fetch_new behavior

- Calls `list_findings` (with optional `FindingCriteria`) to get all IDs, then
  calls `get_findings` in batches of `batch_size` (max 50 per AWS API limit).
- Paginates both API calls automatically via `NextToken`.
- When `since` is provided, passes an `updatedAt GreaterThan` criterion to
  `list_findings` (epoch milliseconds).

---

## SecurityHubFindingsAdapter

**System**: AWS Security Hub (ASFF format)
**Direction**: read only (write raises `NotImplementedError`)
**Auth**: IAM permissions

Security Hub findings follow the Amazon Security Finding Format (ASFF).  GuardDuty,
Inspector, and other AWS services publish findings to Security Hub automatically when
integrations are enabled.

### Required IAM actions

```json
{
  "Effect": "Allow",
  "Action": ["securityhub:GetFindings"],
  "Resource": "*"
}
```

### Config example

```yaml
type: securityhub
region: us-east-1
filters:
  ProductName:
    - Value: GuardDuty
      Comparison: EQUALS
```

### Constructor

```python
import boto3
from ticketsync.adapters.securityhub import SecurityHubFindingsAdapter

adapter = SecurityHubFindingsAdapter(
    client=boto3.client("securityhub", region_name="us-east-1"),
    region="us-east-1",
    filters={
        # Optional ASFF filters passed to get_findings
        "ProductName": [{"Value": "GuardDuty", "Comparison": "EQUALS"}],
        "WorkflowStatus": [{"Value": "NEW", "Comparison": "EQUALS"}],
    },
)
```

### Field mapping (to_ticket)

| ASFF field | Ticket IR field | Notes |
|-----------|-----------------|-------|
| `Id` | `source_id` | full ARN |
| `Title` | `title` | fallback: "Security Hub Finding" |
| `Description.Text` or `Description` (str) | `description` | |
| `Severity.Label` | `severity` | CRITICAL→critical, HIGH→high, MEDIUM→medium, LOW→low, INFORMATIONAL→informational |
| `Workflow.Status` | `status` | NEW/NOTIFIED→open, SUPPRESSED→closed, RESOLVED→resolved |
| `Types[0]` | `category` | first entry in the Types array |
| `ProductArn` | `external_url` | |
| `CreatedAt` | `created_at` | ISO-8601 string |
| `UpdatedAt` | `updated_at` | ISO-8601 string |
| `AwsAccountId` | `entities` | `AccountEntity(uid=account_id, type="aws")` |
| `Region` | `tags` | as `region:<value>` |
| `ProductName` | `tags` | as `product:<value>` |
| `CompanyName` | `tags` | as `company:<value>` |

### Severity mapping

```
ASFF Severity.Label  TicketSync severity
CRITICAL          -> critical
HIGH              -> high
MEDIUM            -> medium
LOW               -> low
INFORMATIONAL     -> informational
(missing/unknown) -> informational
```

### Workflow status mapping

```
ASFF Workflow.Status  TicketSync status
NEW                -> open
NOTIFIED           -> open
SUPPRESSED         -> closed
RESOLVED           -> resolved
(missing/unknown)  -> open
```

### fetch_new behavior

- Calls `get_findings` with up to 100 results per page; follows `NextToken`.
- Constructor `filters` dict is merged with any additional filters derived from `since`.
- When `since` is provided, adds an `UpdatedAt` date-range filter starting at `since`.
- Filters for different keys are merged; filters for the same key are concatenated as lists.
