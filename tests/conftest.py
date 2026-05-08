"""pytest configuration for TicketSync tests.

No shared fixtures at module level — tests create their own fixtures using
tmp_path (pytest built-in) for filesystem isolation.
"""
