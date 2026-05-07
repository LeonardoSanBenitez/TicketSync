"""Abstract base class for all TicketSync source adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

from ticket_sync.models import Ticket


class BaseAdapter(ABC):
    """Convert a raw platform payload into a canonical :class:`Ticket`.

    Subclass this and implement :meth:`parse` to add a new source adapter.
    Adapters are stateless — they hold no configuration and make no network
    calls. All parsing logic operates solely on the supplied *payload* dict.
    """

    @abstractmethod
    def parse(self, payload: Dict[str, Any]) -> Ticket:
        """Parse a raw webhook payload into a Ticket.

        Args:
            payload: The decoded JSON payload from the source platform,
                represented as a plain Python dictionary.

        Returns:
            A fully-populated :class:`Ticket` instance.  Fields that cannot
            be determined from the payload default to their model defaults
            (e.g. ``TicketPriority.UNKNOWN``).

        Raises:
            KeyError: If a required field is missing from *payload*.
            ValueError: If a field value cannot be mapped to the canonical
                enum values.
        """
