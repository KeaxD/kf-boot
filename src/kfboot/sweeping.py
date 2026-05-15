from __future__ import annotations

import threading
from uuid import uuid4

from hio.base import doing
from keri import help


logger = help.ogler.getLogger(__name__)


class CleanupDoer(doing.Doer):
    def __init__(
        self,
        *,
        expirer,
        interval: float,
        batch_size: int,
        time_budget_seconds: float,
        owner_id: str,
        stop: threading.Event,
        poll_tock: float,
    ):
        """
        Periodic HIO doer that drives the cleanup-task sweep loop.

        Purpose:
        - Run scheduled cleanup sweeps at a fixed interval
        - Attempt to acquire the sweep-leadership lease before each run
        - Execute Expirer.sweep() when leadership is held
        - Back off and retry when another worker owns the lease
        - Stop gracefully when the stop event is set

        Attributes:
        - expirer: The Expirer coordinating expiration/cleanup logic
        - interval: Minimum time between full cleanup sweeps
        - batch_size: Maximum number of tasks to process per sweep
        - time_budget_seconds: Maximum wall-clock time allowed per sweep
        - owner_id: Unique identifier for this worker when claiming tasks/leases
        - stop: Event used to signal shutdown
        - poll_tock: HIO scheduling tick for the doist loop
        """
        super().__init__(tock=poll_tock)
        self.expirer = expirer
        self.interval = interval
        self.batch_size = batch_size
        self.time_budget_seconds = time_budget_seconds
        self.owner_id = owner_id
        self.stop = stop

        # Retry interval used when lease acquisition fails.
        # Ensures we don't hammer the lease table.
        self.retry_interval = max(poll_tock, min(interval, 5.0))

        # Timestamp (HIO time) when the next sweep attempt is allowed
        self._next_run_at = 0.0

    def recur(self, tyme):
        """Run one scheduled cleanup attempt when this worker owns the lease."""

        # Stop requested, release leadership and return
        if self.stop.is_set():
            self.expirer.releaseSweepLease(owner_id=self.owner_id)
            return True

        # Not time yet for the next sweep attempt, returns
        if tyme < self._next_run_at:
            return False

        # Try to acquire the sweep-leadership lease. If another worker owns it, retry later.
        if not self.expirer.acquireSweepLease(owner_id=self.owner_id):
            self._next_run_at = tyme + self.retry_interval
            return False

        # Lease acquired, run a cleanup sweep.
        try:
            results = self.expirer.sweep(
                batch_size=self.batch_size,
                time_budget_seconds=self.time_budget_seconds,
                owner_id=self.owner_id,
                stop=self.stop,
            )
        except Exception:
            logger.exception("Periodic cleanup sweep failed unexpectedly")
        else:
            # Log if actual work was performed
            if any(results.values()):
                logger.info(
                    "Periodic cleanup sweep completed: "
                    f"sessions_expired={results['sessions_expired']}, "
                    f"sessions_cleaned={results['sessions_cleaned']}, "
                    f"accounts_expired={results['accounts_expired']}, "
                    f"accounts_cleaned={results['accounts_cleaned']}, "
                    f"accounts_deleted={results['accounts_deleted']}"
                )

        # Schedule the next sweep attempt
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
        enabled: bool = True,
    ):
        """
        Controller for the background cleanup thread.

        Purpose:
        - Own a dedicated thread that runs the HIO doist loop.
        - Construct and manage a CleanupDoer instance inside that loop.
        - Provide start/stop lifecycle management for periodic cleanup.
        - Ensure cleanup runs only when explicitly enabled and configured.

        Attributes:
        - expirer: The Expirer performing expiration/cleanup logic.
        - interval: Minimum time between cleanup sweeps.
        - batch_size: Maximum number of tasks per sweep.
        - time_budget_seconds: Maximum wall-clock time per sweep.
        - enabled: Whether the runner should start automatically.
        - _stop: Event used to signal shutdown to the CleanupDoer.
        - _thread: Background thread running the HIO doist loop.
        - owner_id: Unique identifier for this runner when acquiring leases.
        """
        self.expirer = expirer
        self.interval = interval
        self.batch_size = batch_size
        self.time_budget_seconds = time_budget_seconds
        self._enabled = enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Unique identity for this runner when claiming the sweep lease
        self.owner_id = f"cleanup-runner-{uuid4().hex[:12]}"

    @property
    def enabled(self) -> bool:
        """Return whether this runner is configured to start."""
        return self._enabled and self.interval > 0

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

        # Reset stop flag and start the background thread
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

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the cleanup thread and release leadership when possible."""
        self._stop.set()

        # Wait for the background thread to exit
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "Periodic cleanup sweeper did not stop within the timeout; "
                    "an in-flight downstream cleanup call may still be finishing."
                )
                return

        # Release leadership and clear thread reference
        self.expirer.releaseSweepLease(owner_id=self.owner_id)
        self._thread = None
        logger.info("Periodic cleanup sweeper stopped")

    def _run(self) -> None:
        """Run the HIO doist loop that repeatedly invokes the cleanup doer."""

        # Polling frequency for the doist loop
        poll_tock = max(0.25, min(self.interval, 1.0))

        # Construct the doer that will run cleanup sweeps
        doer = CleanupDoer(
            expirer=self.expirer,
            interval=self.interval,
            batch_size=self.batch_size,
            time_budget_seconds=self.time_budget_seconds,
            owner_id=self.owner_id,
            stop=self._stop,
            poll_tock=poll_tock,
        )

        # Run the doist loop until stop is signaled
        doist = doing.Doist(
            name="kf-boot-cleanup",
            real=True,
            tock=poll_tock,
            doers=[doer],
        )
        doist.do()
