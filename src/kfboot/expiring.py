# expiring.py

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timedelta
from threading import Event, RLock
from uuid import uuid4

from keri import help

from kfboot.basing import (
    ACCOUNT_STATE_EXPIRED,
    ACCOUNT_STATE_ONBOARDED,
    CLEANUP_TASK_ACCOUNT_CLEANUP,
    CLEANUP_TASK_ACCOUNT_DELETE,
    CLEANUP_TASK_ACCOUNT_EXPIRE,
    CLEANUP_TASK_SESSION_CLEANUP,
    CLEANUP_TASK_SESSION_EXPIRE,
    SESSION_STATE_EXPIRED,
    TERMINAL_SESSION_STATES,
    CleanupTaskRecord,
    SessionRecord,
)
from kfboot.boot_client import BootError
from kfboot.store import accountFailed, nowIso, sessionFailed

logger = help.ogler.getLogger(__name__)


class Expirer:
    """
    Coordinate all session and account expiration, cleanup, and deletion
    workflows using the durable cleanup‑task queue.

    Purpose:
    - Act as the lifecycle controller for sessions and accounts whose TTLs,
      cleanup requirements, or retention windows have elapsed.
    - Provide a single, serialized execution path for all expiration‑related
      state transitions, ensuring correctness even under concurrency.
    - Drive the durable cleanup‑task queue by claiming, processing, rescheduling,
      and completing tasks in due‑time order.

    Core responsibilities:
    - Expire sessions and accounts when their stored expiry timestamps are due.
    - Tear down hosted resources for expired sessions and accounts.
    - Delete expired accounts after cleanup and retention windows.
    - Apply exponential‑backoff retry scheduling for cleanup failures.
    - Maintain cleanup leadership via a lease to prevent multiple workers from
      processing the same tasks concurrently.
    - Provide helper APIs for session failure, lease refresh, and dynamic
      expiration transitions.

    Cleanup‑task lifecycle enforced:
    - `session_expire` => mark session expired.
    - `session_cleanup` => teardown session resources => complete.

    - `account_expire` => mark account expired.
    - `account_cleanup` => teardown account resources => complete.
    - `account_delete` => delete expired, cleaned account => complete.

    High‑level workflow:
    - sweep(): claim tasks until batch/time budget is exhausted.
    - _claimDueTask(): atomically claim one due task.
    - _processClaimedTask(): dispatch to the appropriate handler.
    - _process*(): perform state transitions, teardown, or deletion.
    - _rescheduleTask(): defer failed tasks with exponential backoff.
    - _completeTask(): remove finished tasks from the durable queue.
    """
    # Just for readability
    SESSION_EXPIRE_TASK = CLEANUP_TASK_SESSION_EXPIRE
    SESSION_CLEANUP_TASK = CLEANUP_TASK_SESSION_CLEANUP
    ACCOUNT_EXPIRE_TASK = CLEANUP_TASK_ACCOUNT_EXPIRE
    ACCOUNT_CLEANUP_TASK = CLEANUP_TASK_ACCOUNT_CLEANUP
    ACCOUNT_DELETE_TASK = CLEANUP_TASK_ACCOUNT_DELETE
    SWEEP_LEASE_NAME = "cleanup_sweep"

    def __init__(self, ctx, provisioner):
        """
        Initialize the Expirer, the coordinator responsible for all session and
        account expiration, cleanup, and deletion workflows.

        Responsibilities:
        - Store references to the application context and the Provisioner used
        for tearing down hosted resources.
        - Create a per‑process, per‑instance owner_id used when claiming cleanup
        tasks and acquiring the sweep‑leadership lease. This ensures that each
        worker has a unique identity in the queue.
        - Initialize an internal re‑entrant lock (RLock) to serialize all state
        transitions, DB writes, and cleanup‑task scheduling performed by this
        Expirer instance. This prevents concurrent workers or threads from
        racing during expiration or cleanup operations.

        Attributes:
        - ctx: The BootContext providing configuration, store access, and
        environment dependencies.
        - provisioner: The Provisioner responsible for resource teardown during
        session/account cleanup.
        - _lock: A re‑entrant lock (RLock) to ensures thread‑safety and prevents concurrent
        mutation of session/account lifecycle state.
        - owner_id: A unique, stable identifier for this Expirer worker, constructed from
        the process ID and a short UUID fragment. 
            Used when:
            - claiming cleanup tasks (claimDueCleanupTask)
            - acquiring the sweep‑leadership lease (acquireSweepLease)
            - marking tasks as owned by this worker
        Guarantees that each worker has a distinct identity in the durable cleanup‑task queue.
        """
        self.ctx = ctx
        self.provisioner = provisioner
        self._lock = RLock()
        self.owner_id = f"cleanup-{os.getpid()}-{uuid4().hex[:12]}"

    def sweep(
        self,
        *,
        batch_size: int | None = None,
        time_budget_seconds: float | None = None,
        now: str | None = None,
        owner_id: str | None = None,
        stop: Event | None = None,
    ) -> dict[str, int]:
        """Claim and process due cleanup tasks until count or time budgets are spent."""
        
        # Get batch size limit if provided, if None fallback to config
        limit = batch_size if batch_size is not None else self.ctx.config.cleanup_batch_size
        
        # Get budget limit if provided, if None fallback to config
        budget = (
            time_budget_seconds
            if time_budget_seconds is not None
            else self.ctx.config.cleanup_time_budget_seconds
        )

        # Create result object for logging work done
        results = {
            "sessions_expired": 0,
            "sessions_cleaned": 0,
            "accounts_expired": 0,
            "accounts_cleaned": 0,
            "accounts_deleted": 0,
        }

        # Return if limit or budget is 0
        if limit <= 0 or budget <= 0:
            return results

        # Set up owner, time and task claimed
        claim_owner = owner_id or self.owner_id
        started_at = time.monotonic()
        claimed = 0

        while claimed < limit:
            if stop is not None and stop.is_set():
                break
            if time.monotonic() - started_at >= budget:
                break

            # Get current time to set task start time
            current = now or nowIso()

            # Claim a due task 
            task = self._claimDueTask(now=current, owner_id=claim_owner)
            if task is None:
                # If no due tasks are found, break
                break
            
            # Increment task claimed number
            claimed += 1

            # Process task
            category, _value = self._processClaimedTask(task, now=current)
            if category is not None:
                # Increment the work done based on the category
                results[category] += 1

        return results

    def markSessionExpired(self, session: SessionRecord, *, now: str | None = None) -> SessionRecord:
        """Persist a session transition into the expired state."""

        # Determine the timestamp to use for the expiration event if provided else use current time
        current = now or nowIso()

        # Serialize the state transition
        with self._lock:
            # Set the session as expired 
            session.state = SESSION_STATE_EXPIRED
            session.updated_at = current

            # Set expiration date if none
            if not session.expired_at:
                session.expired_at = current
            
            # Save the record to DB
            self.ctx.store.saveSession(session)

        # Return the updated session
        return session

    def markAccountExpired(self, account, *, now: str | None = None):
        """Persist an account transition into the expired state"""

        # Use time provided if not use current time
        current = now or nowIso()

        # Serialize the state transition with the lock
        with self._lock:

            # Set the account as expired
            account.status = ACCOUNT_STATE_EXPIRED

            # Update the account expired_at to current if none
            if not account.expired_at:
                account.expired_at = current

            # Save account to DB
            self.ctx.store.saveAccount(account)

        # Return updated account
        return account

    def expireSessions(
        self,
        *,
        limit: int | None = None,
        now: str | None = None,
    ) -> list[SessionRecord]:
        """Return expired sessions from `session_expire` tasks"""
        return [
            # Iterate through session expire task and return the record of that session
            record for record in self._drainTaskType(
                self.SESSION_EXPIRE_TASK,
                limit=limit,
                now=now,
            )
            if isinstance(record, SessionRecord)
        ]

    def cleanupExpiredSessions(
        self,
        *,
        limit: int | None = None,
        now: str | None = None,
    ) -> list[str]:
        """Return session ids from `session_cleanup` tasks"""
        return [
            session_id
            for session_id in self._drainTaskType(
                self.SESSION_CLEANUP_TASK,
                limit=limit,
                now=now,
            )
            if isinstance(session_id, str)
        ]

    def expireAccounts(
        self,
        *,
        limit: int | None = None,
        now: str | None = None,
    ) -> list[str]:
        """Return account ids from `account_expire` tasks"""
        return [
            account_aid
            for account_aid in self._drainTaskType(
                self.ACCOUNT_EXPIRE_TASK,
                limit=limit,
                now=now,
            )
            if isinstance(account_aid, str)
        ]

    def cleanupExpiredAccounts(
        self,
        *,
        limit: int | None = None,
        now: str | None = None,
    ) -> list[str]:
        """Return account ids from `account_cleanup` tasks"""
        return [
            account_aid
            for account_aid in self._drainTaskType(
                self.ACCOUNT_CLEANUP_TASK,
                limit=limit,
                now=now,
            )
            if isinstance(account_aid, str)
        ]

    def deleteExpiredAccounts(
        self,
        *,
        limit: int | None = None,
        now: str | None = None,
    ) -> list[str]:
        """Return account ids from `account_delete` tasks"""
        return [
            account_aid
            for account_aid in self._drainTaskType(
                self.ACCOUNT_DELETE_TASK,
                limit=limit,
                now=now,
            )
            if isinstance(account_aid, str)
        ]

    def failSession(
        self,
        *,
        session: SessionRecord,
        reason: str,
        account=None,
        teardown: bool = False,
    ) -> None:
        """Mark a session failed and optionally tear down any staged resources"""
        sessionFailed(session, reason)
        self.ctx.store.saveSession(session)
        logger.warning(
            f"Session {session.session_id} failed: {reason}",
        )

        failed = accountFailed(account)
        if failed is not None:
            self.ctx.store.saveAccount(failed)
            logger.warning(
                f"Account failed due to session failure for account AID {failed.account_aid}",
            )

        if teardown:
            logger.info(
                f"Session resource teardown initiated due to session failure for session {session.session_id}"
            )
            try:
                self.provisioner.teardownSessionResources(session=session, account=account)
            except BootError as exc:
                session.failure_reason = f"{reason} Cleanup failed: {exc}"
                session.updated_at = nowIso()
                self.ctx.store.saveSession(session)
                logger.warning(
                    f"Session resource teardown failed for {session.session_id}: {exc}",
                )

    def refreshSessionLease(self, session: SessionRecord) -> None:
        """Extend the TTL for a still-active session"""
        self.ctx.store.refreshSessionLease(session)
        logger.debug(f"Session lease refreshed for session {session.session_id}")

    def acquireSweepLease(self, *, owner_id: str | None = None, now: str | None = None) -> bool:
        """Attempt to become the active background cleanup leader"""
        with self._lock:
            return self.ctx.store.acquireLease(
                self.SWEEP_LEASE_NAME,
                owner_id=owner_id or self.owner_id,
                ttl_seconds=self.ctx.config.cleanup_leader_ttl_seconds,
                now=now or nowIso(),
            )

    def releaseSweepLease(self, *, owner_id: str | None = None) -> None:
        """Release background cleanup leadership for this worker"""
        with self._lock:
            self.ctx.store.releaseLease(
                self.SWEEP_LEASE_NAME,
                owner_id=owner_id or self.owner_id,
            )

    def _drainTaskType(
        self,
        task_kind: str,
        *,
        limit: int | None = None,
        now: str | None = None,
    ) -> list[object]:
        """Drain one cleanup task kind serially and collect non-empty results"""
        rows: list[object] = []
        claim_owner = self.owner_id

        while limit is None or len(rows) < limit:
            current = now or nowIso()
            task = self._claimDueTask(now=current, owner_id=claim_owner, kind=task_kind)
            if task is None:
                break

            _category, value = self._processClaimedTask(task, now=current)
            if value is not None:
                rows.append(value)

        return rows

    def _claimDueTask(
        self,
        *,
        now: str,
        owner_id: str,
        kind: str | None = None,
    ) -> CleanupTaskRecord | None:
        """Claim one due task from the durable queue"""
        with self._lock:
            return self.ctx.store.claimDueCleanupTask(
                now=now,
                owner_id=owner_id,
                claim_ttl_seconds=self.ctx.config.cleanup_task_claim_ttl_seconds,
                kind=kind,
            )

    def _processClaimedTask(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
    ) -> tuple[str | None, object | None]:
        """Dispatch a claimed task to its task-specific handler"""
        if task.kind == self.SESSION_EXPIRE_TASK:
            return self._processSessionExpire(task, now=now)
        if task.kind == self.SESSION_CLEANUP_TASK:
            return self._processSessionCleanup(task, now=now)
        if task.kind == self.ACCOUNT_EXPIRE_TASK:
            return self._processAccountExpire(task, now=now)
        if task.kind == self.ACCOUNT_CLEANUP_TASK:
            return self._processAccountCleanup(task, now=now)
        if task.kind == self.ACCOUNT_DELETE_TASK:
            return self._processAccountDelete(task, now=now)

        logger.warning(f"Unknown cleanup task kind '{task.kind}' for subject {task.subject}")
        
        # Task is invalid: task is not from the expected task list, mark as complete for removal
        self._completeTask(task.kind, task.subject)
        return None, None

    def _processSessionExpire(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
    ) -> tuple[str | None, object | None]:
        """Set a session as expired when its stored expiry time is due"""
        
        # Retrieve session
        session = self.ctx.store.getSession(task.subject)
        
        # If session is not found, it is an orphaned task, mark as complete for removal
        if session is None:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If session is in terminal states, mark as complete for removal
        if session.state in TERMINAL_SESSION_STATES:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If session does not have an expiration date, task is invalid so mark as complete for removal
        if not session.expires_at:
            self._completeTask(task.kind, task.subject)
            return None, None

        try:
            # Validate expiration date
            expires_at = datetime.fromisoformat(session.expires_at)
        except ValueError:
            logger.warning(
                f"Session {session.session_id} has invalid expires_at format: {session.expires_at}",
            )
            # Mark task as complete for removal
            self._completeTask(task.kind, task.subject)
            return None, None

        # If session is not due yet, reschedule task with the updated time
        if expires_at > datetime.fromisoformat(now):
            self._rescheduleTask(
                task.kind,
                task.subject,
                due_at=session.expires_at,
                now=now,
                reset_attempts=True,
            )
            return None, None

        # Mark the session as expired
        self.markSessionExpired(session, now=now)
        logger.info(
            f"Session expired for session {session.session_id}",
        )
        return "sessions_expired", session

    def _processSessionCleanup(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
    ) -> tuple[str | None, object | None]:
        """Tear down hosted resources for one expired session"""

        # Retrieve session
        session = self.ctx.store.getSession(task.subject)
        
        # If session is None, task is invalid, mark as complete for removal
        if session is None:
            self._completeTask(task.kind, task.subject)
            return None, None
        
        # If session is not expired, task is invalid
        if session.state != SESSION_STATE_EXPIRED:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If sesion's resources has not been cleaned up yet, task is invalid
        if session.resources_cleaned_at:
            self._completeTask(task.kind, task.subject)
            return None, None

        # Retrieve the account for that session
        account = self.ctx.store.getAccount(session.account_aid) if session.account_aid else None

        try:
            # Teardown session resources
            self.provisioner.teardownSessionResources(session=session, account=account)
        except BootError as exc:
            # If error, log and save it
            session.failure_reason = f"Cleanup failed after expiry: {exc}"
            session.updated_at = now
            with self._lock:
                self.ctx.store.saveSession(session)
                retryTime = self._nextRetryAt(task, now=now)
                # Reschedule session deletion
                self._rescheduleTask(
                    task.kind,
                    task.subject,
                    due_at=retryTime,
                    now=now,
                    last_error=str(exc),
                )
            logger.warning(
                f"Session resource teardown failed during expiry for session {session.session_id}: {exc}\n"
                f"Task was reschedule for {retryTime}"
            )
        else:
            # Update session record with the resources cleaned up time
            session.resources_cleaned_at = now
            session.updated_at = now
            with self._lock:
                self.ctx.store.saveSession(session)
                failed = accountFailed(account)
                if failed is not None:
                    self.ctx.store.saveAccount(failed)
                    logger.info(
                        f"Account failed due to session expiry for account {account.account_aid}",
                    )
                # Mark task as complete for removal
                self._completeTask(task.kind, task.subject)
            logger.info(
                f"Session resources cleaned after expiry for session {session.session_id}",
            )

        return "sessions_cleaned", session.session_id

    def _processAccountExpire(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
    ) -> tuple[str | None, object | None]:
        """Expire one onboarded account if its expiry time is due"""

        # Retrieve account for that task
        account = self.ctx.store.getAccount(task.subject)

        # If account is not found, task is invalid mark as complete for removal
        if account is None:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If account is not onboarded or does not have an expiry date, task is invalid
        if account.status != ACCOUNT_STATE_ONBOARDED or not account.expires_at:
            self._completeTask(task.kind, task.subject)
            return None, None

        try:
            # Validate expiration date
            expires_at = datetime.fromisoformat(account.expires_at)
        except ValueError:
            logger.warning(
                f"Account {account.account_aid} has invalid expires_at format: {account.expires_at}",
            )
            # Mark task as complete for removal
            self._completeTask(task.kind, task.subject)
            return None, None

        # If account is not yet due, reschedule task with the new date
        if expires_at > datetime.fromisoformat(now):
            self._rescheduleTask(
                task.kind,
                task.subject,
                due_at=account.expires_at,
                now=now,
                reset_attempts=True,
            )
            return None, None
        
        # Mark the account as expired
        self.markAccountExpired(account, now=now)
        logger.info(
            f"Account expired at {account.expires_at} for account AID {account.account_aid}",
        )
        return "accounts_expired", account.account_aid

    def _processAccountCleanup(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
    ) -> tuple[str | None, object | None]:
        """Tear down hosted resources for one expired account"""

        # Retrieve the account for that task
        account = self.ctx.store.getAccount(task.subject)

        # If account is not found, task is invalid, mark as complete for removal
        if account is None:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If account is not expired, task is invalid
        if account.status != ACCOUNT_STATE_EXPIRED:
            self._completeTask(task.kind, task.subject)
            return None, None
        
        # If account resources hasn't been cleaned up yet, task is invalid
        if account.resources_cleaned_at:
            with self._lock:
                # Save the account to trigger a reprocess of tasks based on its state
                self.ctx.store.saveAccount(account)
                self._completeTask(task.kind, task.subject)
            return None, None

        try:
            # Teardown Account resources
            self.provisioner.teardownAccountResources(
                account_aid=account.account_aid,
                account=account,
            )
        except BootError as exc:
            with self._lock:
                retryTime = self._nextRetryAt(task, now=now)
                # Reschedule task due to errorr
                self._rescheduleTask(
                    task.kind,
                    task.subject,
                    due_at=retryTime,
                    now=now,
                    last_error=str(exc),
                )
            logger.warning(
                f"Resource teardown failed for expired account {account.account_aid}: {exc}"
                f"Task was rescheduled for {retryTime}"
            )
        else:
            # Update account record with cleaned up time
            account.resources_cleaned_at = now
            with self._lock:
                self.ctx.store.saveAccount(account)
                self._completeTask(task.kind, task.subject)
            logger.info(
                f"Expired account resources cleaned for account AID {account.account_aid}",
            )

        return "accounts_cleaned", account.account_aid

    def _processAccountDelete(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
    ) -> tuple[str | None, object | None]:
        """Delete one expired account after cleanup and retention has been done"""

        # Retrieve account for that task
        account = self.ctx.store.getAccount(task.subject)

        # If account is not found, task is invalid, mark as completed for removal
        if account is None:
            self._completeTask(task.kind, task.subject)
            return None, None
        
        # If account is not expired, task is invalid
        if account.status != ACCOUNT_STATE_EXPIRED:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If account resources hasn't been cleaned up yet, task is invalid
        if not account.resources_cleaned_at:
            with self._lock:
                self.ctx.store.saveAccount(account)
                self._completeTask(task.kind, task.subject)
            return None, None

        # Retrieve time account should be deleted at
        delete_due_at = self._deleteDueAt(account)

        # If account deletion is not due yet, reschedule with the new date
        if delete_due_at > now:
            self._rescheduleTask(
                task.kind,
                task.subject,
                due_at=delete_due_at,
                now=now,
            )
            return None, None

        try:
            # Delete account
            self.provisioner.deleteAccount(
                account_aid=account.account_aid,
                account=account,
            )
        except BootError as exc:
            with self._lock:
                retryTime = self._nextRetryAt(task, now=now)
                self._rescheduleTask(
                    task.kind,
                    task.subject,
                    due_at=retryTime,
                    now=now,
                    last_error=str(exc),
                )
            logger.warning(
                f"Expired account deletion failed for account {account.account_aid}: {exc}"
                f"Task was rescheduled for {retryTime}"
            )
        else:
            with self._lock:
                self._completeTask(task.kind, task.subject)
            logger.info(
                f"Expired account deleted for account AID {account.account_aid}",
            )
            return "accounts_deleted", account.account_aid

        return None, None

    def _nextRetryAt(self, task: CleanupTaskRecord, *, now: str) -> str:
        """Compute the next retry time using exponential backoff and optional jitter"""
        base_delay = max(self.ctx.config.cleanup_failure_backoff_seconds, 0.0)
        max_delay = max(self.ctx.config.cleanup_failure_backoff_max_seconds, base_delay)
        attempt_number = max(task.attempt_count, 1)
        delay = base_delay * (2 ** (attempt_number - 1)) if base_delay > 0 else 0.0
        delay = min(delay, max_delay)

        jitter = max(self.ctx.config.cleanup_failure_jitter_seconds, 0.0)
        if jitter > 0:
            delay += random.uniform(0.0, jitter)

        return (datetime.fromisoformat(now) + timedelta(seconds=delay)).isoformat()

    def _deleteDueAt(self, account) -> str:
        """Compute the earliest deletion time for an expired, cleaned account.
        If expired_account_retention_seconds is not configured, the deletion is immediate.
        """
        # Get a base time either the account expiration time, resources cleaned or current
        anchor = account.expired_at or account.resources_cleaned_at or nowIso()

        # Get the retention time from the config if provided, if it is invalid or none = 0
        retention = max(self.ctx.config.expired_account_retention_seconds, 0.0)
        if retention <= 0:
            # Return the anchor which would essentially be an immediate deletion
            return anchor
        return (datetime.fromisoformat(anchor) + timedelta(seconds=retention)).isoformat()

    def _rescheduleTask(
        self,
        kind: str,
        subject: str,
        *,
        due_at: str,
        now: str,
        last_error: str | None = None,
        reset_attempts: bool = False,
    ) -> None:
        """Saves a new due time for a task after deferral or failure in the DB"""
        with self._lock:
            self.ctx.store.scheduleCleanupTask(
                kind,
                subject,
                due_at=due_at,
                now=now,
                last_error=last_error,
                reset_attempts=reset_attempts,
            )

    def _completeTask(self, kind: str, subject: str) -> None:
        """Remove a task from the durable queue once it has finished"""
        with self._lock:
            self.ctx.store.completeCleanupTask(kind, subject)
