from __future__ import annotations

from datetime import UTC, datetime

from hio.base import doing
from keri import help


logger = help.ogler.getLogger(__name__)


def _nowIso() -> str:
    return datetime.now(UTC).isoformat()


class CleanupState:
    def __init__(self, *, enabled: bool, interval: float):
        self.enabled = enabled
        self.interval = interval
        self._running = False
        self._last_sweep_started_at = ""
        self._last_sweep_finished_at = ""
        self._last_progress_at = ""
        self._current_sweep_started_at = ""
        self._last_error = ""
        self._last_error_at = ""
        self._last_recovery_at = ""
        self._recovered_claimed_tasks = 0

    @property
    def expected_running(self) -> bool:
        return self.enabled and self.interval > 0

    @property
    def is_running(self) -> bool:
        return self._running

    def noteStarted(self) -> None:
        self._running = True

    def noteStopped(self) -> None:
        self._running = False
        self._current_sweep_started_at = ""

    def noteStartupRecovery(self, at: str, *, recovered_count: int) -> None:
        self._last_recovery_at = at
        self._recovered_claimed_tasks = recovered_count

    def noteSweepStarted(self, at: str) -> None:
        self._last_sweep_started_at = at
        self._current_sweep_started_at = at

    def noteSweepFinished(self, at: str, results: dict[str, int]) -> None:
        self._last_sweep_finished_at = at
        self._current_sweep_started_at = ""
        self._last_error = ""
        self._last_error_at = ""
        if any(results.values()):
            self._last_progress_at = at

    def noteSweepFailed(self, at: str, error: str) -> None:
        self._last_sweep_finished_at = at
        self._current_sweep_started_at = ""
        self._last_error_at = at
        self._last_error = error

    def snapshot(self, *, now: str | None = None) -> dict[str, object]:
        current = datetime.fromisoformat(now or _nowIso())
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


class CleanupDoer(doing.Doer):
    def __init__(
        self,
        *,
        expirer,
        interval: float,
        batch_size: int,
        time_budget_seconds: float,
        state: CleanupState,
    ):
        """
        Periodic HIO doer that drives the cleanup-task sweep loop.

        This doer is attached to the root service Doist. It reports progress to
        CleanupState so health can distinguish "alive" from "making progress."
        """
        poll_tock = max(0.25, min(interval, 1.0))
        super().__init__(tock=poll_tock)
        self.expirer = expirer
        self.interval = interval
        self.batch_size = batch_size
        self.time_budget_seconds = time_budget_seconds
        self.state = state
        self._next_run_at = 0.0

    def enter(self, *, temp=None):
        if not self.state.expected_running:
            return

        now = _nowIso()
        recovered = self.expirer.recoverClaimedCleanupTasks(now=now)
        self.state.noteStartupRecovery(now, recovered_count=recovered)
        self.state.noteStarted()
        logger.info(
            "Periodic cleanup sweeper started "
            f"(interval={self.interval}s, batch_size={self.batch_size}, time_budget={self.time_budget_seconds}s)"
        )

    def recur(self, tyme):
        """Run one scheduled cleanup attempt for the local cleanup doer."""

        if not self.state.expected_running:
            return True

        if tyme < self._next_run_at:
            return False

        self.state.noteSweepStarted(_nowIso())
        try:
            results = self.expirer.sweep(
                batch_size=self.batch_size,
                time_budget_seconds=self.time_budget_seconds,
            )
        except Exception as exc:
            self.state.noteSweepFailed(_nowIso(), str(exc))
            logger.exception("Periodic cleanup sweep failed unexpectedly")
        else:
            self.state.noteSweepFinished(_nowIso(), results)
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

    def exit(self):
        if self.state.is_running:
            self.state.noteStopped()
            logger.info("Periodic cleanup sweeper stopped")
