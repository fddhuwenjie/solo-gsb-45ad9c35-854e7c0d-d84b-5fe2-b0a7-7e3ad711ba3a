"""SQLite storage layer.

A single connection is shared process-wide and every access is serialized
through one reentrant lock. All mutating operations run inside
``BEGIN IMMEDIATE`` transactions, so interleaved requests can never observe
or produce a half-written state, and primary-key constraints make duplicate
submissions/adjudications impossible even under races.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id                   TEXT PRIMARY KEY,
    allowed_categories   TEXT NOT NULL,              -- JSON array of strings
    phase                TEXT NOT NULL
                         CHECK (phase IN ('collecting', 'adjudicating', 'sealed')),
    reviewer1_token_hash TEXT NOT NULL,
    reviewer2_token_hash TEXT NOT NULL,
    arbiter_token_hash   TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    sealed_at            TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    batch_id   TEXT NOT NULL REFERENCES batches(id),
    position   INTEGER NOT NULL,
    code       TEXT NOT NULL,
    true_label TEXT NOT NULL,
    PRIMARY KEY (batch_id, code)
);

CREATE TABLE IF NOT EXISTS submissions (
    batch_id     TEXT NOT NULL REFERENCES batches(id),
    reviewer     INTEGER NOT NULL CHECK (reviewer IN (1, 2)),
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, reviewer)
);

CREATE TABLE IF NOT EXISTS submission_items (
    batch_id TEXT NOT NULL,
    reviewer INTEGER NOT NULL,
    code     TEXT NOT NULL,
    label    TEXT NOT NULL,
    PRIMARY KEY (batch_id, reviewer, code),
    FOREIGN KEY (batch_id, reviewer) REFERENCES submissions (batch_id, reviewer)
);

CREATE TABLE IF NOT EXISTS adjudications (
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    code        TEXT NOT NULL,
    final_label TEXT NOT NULL,
    PRIMARY KEY (batch_id, code)
);

-- Final, immutable per-sample outcome, written exactly once at seal time.
CREATE TABLE IF NOT EXISTS finals (
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    position        INTEGER NOT NULL,
    code            TEXT NOT NULL,
    final_label     TEXT NOT NULL,
    true_label      TEXT NOT NULL,
    reviewer1_label TEXT NOT NULL,
    reviewer2_label TEXT NOT NULL,
    PRIMARY KEY (batch_id, code)
);

-- Post-seal errata. Revision 0 is the sealed original (no row here); each
-- accepted erratum appends exactly one row with the next revision number.
CREATE TABLE IF NOT EXISTS errata (
    batch_id   TEXT NOT NULL REFERENCES batches(id),
    revision   INTEGER NOT NULL CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, revision)
);

-- Per-sample truth replacement carried by one errata revision.
CREATE TABLE IF NOT EXISTS errata_items (
    batch_id       TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    code           TEXT NOT NULL,
    old_true_label TEXT NOT NULL,  -- truth at the previous revision
    new_true_label TEXT NOT NULL,
    PRIMARY KEY (batch_id, revision, code),
    FOREIGN KEY (batch_id, revision) REFERENCES errata (batch_id, revision)
);
"""


class Store:
    """Thread-safe single-connection SQLite store."""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    @contextmanager
    def read(self):
        """Serialized read access."""
        with self._lock:
            yield self.conn

    @contextmanager
    def write(self):
        """Serialized write access inside an immediate transaction.

        Any exception (including HTTPException used for validation failures)
        rolls the transaction back, so a rejected request never leaves
        partial writes behind.
        """
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def close(self):
        with self._lock:
            self.conn.close()
