"""Background renewal that runs independently of the work it protects.

A handler can block on a model call or a download for minutes. Renewal must not wait for it, or
a slow job would silently cost the executor its lease.

Fail closed: any renewal error, or any renewal that has not succeeded before the claim could
have expired, counts as a lost claim. Losing a claim costs a restart and a reconcile; keeping a
claim we cannot prove we hold could cost a duplicate order.
"""

import threading
import time
from collections.abc import Callable

from trading.monitoring.logging import get_logger

_log = get_logger(__name__)
_MAX_MARGIN_SECONDS = 1.0


class Keepalive:
    """Call ``renew`` every ``interval`` seconds on a daemon thread.

    ``renew`` returns True while the claim is still ours and False once it is gone. ``lost`` is
    set, and the thread exits, on the first of: ``renew`` returning False, ``renew`` raising any
    exception, or no successful renewal within ``ttl`` minus a safety margin. The margin ensures
    ``lost`` is declared before the database lease itself expires.
    """

    def __init__(
        self, name: str, renew: Callable[[], bool], *, interval: float, ttl: float
    ) -> None:
        self._name = name
        self._renew = renew
        self._interval = interval
        self._ttl = ttl
        self._margin = min(_MAX_MARGIN_SECONDS, ttl / 4)
        self._valid_until = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"keepalive-{name}", daemon=True)
        self.lost = threading.Event()

    def start(self) -> "Keepalive":
        # The claim was taken just before this call, so its clock starts now.
        self._valid_until = time.monotonic() + self._ttl - self._margin
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._interval + 1.0)

    def _declare_lost(self, reason: str) -> None:
        _log.critical("keepalive_lost", name=self._name, reason=reason)
        self.lost.set()

    def _run(self) -> None:
        while True:
            # Never sleep past the point where the claim can no longer be trusted.
            remaining = self._valid_until - time.monotonic()
            if self._stop.wait(max(0.0, min(self._interval, remaining))):
                return
            if time.monotonic() >= self._valid_until:
                self._declare_lost("renewal overdue")
                return
            started = time.monotonic()
            try:
                owned = self._renew()
            except Exception:
                _log.critical("keepalive_renew_failed", name=self._name, exc_info=True)
                self.lost.set()
                return
            if not owned:
                self._declare_lost("claim no longer held")
                return
            # Measured from when the renewal was issued, not when it returned: the database
            # extended the lease no earlier than that.
            self._valid_until = started + self._ttl - self._margin
