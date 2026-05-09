# TicketSync

Bidirectional ticket synchronization across ITSM, monitoring, and security systems.

[![CI](https://github.com/LeonardoSanBenitez/TicketSync/actions/workflows/ci.yml/badge.svg)](https://github.com/LeonardoSanBenitez/TicketSync/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ticketsync)](https://pypi.org/project/ticketsync/)
[![Python](https://img.shields.io/pypi/pyversions/ticketsync)](https://pypi.org/project/ticketsync/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

TicketSync is a Python library, not a platform. You `pip install` it and use it in code.
No daemons, no YAML platform to self-host, no vendor lock-in.

---

## Install

```bash
pip install ticketsync
```

For AWS adapters (CloudWatch, GuardDuty, Security Hub, OpsCenter):

```bash
pip install "ticketsync[integration]"
```

Requires Python 3.10+.

---

## Quickstart — CloudWatch alarms into OpsCenter in 15 lines

```python
import boto3
from ticketsync.adapters.cloudwatch_alarms import CloudWatchAlarmsAdapter
from ticketsync.adapters.opscenter import OpsCenterAdapter
from ticketsync.config import SyncConfig
from ticketsync.engine import SyncEngine

source = CloudWatchAlarmsAdapter(
    client=boto3.client("cloudwatch", region_name="us-east-1"),
    state_filter=["ALARM"],
)
dest = OpsCenterAdapter(
    client=boto3.client("ssm", region_name="us-east-1"),
)
config = SyncConfig.from_dict({
    "source": {"type": "cloudwatch_alarms"},
    "destination": {"type": "opscenter"},
    "deduplication": True,
    "lookback_hours": 24,
})

engine = SyncEngine(source=source, dest=dest, config=config)
result = engine.run()
print(f"Synced {result.written} alarms to OpsCenter")
```

---

## Adapter table

| Adapter class | System | Direction | Required IAM / auth |
|---------------|--------|-----------|---------------------|
| `CloudWatchAlarmsAdapter` | AWS CloudWatch Alarms | read | `cloudwatch:DescribeAlarms` |
| `GuardDutyFindingsAdapter` | AWS GuardDuty Findings | read | `guardduty:ListFindings`, `guardduty:GetFindings` |
| `SecurityHubFindingsAdapter` | AWS Security Hub (ASFF) | read | `securityhub:GetFindings` |
| `OpsCenterAdapter` | AWS Systems Manager OpsCenter | read/write | `ssm:GetOpsItem`, `ssm:CreateOpsItem`, `ssm:UpdateOpsItem` |
| `GitHubIssuesAdapter` | GitHub Issues REST API | read/write | GitHub PAT with `repo` scope |
| `LocalFilesystemAdapter` | Local JSON files | read/write | None |

---

## Config YAML reference

```yaml
source:
  type: cloudwatch_alarms      # adapter type key (see table above)
  # any extra keys are passed as kwargs to the adapter constructor

destination:
  type: opscenter
  region: us-east-1

deduplication: true            # skip tickets already written to destination
lookback_hours: 24             # only sync tickets updated in last N hours (0 = all)
```

Load config from a YAML string, file path, or open file object:

```python
from ticketsync.config import SyncConfig

config = SyncConfig.from_yaml("path/to/sync.yaml")
# or
config = SyncConfig.from_dict({"source": {...}, "destination": {...}})
```

---

## Example — GuardDuty findings into GitHub Issues

```python
import boto3
import httpx
from ticketsync.adapters.guardduty import GuardDutyFindingsAdapter
from ticketsync.adapters.github_issues import GitHubIssuesAdapter
from ticketsync.config import SyncConfig
from ticketsync.engine import SyncEngine


class HttpxClient:
    """Thin httpx wrapper matching the GitHubIssuesAdapter client interface."""

    def __init__(self, token: str) -> None:
        self._headers = {"Authorization": f"Bearer {token}"}

    def get(self, url: str, params: dict | None = None) -> object:
        return httpx.get(url, params=params, headers=self._headers).json()

    def post(self, url: str, json: dict | None = None) -> dict:
        return httpx.post(url, json=json, headers=self._headers).json()

    def patch(self, url: str, json: dict | None = None) -> dict:
        return httpx.patch(url, json=json, headers=self._headers).json()


source = GuardDutyFindingsAdapter(
    client=boto3.client("guardduty", region_name="us-east-1"),
    detector_id="your-detector-id",
)
dest = GitHubIssuesAdapter(
    client=HttpxClient("ghp_your_token"),
    owner="your-org",
    repo="security-issues",
)
config = SyncConfig.from_dict({
    "source": {"type": "guardduty"},
    "destination": {"type": "github_issues"},
    "deduplication": True,
    "lookback_hours": 6,
})

result = SyncEngine(source=source, dest=dest, config=config).run()
print(f"Opened {result.written} GitHub issues from GuardDuty findings")
```

---

## Core data model

Every ticket, alarm, finding, and issue is normalized to the `Ticket` IR before
moving between adapters:

```python
from ticketsync.models import Ticket

ticket = Ticket(
    source_system="guardduty",
    source_id="abc123",
    title="Suspicious API call from known malicious IP",
    description="GuardDuty detected an unusual API call pattern.",
    severity="high",        # critical / high / medium / low / informational
    status="open",          # open / in_progress / resolved / closed
    category="Recon:EC2/Portscan",
    tags=["region:us-east-1", "product:GuardDuty"],
)
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full field reference.

---

## Documentation

- [docs/ADAPTERS.md](docs/ADAPTERS.md) — required permissions, field mapping tables, and config examples for every adapter
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — Ticket IR fields, Protocol interface, SyncEngine behavior, deduplication logic
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — how to write a new adapter, test patterns, registry entry

---

## License

Apache 2.0
