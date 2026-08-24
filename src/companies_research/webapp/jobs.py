"""Background jobs for the web UI.

Scanning a mailbox takes tens of seconds, which is far too long for an HTTP
request. Each long action runs on a worker thread and the browser polls for
progress.

Only one job runs at a time. That is a real constraint, not a simplification:
seeding and scanning touch the same mailboxes and the same SQLite file, and a
single user pressing two buttons has no reason to want both at once.
"""

from __future__ import annotations

import logging
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable  # noqa: F401  (Callable is used in Job)

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"  # running | done | error | cancelled
    phase: str = ""
    lines: list[str] = field(default_factory=list)
    result: Any = None
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    # Set by the work function itself, via `RUNNER.set_canceller`, once it knows
    # how it can be stopped. Jobs that never register one simply run to the end.
    cancel: Callable[[], None] | None = None
    cancelled: bool = False
    # A per-job secret handed only to the caller that started it. Used by the
    # pre-login Google flow so only the browser that began a sign-in can poll
    # it or trade it for a session — never exposed in as_dict().
    secret: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "phase": self.phase,
            "lines": self.lines[-200:],
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancellable": self.status == "running" and self.cancel is not None,
        }


class _LogPump(logging.Handler):
    """Mirrors library logging into the job's detail pane.

    Users never need this, but when something fails it is the difference
    between "it didn't work" and an actionable error.

    The job lock deliberately does *not* live on ``self.lock``: that name is
    already owned by :class:`logging.Handler`, which acquires it around every
    call to :meth:`emit`. Assigning to it means ``emit`` is entered with the
    lock held and then tries to take it again — and the runner's lock is a
    plain :class:`~threading.Lock`, so the first record to arrive deadlocks
    the job thread while holding the lock every other request needs.
    """

    def __init__(self, job: Job, lock: threading.Lock) -> None:
        super().__init__(level=logging.INFO)
        self.job = job
        self._job_lock = lock

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:  # a broken format string must not kill the job
            return
        with self._job_lock:
            self.job.lines.append(f"{record.levelname.lower()}: {text}")


class JobRunner:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._current: str | None = None

    # -- inspection ------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def take(self, job_id: str, *, kind: str) -> Job | None:
        """Pop a finished job of the given kind, atomically. Returns it once.

        The single-use guarantee for session minting: a second call for the
        same id gets None, so a finished sign-in cannot be replayed into more
        than one session, and the identity it carries stops being readable the
        moment it is spent.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.kind != kind or job.status != "done":
                return None
            self._jobs.pop(job_id, None)
            if self._current == job_id:
                self._current = None
            return job

    @property
    def busy(self) -> bool:
        with self._lock:
            current = self._jobs.get(self._current or "")
            return current is not None and current.status == "running"

    def current(self) -> Job | None:
        with self._lock:
            return self._jobs.get(self._current or "")

    # -- cancellation ----------------------------------------------------

    def set_canceller(self, stop: Callable[[], None]) -> None:
        """Called by the running job once it knows how it can be stopped.

        Only one job runs at a time, so "the running job" is unambiguous.
        """
        with self._lock:
            job = self._jobs.get(self._current or "")
            if job is not None and job.status == "running":
                job.cancel = stop

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "running" or job.cancel is None:
                return False
            job.cancelled = True
            job.phase = "Stopping…"
            stop = job.cancel
        # Outside the lock: stopping may block on I/O, and everything else
        # (including the browser polling this job) needs the lock meanwhile.
        stop()
        return True

    # -- execution -------------------------------------------------------

    def start(self, kind: str, work: Callable[[Progress], Any]) -> Job:
        with self._lock:
            running = self._jobs.get(self._current or "")
            if running is not None and running.status == "running":
                raise JobBusy(running.kind)

            job = Job(
                id=uuid.uuid4().hex[:12],
                kind=kind,
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            self._jobs[job.id] = job
            self._current = job.id
            self._prune()

        thread = threading.Thread(target=self._run, args=(job, work), daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, work: Callable[[Progress], Any]) -> None:
        def progress(message: str) -> None:
            with self._lock:
                job.phase = message
                job.lines.append(message)

        pump = _LogPump(job, self._lock)
        root = logging.getLogger("companies_research")
        root.addHandler(pump)
        try:
            result = work(progress)
            with self._lock:
                job.result = result
                job.status = "done"
        except Exception as exc:
            with self._lock:
                stopped_on_request = job.cancelled
            if stopped_on_request:
                # The exception is how the work unwinds once cancelled; it is
                # the expected ending, not a failure to report.
                log.info("job %s (%s) cancelled", job.id, job.kind)
                with self._lock:
                    job.status = "cancelled"
            else:
                log.exception("job %s (%s) failed", job.id, job.kind)
                with self._lock:
                    job.status = "error"
                    job.error = friendly_error(exc)
                    job.lines.append(traceback.format_exc().strip().splitlines()[-1])
        finally:
            root.removeHandler(pump)
            with self._lock:
                job.finished_at = datetime.now(timezone.utc).isoformat()

    def _prune(self, keep: int = 20) -> None:
        """Caller holds the lock."""
        if len(self._jobs) <= keep:
            return
        ordered = sorted(self._jobs.values(), key=lambda j: j.started_at)
        for stale in ordered[: len(ordered) - keep]:
            if stale.status != "running":
                self._jobs.pop(stale.id, None)


class JobBusy(RuntimeError):
    def __init__(self, kind: str) -> None:
        super().__init__(f"already running: {kind}")
        self.kind = kind


def friendly_error(exc: Exception) -> str:
    """Turn library exceptions into something a non-engineer can act on."""
    name = type(exc).__name__
    text = str(exc).strip() or name

    lowered = text.lower()
    if "authentication failed" in lowered or "invalid credentials" in lowered:
        return (
            "The mail server rejected the sign-in. If you are using an app "
            "password, generate a new one and paste it again."
        )
    if "api key" in lowered or name in {"AuthenticationError", "PermissionDeniedError"}:
        return "Claude rejected the API key. Check it in Settings."
    if name == "RateLimitError":
        return "Claude is rate limiting this key right now. Try again in a minute."
    if name in {"APIConnectionError", "ConnectionError", "TimeoutError"} or "temporary failure" in lowered:
        return f"Could not reach the server — check your internet connection. ({text})"
    if name in {"MissingClientSecret", "ConsentTimeout"}:
        return text  # already written for the person reading it
    return f"{name}: {text}" if name not in text else text


RUNNER = JobRunner()
