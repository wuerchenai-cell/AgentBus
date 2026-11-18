"""Fixtures use only the original claim object, never a fresh disk ticket."""
from agentbus.claim_fencing import captured_ticket

def receipt_ticket(original):
    if getattr(original, "claim_epoch", 0) > 0:
        return captured_ticket(original)
    return None  # Explicit historical unfenced fixtures.


def receipt_source(original):
    ticket = receipt_ticket(original)
    return ticket.source_fields() if ticket is not None else {}
