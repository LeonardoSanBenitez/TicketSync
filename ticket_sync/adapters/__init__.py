"""Source adapters for TicketSync.

Each adapter converts a raw platform-specific webhook payload into the
canonical :class:`ticket_sync.models.Ticket` schema.

Usage::

    from ticket_sync.adapters import get_adapter

    adapter = get_adapter("cloudwatch")
    ticket = adapter.parse(raw_payload)

Or import specific adapters directly::

    from ticket_sync.adapters.cloudwatch import CloudWatchAdapter

    ticket = CloudWatchAdapter().parse(payload)
"""

from __future__ import annotations

from ticket_sync.adapters.base import BaseAdapter
from ticket_sync.adapters.cloudwatch import CloudWatchAdapter
from ticket_sync.adapters.github import GitHubAdapter
from ticket_sync.adapters.jira import JiraAdapter
from ticket_sync.adapters.pagerduty import PagerDutyAdapter

_ADAPTER_REGISTRY: dict[str, type[BaseAdapter]] = {
    "cloudwatch": CloudWatchAdapter,
    "pagerduty": PagerDutyAdapter,
    "jira": JiraAdapter,
    "github": GitHubAdapter,
}


def get_adapter(source: str) -> BaseAdapter:
    """Return an adapter instance for the given source name.

    Args:
        source: One of ``"cloudwatch"``, ``"pagerduty"``, ``"jira"``,
            ``"github"``.

    Returns:
        An instantiated :class:`BaseAdapter` subclass.

    Raises:
        ValueError: If *source* is not a known adapter name.

    >>> from ticket_sync.adapters import get_adapter
    >>> adapter = get_adapter("cloudwatch")
    >>> adapter.__class__.__name__
    'CloudWatchAdapter'
    """
    key = source.lower().strip()
    if key not in _ADAPTER_REGISTRY:
        known = ", ".join(sorted(_ADAPTER_REGISTRY))
        raise ValueError(f"Unknown adapter '{source}'. Known adapters: {known}")
    return _ADAPTER_REGISTRY[key]()


__all__ = [
    "BaseAdapter",
    "CloudWatchAdapter",
    "GitHubAdapter",
    "JiraAdapter",
    "PagerDutyAdapter",
    "get_adapter",
]
