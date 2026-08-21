"""Check for new mail on a timer, so nobody has to press the button.

The watcher runs exactly the scan the button runs, through the same job runner —
one job at a time is a property of :mod:`jobs`, not something worked around
here. A tick that finds the runner busy is simply skipped; the next one will
cover the same window, because the window is derived from when the last scan
finished rather than from when this tick started.

It stays quiet until setup is finished. Scanning before the seed step has taught
it who you already know would report every long-standing contact as a new lead,
which is worse than not scanning at all.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from ..config import SETTINGS
from ..pipeline import LAST_SCAN_KEY, new_mail_since, scan
from ..store import Store
from . import mailboxes
from .jobs import RUNNER, JobBusy

log = logging.getLogger(__name__)

JOB_KIND = "watch"


def _eligible() -> str | None:
    """Why this tick should be skipped, or ``None`` to go ahead."""
    if not SETTINGS.watch_enabled:
        return "watching is switched off"
    if not mailboxes.configured_accounts():
        return "no mailbox connected yet"
    if Store().get_state("seeded_at") is None:
        return "setup is not finished (contacts not learned yet)"
    return None


def check_now(progress=None) -> dict:
    """One pass over whatever has arrived since the last one."""
    say = progress or (lambda _msg: None)
    store = Store()
    since_at = new_mail_since(store, cap=timedelta(days=SETTINGS.scan_days))
    started = datetime.now(timezone.utc)

    report = scan(since_at=since_at, progress=say, store=store)

    # Stamped from before the scan, so mail that lands mid-scan falls inside the
    # next window instead of into the gap between the two.
    store.set_state(LAST_SCAN_KEY, started.isoformat())
    return {
        "fetched": report.fetched,
        "leads": len(report.leads),
        "triaged": len(report.triaged),
        "skipped": report.skip_counts(),
        "since": since_at.isoformat(),
        "errors": [
            {"mailbox": e.account.email or e.account.account_id, "error": e.error}
            for e in report.errors
        ],
    }


class Watcher:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_skip: str | None = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mail-watcher", daemon=True)
        self._thread.start()
        log.info("Mail watcher started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("Mail watcher stopped")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- loop ------------------------------------------------------------

    def _loop(self) -> None:
        # Settings are read every tick rather than captured here, so changing
        # the interval or switching watching off takes effect without a restart.
        while not self._stop.wait(self._delay()):
            try:
                self._tick()
            except Exception:  # a bad tick must never kill the loop
                log.exception("Mail watcher tick failed")

    def _delay(self) -> float:
        return max(1, SETTINGS.watch_interval_minutes) * 60

    def _tick(self) -> None:
        reason = _eligible()
        if reason:
            if reason != self.last_skip:  # log the state change, not every tick
                log.info("Not checking mail: %s", reason)
            self.last_skip = reason
            return
        self.last_skip = None
        try:
            RUNNER.start(JOB_KIND, check_now)
        except JobBusy as exc:
            log.debug("Skipping this check; %s is running", exc.kind)


WATCHER = Watcher()
