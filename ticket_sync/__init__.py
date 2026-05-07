"""TicketSync — normalize tickets from any source into a standard schema.

This is the foundational layer of the Libre Ticket Suite.
It defines the canonical Ticket model and provides source adapters
that convert raw payloads from platforms like ServiceNow, Jira,
PagerDuty, and AWS CloudWatch into normalized Ticket objects.

>>> from ticket_sync import Ticket, version
>>> version()
'hello ticket'
"""

from ticket_sync.adapters import (
    BaseAdapter,
    CloudWatchAdapter,
    GitHubAdapter,
    JiraAdapter,
    PagerDutyAdapter,
    get_adapter,
)
from ticket_sync.models import Ticket, TicketPriority, TicketStatus, TicketSource
from ticket_sync.version import version

__all__ = [
    "BaseAdapter",
    "CloudWatchAdapter",
    "GitHubAdapter",
    "JiraAdapter",
    "PagerDutyAdapter",
    "get_adapter",
    "Ticket",
    "TicketPriority",
    "TicketStatus",
    "TicketSource",
    "version",
]
