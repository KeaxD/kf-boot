from __future__ import annotations

import threading
from datetime import UTC, datetime
from uuid import uuid4

from hio.base import doing
from keri import help


logger = help.ogler.getLogger(__name__)


def _nowIso() -> str:
    return datetime.now(UTC).isoformat()


class CleanupDoer(doing.Doer):
    def __init__(
        self,
        *,
        expirer,
        interval: float,
        batch_size: int,
        time_budget_seconds: float,
        owner_id: str,
        runner,
        stop: threading.Event,
        poll_tock: float,
    ):
        """
        Periodic HIO doer that drives the cleanup-task sweep loop.

        This doer runs the local cleanup sweep on a schedule and reports progress back
        to CleanupRunner so health can distinguish "alive" from "making progress."
        """
        super().__init__(tock=poll_tock)
        self.expirer = expirer
        self.interval = interval
        self.batch_size = batch_size
        self.time_budget_seconds = time_budget_seconds
        self.owner_id = owner_id
        self.runner = runner
        self.stop = stop
        self._next_run_at = 0.0

    def recur(self, tyme):
        """Run one scheduled cleanup attempt for the single local cleanup runner."""

        if self.stop.is_set():
            return True

        if tyme < self._next_run_at:
            return False

        self.runner.noteSweepStarted(_nowIso())
        try:
            results = self.expirer.sweep(
                batch_size=self.batch_size,
                time_budget_seconds=self.time_budget_seconds,
                owner_id=self.owner_id,
                stop=self.stop,
            )
        except Exception as exc:
            self.runner.noteSweepFailed(_nowIso(), str(exc))
            logger.exception("Periodic cleanup sweep failed unexpectedly")
        else:
            self.runner.noteSweepFinished(_nowIso(), results)
            if any(results.values()):
                logger.info(
                    "Periodic cleanup sweep completed: "
                    f"sessions_expired={results['sessions_expired']}, "
                    f"sessions_cleaned={results['sessions_cleaned']}, "
                    f"sessions_deleted={results['sessions_deleted']}, "
                    f"accounts_expired={results['accounts_expired']}, "
                    f"accounts_cleaned={results['accounts_cleaned']}, "
                    f"accounts_deleted={results['accounts_deleted']}"
                )

        self._next_run_at = tyme + self.interval
        return False


class CleanupRunner:
    def __init__(
        self,
        *,
        expirer,
        interval: float,
        batch_size: int,
        time_budget_seconds: float,
        stop_timeout_seconds: float,
        enabled: bool = True,
    ):
        """Controller for the background cleanup thread"""
        self.expirer = expirer
        self.interval = interval
        self.batch_size = batch_size
        self.time_budget_seconds = time_budget_seconds
        self.stop_timeout_seconds = stop_timeout_seconds
        self._enabled = enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._last_sweep_started_at = ""
        self._last_sweep_finished_at = ""
        self._last_progress_at = ""
        self._current_sweep_started_at = ""
        self._last_error = ""
        self._last_error_at = ""
        self._last_recovery_at = ""
        self._recovered_claimed_tasks = 0
        self.owner_id = f"cleanup-runner-{uuid4().hex[:12]}"

    @property
    def enabled(self) -> bool:
        """Return whether this runner is configured to start."""
        return self._enabled and self.interval > 0

    @property
    def is_running(self) -> bool:
        """Return whether the cleanup thread is currently alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the background cleanup thread if cleanup is enabled."""
        if not self._enabled:
            logger.info("Periodic cleanup sweeper disabled because cleanup_runner_enabled is false")
            return
        if self.interval <= 0:
            logger.info("Periodic cleanup sweeper disabled because cleanup_interval_seconds <= 0")
            return
        if self._thread is not None and self._thread.is_alive():
            return

        recovered = self.expirer.recoverClaimedCleanupTasks(now=_nowIso())
        self.noteStartupRecovery(_nowIso(), recovered_count=recovered)

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="kf-boot-cleanup",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Periodic cleanup sweeper started "
            f"(interval={self.interval}s, batch_size={self.batch_size}, time_budget={self.time_budget_seconds}s)"
        )

    def stop(self, *, timeout: float | None = None) -> None:
        """Stop the cleanup thread and wait for the in-flight sweep when possible."""
        self._stop.set()
        effective_timeout = self.stop_timeout_seconds if timeout is None else timeout

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=effective_timeout)
            if self._thread.is_alive():
                logger.warning(
                    "Periodic cleanup sweeper did not stop within the timeout; "
                    "a downstream cleanup call may still be finishing."
                )
                return

        self._thread = None
        logger.info("Periodic cleanup sweeper stopped")

    def noteStartupRecovery(self, at: str, *, recovered_count: int) -> None:
        with self._state_lock:
            self._last_recovery_at = at
            self._recovered_claimed_tasks = recovered_count

    def noteSweepStarted(self, at: str) -> None:
        with self._state_lock:
            self._last_sweep_started_at = at
            self._current_sweep_started_at = at

    def noteSweepFinished(self, at: str, results: dict[str, int]) -> None:
        with self._state_lock:
            self._last_sweep_finished_at = at
            self._current_sweep_started_at = ""
            self._last_error = ""
            self._last_error_at = ""
            if any(results.values()):
                self._last_progress_at = at

    def noteSweepFailed(self, at: str, error: str) -> None:
        with self._state_lock:
            self._last_sweep_finished_at = at
            self._current_sweep_started_at = ""
            self._last_error_at = at
            self._last_error = error

    def snapshot(self, *, now: str | None = None) -> dict[str, object]:
        current = datetime.fromisoformat(now or _nowIso())
        with self._state_lock:
            current_sweep_started_at = self._current_sweep_started_at or None
            current_sweep_age_seconds = None
            if current_sweep_started_at is not None:
                current_sweep_age_seconds = max(
                    (current - datetime.fromisoformat(current_sweep_started_at)).total_seconds(),
                    0.0,
                )

            return {
                "last_sweep_started_at": self._last_sweep_started_at or None,
                "last_sweep_finished_at": self._last_sweep_finished_at or None,
                "last_progress_at": self._last_progress_at or None,
                "current_sweep_started_at": current_sweep_started_at,
                "current_sweep_age_seconds": current_sweep_age_seconds,
                "last_error_at": self._last_error_at or None,
                "last_error": self._last_error or None,
                "last_recovery_at": self._last_recovery_at or None,
                "recovered_claimed_tasks": self._recovered_claimed_tasks,
            }

    def _run(self) -> None:
        """Run the HIO doist loop that repeatedly invokes the cleanup doer."""

        poll_tock = max(0.25, min(self.interval, 1.0))
        doer = CleanupDoer(
            expirer=self.expirer,
            interval=self.interval,
            batch_size=self.batch_size,
            time_budget_seconds=self.time_budget_seconds,
            owner_id=self.owner_id,
            runner=self,
            stop=self._stop,
            poll_tock=poll_tock,
        )

        doist = doing.Doist(
            name="kf-boot-cleanup",
            real=True,
            tock=poll_tock,
            doers=[doer],
        )
        doist.do()
