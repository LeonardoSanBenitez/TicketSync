"""TicketSync adapter protocol.

Any class that implements the four methods below is a valid TicketSync adapter.
We use ``typing.Protocol`` with ``runtime_checkable`` so that callers can use
``isinstance`` checks, and so that structural subtyping is enforced without
requiring inheritance.

Adapters are responsible for:
- ``to_ticket``    — map a raw vendor payload dict to a ``Ticket`` IR
- ``from_ticket``  — map a ``Ticket`` IR back to a vendor-native dict
- ``fetch_new``    — pull new/updated tickets from the source system
- ``write``        — push a ticket to the destination system

The library has **no opinion** on scheduling, queueing, or retry.  Callers own
those concerns.  ``fetch_new`` returns a list; callers decide how to process it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from ticketsync.models import Ticket


@runtime_checkable
class TicketAdapter(Protocol):
    """Structural protocol every adapter must satisfy."""

    #: Short name used as the ``source_system`` field on produced tickets.
    system_name: str

    def to_ticket(self, raw: dict[str, object]) -> Ticket:
        """Convert a raw vendor payload dict to a canonical ``Ticket``.

        Parameters
        ----------
        raw:
            The vendor-native payload as a plain Python dict.  The adapter
            must not modify this dict.

        Returns
        -------
        Ticket
            A fully populated ticket.  The ``raw`` field on the returned ticket
            should contain ``raw`` verbatim so callers retain access to
            vendor-specific fields.
        """
        ...

    def from_ticket(self, ticket: Ticket) -> dict[str, object]:
        """Serialize a ``Ticket`` IR into a vendor-native payload dict.

        The returned dict should be suitable for passing directly to the
        vendor's write API.  Fields that the vendor does not support can be
        silently dropped.
        """
        ...

    def fetch_new(self, since: datetime | None = None) -> list[dict[str, object]]:
        """Return raw payloads for tickets created or updated since ``since``.

        Parameters
        ----------
        since:
            If ``None``, the adapter should return all available tickets (or
            a reasonable recent window — the exact semantics are
            adapter-specific).  If provided, only tickets updated after this
            timestamp should be returned.

        Returns
        -------
        list[dict]
            Raw vendor payloads.  Call ``to_ticket`` on each element to
            convert to the IR.
        """
        ...

    def write(self, ticket: Ticket) -> str:
        """Push a ticket to the system and return its vendor-native ID.

        Parameters
        ----------
        ticket:
            The ticket to write.

        Returns
        -------
        str
            The vendor-native identifier assigned by the destination system.
        """
        ...
