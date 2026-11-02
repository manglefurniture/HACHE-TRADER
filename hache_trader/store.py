"""SQLite transactions, append-only event ledger and recovery verification."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3
from .domain import GuardError, canonical, digest, money


def local_day(timestamp):
    # America/Cancun has fixed UTC-05 in this supported operational period.
    from datetime import timedelta
    return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=-5), "America/Cancun")).date().isoformat()


class Store:
    def __init__(self, path, policy, now):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at INTEGER NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL UNIQUE);
          CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
          CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
          CREATE TABLE IF NOT EXISTS updates(id INTEGER PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, body TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS confirmations(nonce TEXT PRIMARY KEY, expires INTEGER NOT NULL, message_id INTEGER, used INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS scan_diagnostics(
            id INTEGER PRIMARY KEY, at INTEGER NOT NULL, day TEXT NOT NULL,
            universe INTEGER NOT NULL, eligible INTEGER NOT NULL,
            checked INTEGER NOT NULL, signal_markets INTEGER NOT NULL,
            qualified INTEGER NOT NULL, reasons TEXT NOT NULL, decision TEXT NOT NULL
          );
          CREATE INDEX IF NOT EXISTS scan_diagnostics_day
            ON scan_diagnostics(day, id);
        """)
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM state").fetchone():
                initial = {"mode": "paper", "cash": policy.capital, "reserved": "0", "position": None,
                           "orders": {}, "realized": "0", "fees": "0", "paused": True,
                           "emergency": False, "reconciled": False, "policy": policy.fingerprint,
                           "capital": policy.capital, "day": local_day(now), "day_loss": "0",
                           "last_books": {}, "closed_trades": 0, "started": now, "last_cycle": 0,
                           "last_error": None, "markets": [], "marks": {}}
                self.save(initial, "initialized", {}, now)
            elif self.read()["policy"] != policy.fingerprint:
                raise GuardError("policy_change_requires_versioned_migration")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def read(self):
        return json.loads(self.db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])

    def save(self, state, kind, details, now):
        if not self.db.in_transaction:
            raise GuardError("transaction_required")
        self.validate(state)
        previous = self.db.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
        previous = previous[0] if previous else "0" * 64
        payload = canonical({"details": details, "state": state})
        hash_ = digest([now, kind, payload, previous])
        self.db.execute("INSERT INTO events(at,kind,payload,previous,hash) VALUES(?,?,?,?,?)", (now, kind, payload, previous, hash_))
        self.db.execute("INSERT INTO state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (canonical(state),))

    @staticmethod
    def validate(s):
        if s["mode"] != "paper" or money(s["cash"]) < 0 or money(s["reserved"]) < 0:
            raise GuardError("invalid_ledger")
        position = s["position"]
        basis = money(position["cost"]) if position else money(0)
        if position and (money(position["quantity"]) <= 0 or basis < 0):
            raise GuardError("invalid_position")
        expected = money(s["capital"]) + money(s["realized"])
        if abs(money(s["cash"]) + money(s["reserved"]) + basis - expected) > money("0.000000000001"):
            raise GuardError("ledger_does_not_balance")
        reservations = sum((money(o["reserve"]) for o in s["orders"].values()), money(0))
        if reservations != money(s["reserved"]):
            raise GuardError("reservation_mismatch")

    def reconcile(self, now):
        with self.transaction():
            previous = "0" * 64
            final = None
            for row in self.db.execute("SELECT * FROM events ORDER BY id"):
                if row["previous"] != previous or row["hash"] != digest([row["at"], row["kind"], row["payload"], previous]):
                    raise GuardError("audit_chain_invalid")
                final = json.loads(row["payload"])["state"]
                self.validate(final)
                previous = row["hash"]
            state = self.read()
            if final != state:
                raise GuardError("state_ledger_mismatch")
            # Unfilled paper reservations are cancellable locally; no exchange claims.
            for order in state["orders"].values():
                if order["status"] in ("open", "partial"):
                    state["cash"] = str(money(state["cash"]) + money(order["reserve"]))
                    order["reserve"] = "0"
                    order["status"] = "cancelled"
            state["reserved"] = "0"
            state["reconciled"] = True
            state["paused"] = True
            self.save(state, "reconciled_paused", {}, now)

    def control(self, action, now):
        with self.transaction():
            self.control_in_transaction(action, now)

    def control_in_transaction(self, action, now):
        state = self.read()
        if action == "resume":
            if state.get("telegram_connection") == "unavailable":
                raise GuardError("resume_requires_telegram_connection")
            if not state["reconciled"] or state["emergency"] or not 0 <= now - state["last_cycle"] <= 120 or state["last_error"]:
                raise GuardError("resume_requires_healthy_reconciled_state")
            state["paused"] = False
            state["telegram_resume_required"] = False
        elif action in ("pause", "emergency"):
            state["paused"] = True
            state["telegram_resume_required"] = False
            state["emergency"] = state["emergency"] or action == "emergency"
        else:
            raise GuardError("unknown_control")
        self.save(state, action, {}, now)

    def enqueue(self, key, message):
        self.db.execute("INSERT OR IGNORE INTO outbox(id,body) VALUES(?,?)", (key, canonical(message)))

    def backup(self, destination):
        target = sqlite3.connect(destination)
        try:
            self.db.backup(target)
        finally:
            target.close()

    def snapshot(self):
        return deepcopy(self.read())
