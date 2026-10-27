"""Background renewal that runs independently of the work it protects.

A handler can block on a model call or a download for minutes. Renewal must not wait for it, or
a slow job would silently cost the executor its lease.
"""

import threading
import time
from collections.abc import Callable

from sqlalchemy.exc import SQLAlchemyError

from trading.monitoring.logging import get_logger

_log = get_logger(__name__)


class Keepalive:
    """Call ``renew`` every ``interval`` seconds on a daemon thread.

    ``renew`` returns True while the claim is still ours and False once it is gone. ``lost`` is
    set when it returns False, or when renewals have failed for a whole ``ttl`` (by then the claim
    has expired anyway, so it can no longer be trusted).
    """

    def __init__(
        self, name: str, renew: Callable[[], bool], *, interval: float, ttl: float
    ) -> None:
        self._name = name
        self._renew = renew
        self._interval = interval
        self._ttl = ttl
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"keepalive-{name}", daemon=True)
        self.lost = threading.Event()

    def start(self) -> "Keepalive":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._interval + 1.0)

    def _run(self) -> None:
        deadline = time.monotonic() + self._ttl
        while not self._stop.wait(self._interval):
            try:
                owned = self._renew()
            except SQLAlchemyError:
                _log.warning("keepalive_database_error", name=self._name, exc_info=True)
                if time.monotonic() >= deadline:
                    self.lost.set()
                    return
                continue
            if not owned:
                self.lost.set()
                return
            deadline = time.monotonic() + self._ttl
