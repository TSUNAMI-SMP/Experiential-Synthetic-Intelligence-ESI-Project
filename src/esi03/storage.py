"""Nested savepoints let memory, beliefs, core state and their event commit as one unit."""

import copy
import sqlite3
from functools import wraps

from esi.storage import Store as PreviousStore


class Connection(sqlite3.Connection):
    def __enter__(self):
        stack = getattr(self, "_savepoints", [])
        if not self.in_transaction:
            self.execute("BEGIN")
            name = None
        else:
            sequence = getattr(self, "_savepoint_sequence", 0) + 1
            self._savepoint_sequence = sequence
            name = "esi03_unit_" + str(sequence)
            self.execute("SAVEPOINT " + name)
        self._savepoints = stack + [name]
        return self

    def __exit__(self, exception_type, exception, traceback):
        name = self._savepoints.pop()
        if name is None:
            self.rollback() if exception_type else self.commit()
        else:
            if exception_type:
                self.execute("ROLLBACK TO " + name)
            self.execute("RELEASE " + name)
        return False


class Store(PreviousStore):
    def __init__(self, path, max_records=200000, read_only=False):
        super().__init__(path, max_records, read_only)
        if read_only:
            return
        self.db.close()
        self.db = sqlite3.connect(self.path, check_same_thread=False, factory=Connection)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")


def atomic(function):
    @wraps(function)
    def operation(self, *args, **kwargs):
        owner = self.session if hasattr(self, "session") else self
        with owner.lock, owner.store.lock:
            before = copy.deepcopy(owner.dump())
            try:
                with owner.store.db:
                    return function(self, *args, **kwargs)
            except Exception:
                owner.restore(before)
                raise

    return operation
