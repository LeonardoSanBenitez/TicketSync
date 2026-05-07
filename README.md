# TicketSync

Normalize tickets from any source into a standard schema.

Part of the [Libre Ticket Suite](https://github.com/Libre-Ticket-Suite).

## Install

```bash
pip install ticket-sync
```

## Quickstart

```python
from ticket_sync import Ticket, TicketSource, TicketPriority

ticket = Ticket(
    title="CPU usage above 90%",
    source=TicketSource.CLOUDWATCH,
    priority=TicketPriority.HIGH,
)
print(ticket.to_dict())
```
