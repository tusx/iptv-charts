"""Exceptions shared across modules.

Kept in one place so the sync engine can catch cancellation raised from deep
inside a client's streaming loop without clients importing the sync engine.
"""

from __future__ import annotations


class SyncCancelled(Exception):
    """Raised inside a streaming loop when the user asks to stop.

    Clients raise this rather than returning early so that partial state is
    never mistaken for a complete result.
    """


__all__ = ["SyncCancelled"]
