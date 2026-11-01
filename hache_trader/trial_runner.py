"""Keep Telegram responsive while the simulated market scan performs network I/O.

The controller and the market worker use DIFFERENT SQLite connections, each
created in its owning thread. SQLite WAL/BEGIN IMMEDIATE serializes ledger
writes. Neither component ever has access to a real order-execution API.
"""
import threading
import time

from .domain import GuardError
from .runtime import Worker
from .store import Store


class TrialMarketLoop:
    def __init__(self, ledger, policy, api, stop_file, *,
                 clock=time.time, interval=30, worker_factory=None):
        self.ledger, self.policy, self.api, self.stop_file = ledger, policy, api, stop_file
        self.clock, self.interval = clock, interval
        self.worker_factory = worker_factory or Worker
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._failed = threading.Event()
        self._thread = None

    def _run(self):
        store = None
        try:
            store = Store(self.ledger, self.policy, int(self.clock()))
            worker = self.worker_factory(store, self.policy, self.api, self.stop_file,
                                         clock=self.clock)
            self._ready.set()
            while not self._stop.is_set():
                worker.cycle()
                if self._stop.wait(self.interval):
                    break
        except BaseException:
            # Never log exception objects: provider responses can contain URLs
            # or credentials. A failed controller thread must stop the service.
            self._failed.set()
            self._ready.set()
            if store is not None:
                try:
                    store.control("pause", int(self.clock()))
                except Exception:
                    pass
        finally:
            if store is not None:
                store.close()

    def start(self):
        if self._thread is not None:
            raise GuardError("trial_market_loop_already_started")
        self._thread = threading.Thread(
            target=self._run, name="hache-trader-paper-market", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(10):
            raise GuardError("trial_market_loop_start_timeout")
        self.check()

    def check(self):
        if self._thread is None or self._failed.is_set() or not self._thread.is_alive():
            raise GuardError("trial_market_loop_unavailable")

    def close(self):
        self._stop.set()
        if self._thread is not None:
            # API calls have bounded per-request timeouts; do not stall a
            # systemd shutdown waiting on an exchange that may be offline.
            self._thread.join(timeout=2)
