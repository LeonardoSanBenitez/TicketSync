"""TicketSync — bidirectional ticket synchronization across systems.

Public API surface::

    from ticketsync import Ticket, TicketAdapter, SyncEngine, SyncConfig
    from ticketsync.adapters import LocalFilesystemAdapter, ADAPTER_REGISTRY
"""

__version__ = "0.2.0"

from ticketsync.models import (
    Ticket,
    Entity,
    SeverityLevel,
    TicketStatus,
    AccountEntity,
    HostEntity,
    ProcessEntity,
    IpAddressEntity,
    FileEntity,
    UrlEntity,
)
from ticketsync.adapter import TicketAdapter
from ticketsync.config import SyncConfig, AdapterConfig
from ticketsync.engine import SyncEngine, SyncResult

__all__ = [
    "__version__",
    # Models
    "Ticket",
    "Entity",
    "SeverityLevel",
    "TicketStatus",
    "AccountEntity",
    "HostEntity",
    "ProcessEntity",
    "IpAddressEntity",
    "FileEntity",
    "UrlEntity",
    # Protocol
    "TicketAdapter",
    # Config
    "SyncConfig",
    "AdapterConfig",
    # Engine
    "SyncEngine",
    "SyncResult",
]


def hello_ticket() -> str:
    """Return a greeting from TicketSync. Used to verify the package installs and imports."""
    return "hello ticket from TicketSync"
