"""Persistence layer.

One module, one dependency direction: agents and the pipeline call Store, and
Store is the only thing that knows Firestore exists. That keeps the agents
unit-testable against a fake and keeps collection naming in a single place.
"""

from agenda7.store.firestore_store import Collections, Store

__all__ = ["Store", "Collections"]
