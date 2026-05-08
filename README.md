# TicketSync

**Bidirectional ticket synchronization across ITSM and issue-tracking systems.**

Part of the [Libre Ticket Suite](https://github.com/LeonardoSanBenitez) — a loosely coupled set of open-source Python libraries for automating ticket, alert, and issue handling.

[![CI](https://github.com/LeonardoSanBenitez/TicketSync/actions/workflows/ci.yml/badge.svg)](https://github.com/LeonardoSanBenitez/TicketSync/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ticketsync)](https://pypi.org/project/ticketsync/)
[![Python](https://img.shields.io/pypi/pyversions/ticketsync)](https://pypi.org/project/ticketsync/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)


## Overview

TicketSync provides a structured IR (Intermediate Representation) and sync engine for moving tickets between ITSM, alerting, and issue-tracking systems. It is designed to be:

- **Adapter-based**: each system is a pluggable adapter implementing a simple protocol.
- **Config-driven**: a single YAML file defines sources, destinations, and sync behaviour.
- **Type-safe**: fully typed with `pydantic` v2 models; mypy --strict passes.


## Install

```bash
pip install ticketsync
```

Requires Python 3.10+.


## Quick start

```python
from ticketsync import SyncEngine, SyncConfig

config = SyncConfig.from_yaml("sync.yaml")
engine = SyncEngine(config)
result = engine.run()
print(f"Synced {result.synced} tickets, {result.skipped} skipped, {result.errors} errors")
```

### Minimal `sync.yaml`

```yaml
source:
  adapter: local
  path: ./tickets/source

destination:
  adapter: local
  path: ./tickets/destination
```


## Supported adapters

| Adapter | Direction | Status |
|---------|-----------|--------|
| `local` | read / write | stable |
| `github_issues` | read | stable |
| `opscenter` | write | stable |

Adapters are importable from `ticketsync.adapters`:

```python
from ticketsync.adapters import LocalFilesystemAdapter, ADAPTER_REGISTRY
```


## Core data model

Every ticket is normalised to the `Ticket` IR before being passed between adapters:

```python
from ticketsync import Ticket, SeverityLevel, TicketStatus

ticket = Ticket(
    id="acme-42",
    title="Disk usage critical on web-01",
    severity=SeverityLevel.HIGH,
    status=TicketStatus.OPEN,
)
```

Rich entity types (`AccountEntity`, `HostEntity`, `IpAddressEntity`, `UrlEntity`, `FileEntity`, `ProcessEntity`) can be attached to tickets for richer context.


## Writing a custom adapter

```python
from ticketsync import TicketAdapter, Ticket
from typing import Iterator

class MyAdapter(TicketAdapter):
    def read(self) -> Iterator[Ticket]:
        yield Ticket(id="1", title="example", ...)

    def write(self, ticket: Ticket) -> None:
        print(f"Writing: {ticket.title}")
```


## License

Apache 2.0
