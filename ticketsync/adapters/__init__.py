"""Built-in adapters for TicketSync.

Importing this package registers all built-in adapters into the global
``ADAPTER_REGISTRY``.  Callers can also register their own adapters::

    from ticketsync.adapters import ADAPTER_REGISTRY
    ADAPTER_REGISTRY["my_system"] = MyAdapter

The registry maps short names (the ``type`` field in YAML config) to adapter
*classes* (not instances).  The ``SyncEngine`` instantiates adapters from the
registry using the options dict from ``AdapterConfig``.
"""

from __future__ import annotations

from typing import Any, Type

from ticketsync.adapter import TicketAdapter

# Maps adapter type names to their classes.
# Populated by importing the adapter modules below.
ADAPTER_REGISTRY: dict[str, Type[Any]] = {}

from ticketsync.adapters.local import LocalFilesystemAdapter  # noqa: E402
from ticketsync.adapters.opscenter import OpsCenterAdapter  # noqa: E402
from ticketsync.adapters.github_issues import GitHubIssuesAdapter  # noqa: E402

ADAPTER_REGISTRY["local"] = LocalFilesystemAdapter
ADAPTER_REGISTRY["opscenter"] = OpsCenterAdapter
ADAPTER_REGISTRY["github_issues"] = GitHubIssuesAdapter

__all__ = [
    "ADAPTER_REGISTRY",
    "LocalFilesystemAdapter",
    "OpsCenterAdapter",
    "GitHubIssuesAdapter",
]
