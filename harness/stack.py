"""Process and database lifecycle for the chaos harness (Phase 2).

Contract: every scenario gets a clean, controlled, observable system — a
dedicated database (ledgerproof_chaos), a dedicated Redis queue (DB 1, never
the dev queue on DB 0), an ingest server and a Celery worker whose logs the
runner can read, a HARD kill for the PARTIAL_WRITE fault, and a reliable
quiescence signal so the runner knows a scenario finished instead of guessing
with sleeps. A flaky quiescence check would turn every downstream measurement
into noise, so it polls two independent sources and requires both to agree
twice in a row.

Everything the child processes need arrives through the environment (env beats
.env in pydantic-settings), so the harness stack can never touch the dev
database, the dev queue, or live Stripe.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Self
from urllib.parse import urlsplit

import httpx
import psycopg
import redis
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "artifacts" / "logs"

_IS_WINDOWS_PLATFORM = os.name == "nt"
VENV_PYTHON = REPO_ROOT / ".venv" / ("Scripts/python.exe" if _IS_WINDOWS_PLATFORM else "bin/python")

HARNESS_DB = "ledgerproof_chaos"
# Hermetic: the harness signs its own deliveries with its own secret, so the
# core loop never depends on a live `stripe listen` session. Never a real
# Stripe secret.
HARNESS_SECRET = "whsec_" + "c" * 32

_IS_WINDOWS = os.name == "nt"
# Generous: these only elapse on failure, and `uv run` + celery's first import
# on a cold filesystem cache is slow on Windows.
_INGEST_READY_TIMEOUT = 90.0
_WORKER_READY_TIMEOUT = 120.0
_SHUTDOWN_TIMEOUT = 15.0
_POLL_INTERVAL = 0.15
_WORKER_READY_MARKER = "ready"  # celery logs "celery@host ready." at INFO


def _load_migrate() -> Callable[[str], list[str]]:
    # scripts/ is not a package; import migrate.py by file path.
    path = REPO_ROOT / "scripts" / "migrate.py"
    spec = importlib.util.spec_from_file_location("_harness_migrate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.migrate


_migrate = _load_migrate()


def _dbname(url: str) -> str:
    return str(psycopg.conninfo.conninfo_to_dict(url)["dbname"])


def _maintenance_url(url: str) -> str:
    """Same server, `postgres` database — you cannot DROP the database you are in."""
    params = psycopg.conninfo.conninfo_to_dict(url)
    params["dbname"] = "postgres"
    return psycopg.conninfo.make_conninfo(**params)


def _redis_db_index(url: str) -> int:
    path = urlsplit(url).path.lstrip("/")
    return int(path) if path.isdigit() else 0


def _kill_tree(proc: subprocess.Popen[bytes], *, timeout: float = _SHUTDOWN_TIMEOUT) -> None:
    """Terminate a child and every process it spawned. Unclean by design.

    `uv run X` is a *parent* of the python process doing the work (uv cannot
    exec-replace itself on Windows), so killing only proc.pid leaves an orphan
    worker still draining the queue — verified. TerminateProcess on the whole
    tree is both the no-orphan guarantee and the SIGKILL semantics the
    PARTIAL_WRITE fault needs.
    """
    if proc.poll() is not None:
        proc.wait()
        return
    if _IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:  # pragma: no cover — this project runs on Windows
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover — taskkill /F does not miss
        proc.kill()
        proc.wait(timeout=timeout)


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


@dataclass
class Stack:
    """A whole LedgerProof deployment the harness owns start to finish."""

    port: int = 8100
    db_url: str = f"postgresql://postgres:ledgerproof@localhost:5432/{HARNESS_DB}"
    app_db_url: str = f"postgresql://ledger_app:ledger_app@localhost:5432/{HARNESS_DB}"
    redis_url: str = "redis://localhost:6379/1"  # DB 1: never the dev queue
    webhook_secret: str = HARNESS_SECRET

    _ingest: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _worker: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _ingest_fh: IO[bytes] | None = field(default=None, init=False, repr=False)
    _worker_fh: IO[bytes] | None = field(default=None, init=False, repr=False)
    _worker_log_offset: int = field(default=0, init=False, repr=False)
    _redis: redis.Redis | None = field(default=None, init=False, repr=False)

    # ---------------------------------------------------------------- urls --

    @property
    def ingest_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/webhook"

    @property
    def ingest_log_path(self) -> Path:
        return LOG_DIR / f"ingest_{self.port}.log"

    @property
    def worker_log_path(self) -> Path:
        return LOG_DIR / f"worker_{self.port}.log"

    @property
    def ingest_proc(self) -> subprocess.Popen[bytes] | None:
        return self._ingest

    @property
    def worker_proc(self) -> subprocess.Popen[bytes] | None:
        return self._worker

    # ----------------------------------------------------------- database --

    def create_database(self) -> None:
        """Drop and recreate the harness database, then migrate it."""
        name = _dbname(self.db_url)
        with psycopg.connect(_maintenance_url(self.db_url), autocommit=True) as conn:
            # WITH (FORCE) terminates leftover backends from a crashed run;
            # without it a stale connection makes DROP DATABASE block forever.
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        _migrate(self.db_url)

    def reset_ledger(self) -> None:
        """Empty the ledger between scenarios: drop the schema and re-migrate.

        TRUNCATE is not an option — the ledger's own append-only triggers
        (entries_no_truncate / transactions_no_truncate) reject it. The rule
        the ledger enforces on the application also constrains its harness,
        which is the point; a schema reset is the sanctioned path.
        """
        with psycopg.connect(self.db_url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        _migrate(self.db_url)

    # -------------------------------------------------------------- redis --

    def _redis_client(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.Redis.from_url(
                self.redis_url, decode_responses=True, socket_timeout=5
            )
        return self._redis

    def purge_queue(self) -> None:
        """Empty the harness broker DB, including unacked in-flight messages.

        A message left over from a previous scenario (especially one a killed
        worker never acked) would be processed inside the NEXT scenario and
        attributed to the wrong fault.
        """
        if _redis_db_index(self.redis_url) == 0:
            raise RuntimeError(
                f"refusing to purge redis db 0 (the dev queue): {self.redis_url}. "
                "The harness owns db 1."
            )
        self._redis_client().flushdb()

    def queue_len(self) -> int:
        """Messages waiting in the broker's celery queue(s).

        Counts only the ready lists, not the `unacked` hash: a message a KILLED
        worker never acked stays in `unacked` until kombu's visibility timeout
        (an hour by default), which would wedge quiescence forever after the
        PARTIAL_WRITE fault. In-flight work is covered by the event-log poll
        instead.
        """
        client = self._redis_client()
        total = 0
        for key in client.scan_iter(match="celery*", count=100):
            if client.type(key) == "list":  # priority steps add suffixed keys
                total += client.llen(key)
        return total

    # ------------------------------------------------------------ process --

    def _child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "STRIPE_WEBHOOK_SECRET": self.webhook_secret,
                "DATABASE_URL": self.db_url,
                "APP_DATABASE_URL": self.app_db_url,
                "TEST_DATABASE_URL": self.db_url,
                "REDIS_URL": self.redis_url,
                # Empty, not absent: env beats .env in pydantic-settings, so
                # this positively disables egress rather than falling through
                # to the real sandbox key in .env.
                "STRIPE_SECRET_KEY": "",
                # Readiness and failures are read back out of the log FILE, so
                # the children must not sit on a buffered pipe.
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        return env

    def _launch(
        self, argv: list[str], log_path: Path, *, truncate: bool
    ) -> tuple[subprocess.Popen[bytes], IO[bytes], int]:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if truncate:
            log_path.open("wb").close()
        fh = log_path.open("ab")
        offset = fh.tell()
        proc = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            env=self._child_env(),
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **({} if _IS_WINDOWS else {"start_new_session": True}),
        )
        return proc, fh, offset

    def _start_ingest(self) -> None:
        argv = [
            "uv",
            "run",
            "uvicorn",
            "--factory",
            "ledgerproof.ingest.server:build_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--log-level",
            "info",
        ]
        self._ingest, self._ingest_fh, _ = self._launch(
            argv, self.ingest_log_path, truncate=True
        )
        deadline = time.monotonic() + _INGEST_READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._ingest.poll() is not None:
                raise RuntimeError(
                    f"ingest exited with code {self._ingest.returncode} before it was "
                    f"ready:\n{self.ingest_log(60)}"
                )
            if self.ingest_healthy():
                return
            time.sleep(0.2)
        raise TimeoutError(
            f"ingest did not answer /healthz on port {self.port} within "
            f"{_INGEST_READY_TIMEOUT}s:\n{self.ingest_log(60)}"
        )

    def _start_worker(self, *, truncate: bool) -> None:
        # The venv interpreter directly, NOT `uv run celery`: uv cannot
        # exec-replace itself on Windows, so `uv run` stays as a parent and the
        # only way to reach the real writer is a taskkill /T tree walk that
        # costs ~900ms — an eternity next to a ~150ms ingest->COMMIT pipeline,
        # which made PARTIAL_WRITE unable to ever land inside a write. Launched
        # this way, Popen.pid IS the process holding the database transaction
        # and kill() reaches it in microseconds. --pool=solo spawns no children,
        # so there is no tree to miss.
        argv = [
            str(VENV_PYTHON),
            "-m",
            "celery",
            "-A",
            "ledgerproof.worker.tasks",
            "worker",
            "--pool=solo",  # REQUIRED on Windows: prefork is unsupported
            "-l",
            "info",
            "-n",
            f"chaos{self.port}@%h",
        ]
        self._worker, self._worker_fh, self._worker_log_offset = self._launch(
            argv, self.worker_log_path, truncate=truncate
        )
        self._wait_for_worker_ready()

    def _wait_for_worker_ready(self) -> None:
        assert self._worker is not None
        deadline = time.monotonic() + _WORKER_READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._worker.poll() is not None:
                raise RuntimeError(
                    f"worker exited with code {self._worker.returncode} before it was "
                    f"ready:\n{self.worker_log(60)}"
                )
            # Read from the offset captured at launch so a RESTART is not
            # satisfied by the previous worker's "ready" line.
            with self.worker_log_path.open("rb") as fh:
                fh.seek(self._worker_log_offset)
                fresh = fh.read().decode("utf-8", errors="replace")
            if _WORKER_READY_MARKER in fresh.lower():
                return
            time.sleep(0.2)
        raise TimeoutError(
            f"worker did not report ready within {_WORKER_READY_TIMEOUT}s:\n"
            f"{self.worker_log(60)}"
        )

    def start(self) -> None:
        """Purge the queue, then bring up ingest and the worker."""
        if self._ingest is not None or self._worker is not None:
            raise RuntimeError("stack already started; call stop() first")
        if self.ingest_healthy(timeout=1.0):
            # Something already owns the port. Left unchecked, the new uvicorn
            # would die on bind while /healthz kept answering from the STALE
            # process, and every scenario would silently measure that one.
            raise RuntimeError(
                f"port {self.port} already serves /healthz — an orphaned harness stack "
                "is still running; stop it before starting a new one"
            )
        self.purge_queue()
        try:
            self._start_ingest()
            self._start_worker(truncate=True)
        except BaseException:
            self.stop()  # never leave half a stack running behind an exception
            raise

    def stop(self) -> None:
        """Terminate both children and drain their logs. Idempotent.

        Windows offers no graceful stop for a console child without a shared
        console, so this is a tree kill; correctness after an unclean stop is
        the ledger's job (and start() purges the queue anyway).
        """
        for attr in ("_worker", "_ingest"):
            proc: subprocess.Popen[bytes] | None = getattr(self, attr)
            if proc is not None:
                _kill_tree(proc)
                setattr(self, attr, None)
        for attr in ("_worker_fh", "_ingest_fh"):
            fh = getattr(self, attr)
            if fh is not None:
                fh.flush()
                fh.close()
                setattr(self, attr, None)

    def kill_worker(self) -> None:
        """SIGKILL-equivalent, mid-transaction: the PARTIAL_WRITE fault.

        Deliberately unclean — no celery shutdown hook runs, nothing is acked,
        and any open database transaction dies uncommitted. That is the point:
        the ledger must show either the whole transaction or none of it.
        """
        if self._worker is None:
            raise RuntimeError("worker is not running")
        # kill(), not _kill_tree(): the worker IS a bare python process (see
        # _start_worker), so TerminateProcess lands on the transaction holder
        # immediately. Reaping afterwards costs whatever it costs — by then the
        # process is already dead, which is the measurement that matters.
        self._worker.kill()
        try:
            self._worker.wait(timeout=_SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:  # pragma: no cover - TerminateProcess does not miss
            _kill_tree(self._worker)

    def restart_worker(self) -> None:
        """Relaunch the worker and wait for it to report ready."""
        if self._worker is not None:
            _kill_tree(self._worker)
            self._worker = None
        if self._worker_fh is not None:
            self._worker_fh.flush()
            self._worker_fh.close()
            self._worker_fh = None
        self._start_worker(truncate=False)

    # ------------------------------------------------------- observability --

    def ingest_healthy(self, *, timeout: float = 2.0) -> bool:
        try:
            resp = httpx.get(f"http://127.0.0.1:{self.port}/healthz", timeout=timeout)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    def pending_events(
        self, conn: psycopg.Connection | None = None, event_ids: Sequence[str] | None = None
    ) -> int:
        """Event-log rows the worker has not finished with.

        Scope this to a scenario's own event ids whenever you have them. A
        global count makes every later scenario inherit an earlier one's stuck
        row, which reads as a fresh break in each of them — the harness would
        report one bug dozens of times and hide whatever the later scenarios
        actually did.
        """
        query = "SELECT count(*) FROM stripe_events WHERE status IN ('received','queued')"
        params: tuple = ()
        if event_ids is not None:
            query += " AND id = ANY(%s)"
            params = (list(event_ids),)
        if conn is not None:
            row = conn.execute(query, params).fetchone()
            return int(row[0]) if row else 0
        with psycopg.connect(self.db_url, autocommit=True) as own:
            row = own.execute(query, params).fetchone()
            return int(row[0]) if row else 0

    def wait_quiescent(
        self, *, timeout: float = 30.0, event_ids: Sequence[str] | None = None
    ) -> bool:
        """True once nothing is queued and nothing is in flight; False on timeout.

        Two independent sources must agree, twice in a row ~150ms apart: the
        broker (nothing waiting to be delivered) and the event log (no row
        still 'received'/'queued', which is what an in-flight or retrying task
        looks like). One source alone is a lie — the queue empties the instant
        the worker picks a message up, and the event log lags the enqueue.
        Never hangs: a timeout returns False and the caller records it.
        """
        deadline = time.monotonic() + timeout
        stable = 0
        with psycopg.connect(self.db_url, autocommit=True) as conn:
            while True:
                idle = self.queue_len() == 0 and self.pending_events(conn, event_ids) == 0
                stable = stable + 1 if idle else 0
                if stable >= 2:
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_POLL_INTERVAL)

    def worker_log(self, tail: int = 80) -> str:
        return _tail(self.worker_log_path, tail)

    def ingest_log(self, tail: int = 80) -> str:
        return _tail(self.ingest_log_path, tail)

    # ----------------------------------------------------- context manager --

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
