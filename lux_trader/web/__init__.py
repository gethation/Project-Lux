"""Read-only web view of a live/replay store.

Strictly an observer: every SQLite connection this package opens is
``mode=ro``, and nothing here imports the execution or broker paths. It is safe
to run against the database of a live-execute process that is trading right now
-- WAL (store.py:57) lets a reader in another process read committed data
without blocking the writer.

Phase 1 reads only what the engine has already committed, which it does once per
finalized minute (engine.py:542). The "now" the page shows is therefore up to one
minute old, and the header says so rather than pretending otherwise.
"""

from .queries import StoreReader

__all__ = ["StoreReader"]
