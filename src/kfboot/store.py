from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable
from urllib.parse import urlsplit

from kfboot.basing import (
    CLEANUP_TASK_ACCOUNT_CLEANUP,
    CLEANUP_TASK_ACCOUNT_DELETE,
    CLEANUP_TASK_ACCOUNT_EXPIRE,
    CLEANUP_TASK_SESSION_CLEANUP,
    CLEANUP_TASK_SESSION_DELETE,
    CLEANUP_TASK_SESSION_EXPIRE,
    ACCOUNT_STATE_FAILED,
    ACCOUNT_STATE_EXPIRED,
    ACCOUNT_STATE_ONBOARDED,
    ACCOUNT_STATE_PENDING_ONBOARDING,
    CleanupDueRecord,
    CleanupTaskRecord,
    SESSION_STATE_CANCELLED,
    SESSION_STATE_COMPLETED,
    SESSION_STATE_EXPIRED,
    SESSION_STATE_FAILED,
    TERMINAL_SESSION_STATES,
    AccountRecord,
    BindingRecord,
    ResourceRecord,
    QuotaRecord,
    SessionRecord,
    open_baser,
)


def nowIso() -> str:
    return datetime.now(UTC).isoformat()


def parsePublicUrl(url: str) -> tuple[str, int | None]:
    parts = urlsplit(url)
    return parts.hostname or "", parts.port


def _sortValue(value: Any) -> Any:
    if value is None:
        return ""
    return value


def _resourceValue(record: Any, field: str, default: Any = "") -> Any:
    return getattr(record, field, default)


def _resourceToApi(record: ResourceRecord, *, include_boot_url: bool = False) -> dict[str, Any]:
    data = {
        "kind": _resourceValue(record, "kind", ""),
        "eid": _resourceValue(record, "eid", ""),
        "cid": _resourceValue(record, "cid", ""),
        "name": _resourceValue(record, "name", ""),
        "identifier_alias": _resourceValue(record, "identifier_alias", ""),
        "region_id": _resourceValue(record, "region_id", ""),
        "region_name": _resourceValue(record, "region_name", ""),
        "url": _resourceValue(record, "url", ""),
        "boot_url": _resourceValue(record, "boot_url", ""),
        "public_host": _resourceValue(record, "public_host", ""),
        "public_port": _resourceValue(record, "public_port", None),
        "status": _resourceValue(record, "status", ""),
        "created_at": _resourceValue(record, "created_at", ""),
    }
    if not include_boot_url:
        data.pop("boot_url", None)
    data["oobis"] = list(_resourceValue(record, "oobis", []) or [])
    if _resourceValue(record, "kind", "") == "witness":
        data["witness_url"] = _resourceValue(record, "url", "")
    elif _resourceValue(record, "kind", "") == "watcher":
        data["watcher_url"] = _resourceValue(record, "url", "")
    return data


class Store:
    """
    Persistent state manager for sessions, accounts, resources, and cleanup tasks.

    Responsibilities:
    - Maintain lifecycle‑driven cleanup task graphs.
    - Keep cleanup_tasks and cleanup_due tables synchronized.
    - Provide atomic, idempotent state transitions for sessions and accounts.
    - Track cleanup work in progress so a single runner can recover after restarts.

    Attributes:
    - baser:
        Underlying LMDB‑backed key/value store. All persistent state
        (sessions, accounts, tasks, resources) is stored here.

    - session_ttl_seconds:
        Default TTL applied to newly created sessions. Used by
        refreshSessionLease() and createSession() to compute expires_at.

    - expired_account_retention_seconds:
        Retention window applied after an account reaches EXPIRED and its
        resources are cleaned. Determines when account_delete tasks become due.
    """
    def __init__(
        self,
        path: str,
        *,
        session_ttl_seconds: int = 300,
        account_ttl_seconds: float = 0.0,
        closed_session_retention_seconds: float = 0.0,
        expired_account_retention_seconds: float = 0.0,
    ):
        self.baser = open_baser(path)
        self.session_ttl_seconds = session_ttl_seconds
        self.account_ttl_seconds = account_ttl_seconds
        self.closed_session_retention_seconds = closed_session_retention_seconds
        self.expired_account_retention_seconds = expired_account_retention_seconds

    def close(self) -> None:
        self.baser.close()

    def _sessionPastDue(self, record: SessionRecord, *, now: str | None = None) -> bool:
        """Return True when a non-terminal session has reached its expiry timestamp."""
        if record.state in TERMINAL_SESSION_STATES or not record.expires_at:
            return False

        try:
            expires_at = _parseDt(record.expires_at)
        except ValueError:
            # Invalid expiry metadata should fail closed so corrupted sessions do
            # not remain active forever or keep bypassing cleanup accounting.
            return True

        return expires_at <= _parseDt(now or nowIso())

    def _sessionIsActive(self, record: SessionRecord, *, now: str | None = None) -> bool:
        """Return True when the session is still usable for admission purposes"""
        return record.state not in TERMINAL_SESSION_STATES and not self._sessionPastDue(record, now=now)

    def _sessionHasCleanupDebt(self, record: SessionRecord) -> bool:
        """Check if a closed session's resources still needs to be cleaned"""
        return (
            record.state in {
                SESSION_STATE_EXPIRED,
                SESSION_STATE_FAILED,
                SESSION_STATE_CANCELLED,
            }
            and not record.resources_cleaned_at
        )

    def _sessionConsumesAdmission(self, record: SessionRecord, *, now: str | None = None) -> bool:
        """Return True when a session should still count against onboarding admission.

        Active sessions count towards onboarding capacity, but closed sessions that
        have not finished cleanup still tie up hosted resources. We keep counting that
        cleanup debt so callers cannot rotate principals or aliases to hoard capacity
        while the sweeper is still reclaiming the previous session's resources.
        """
        return self._sessionIsActive(record, now=now) or self._sessionHasCleanupDebt(record)

    def getQuota(self, scope: str, subject: str) -> QuotaRecord | None:
        return self.baser.quotas.get(keys=(scope, subject))

    def saveQuota(self, record: QuotaRecord) -> None:
        self.baser.quotas.pin(keys=(record.scope, record.subject), val=record)

    def deleteQuota(self, scope: str, subject: str) -> None:
        self.baser.quotas.rem(keys=(scope, subject))

    def getCleanupTask(self, kind: str, subject: str) -> CleanupTaskRecord | None:
        """Return the cleanup task for a specific kind/subject pair"""
        return self.baser.cleanup_tasks.get(keys=(kind, subject))

    def ensureCleanupTask(
        self,
        kind: str,
        subject: str,
        *,
        due_at: str,
        now: str | None = None,
    ) -> CleanupTaskRecord:
        """Create a cleanup task if one does not already exist for that kind and subject"""

        # Check if a cleanup task exist for this kind/subject pair
        existing = self.getCleanupTask(kind, subject)
        if existing is not None:

            # Return if yes
            return existing

        # Get current time for the cleanup task record
        current = now or nowIso()

        # Create cleanup task record
        record = CleanupTaskRecord(
            kind=kind,
            subject=subject,
            due_at=due_at,
            created_at=current,
            updated_at=current,
        )

        # Save the task in the db and return it
        self._saveCleanupTask(record)
        return record

    def scheduleCleanupTask(
        self,
        kind: str,
        subject: str,
        *,
        due_at: str,
        now: str | None = None,
        last_error: str | None = None,
        reset_attempts: bool = False,
    ) -> CleanupTaskRecord:
        """Create a clean up task or if one exist already exists for that kind/subject pair, 
        Update the cleanup task and move it to a new due time
        
        Responsibilities:
        - Rewrite the task's `due_at` timestamp.
        - Clear in-progress metadata (`claimed_at`).
        - Optionally reset `attempt_count` and update `last_error`.
        - Maintain consistency between `cleanup_tasks` and `cleanup_due`.
        """

        # Get current time
        current = now or nowIso()

        # Get the record for the kind/subject pair
        record = self.getCleanupTask(kind, subject)
        previous_due_at = ""
        
        if record is None:
            # Create record if none is found
            record = CleanupTaskRecord(
                kind=kind,
                subject=subject,
                due_at=due_at,
                created_at=current,
                updated_at=current,
            )
        else:
            # If it exists, update the record and move its due time forward with the provided due_at
            previous_due_at = record.due_at
            record.due_at = due_at
            record.updated_at = current
            record.claimed_at = ""
            if reset_attempts:
                # Reset number of attempts if flag is provided
                record.attempt_count = 0
            if last_error is not None:
                record.last_error = last_error
        # Save record with its previous due variable and return it
        self._saveCleanupTask(record, previous_due_at=previous_due_at)
        return record

    def completeCleanupTask(self, kind: str, subject: str) -> None:
        """Remove a cleanup task from both the task table and due-time index"""

        # Get the record for that kind/subject pair
        record = self.getCleanupTask(kind, subject)
        if record is None:
            # Return if record does not exist
            return
        if record.due_at:
            # If record due date exist, remove it from the cleanup due table
            self.baser.cleanup_due.rem(keys=(_dueIndexKey(record.due_at), kind, subject))
        # Remove it from the cleanup task table
        self.baser.cleanup_tasks.rem(keys=(kind, subject))

    def cleanupBacklogSnapshot(self, *, now: str | None = None) -> dict[str, Any]:
        """Return a lightweight snapshot of cleanup queue health reporting.

        This walks the durable task table directly instead of depending on the due-time
        index so health remains informative even if the due index ever contains stale rows.
        """

        current = now or nowIso()
        current_dt = _parseDt(current)
        pending_tasks = 0
        due_tasks = 0
        claimed_tasks = 0
        oldest_due_dt: datetime | None = None
        oldest_claimed_dt: datetime | None = None

        for _, task in self.baser.cleanup_tasks.getTopItemIter(keys=()):
            pending_tasks += 1
            # Claimed tasks are in progress for the single runner, so they should
            # be reported separately from due work even if they were originally due.
            if task.claimed_at:
                claimed_tasks += 1
                try:
                    claimed_dt = _parseDt(task.claimed_at or task.updated_at or task.created_at)
                except ValueError:
                    claimed_dt = None
                if claimed_dt is not None and (oldest_claimed_dt is None or claimed_dt < oldest_claimed_dt):
                    oldest_claimed_dt = claimed_dt
                continue
            if not task.due_at:
                continue
            try:
                task_due_dt = _parseDt(task.due_at)
            except ValueError:
                continue
            if task_due_dt > current_dt:
                continue
            due_tasks += 1
            if oldest_due_dt is None or task_due_dt < oldest_due_dt:
                oldest_due_dt = task_due_dt

        oldest_due_at = oldest_due_dt.isoformat() if oldest_due_dt is not None else None
        oldest_due_age_seconds = (
            max((current_dt - oldest_due_dt).total_seconds(), 0.0)
            if oldest_due_dt is not None
            else None
        )
        oldest_claimed_at = oldest_claimed_dt.isoformat() if oldest_claimed_dt is not None else None
        oldest_claimed_age_seconds = (
            max((current_dt - oldest_claimed_dt).total_seconds(), 0.0)
            if oldest_claimed_dt is not None
            else None
        )
        return {
            "pending_tasks": pending_tasks,
            "due_tasks": due_tasks,
            "claimed_tasks": claimed_tasks,
            "oldest_due_at": oldest_due_at,
            "oldest_due_age_seconds": oldest_due_age_seconds,
            "oldest_claimed_at": oldest_claimed_at,
            "oldest_claimed_age_seconds": oldest_claimed_age_seconds,
        }

    def requeueClaimedCleanupTasks(self, *, now: str | None = None) -> int:
        """Make previously claimed tasks immediately visible again.

        In single-runner mode, claim metadata only means "this task was in
        progress." If the process restarted before clearing that marker, we
        immediately requeue the task instead of waiting for an artificial lease.
        """

        current = now or nowIso()
        recovered = 0
        tasks = [task for _, task in self.baser.cleanup_tasks.getTopItemIter(keys=())]

        for task in tasks:
            if not task.claimed_at:
                continue

            previous_due_at = task.due_at
            task.due_at = current
            task.updated_at = current
            task.claimed_at = ""
            self._saveCleanupTask(task, previous_due_at=previous_due_at)
            recovered += 1

        return recovered

    def listDueCleanupTasks(
        self,
        *,
        now: str | None = None,
        kind: str | None = None,
        limit: int | None = None,
    ) -> list[CleanupTaskRecord]:
        """Return due cleanup tasks ordered by due-time"""
        
        # Get current time and create a key with it to compare it with the key of due tasks
        current = now or nowIso()
        current_key = _dueIndexKey(current)

        # Create two lists for due tasks and stale tasks
        rows: list[CleanupTaskRecord] = []
        stale: list[tuple[str, ...]] = []

        # Iterate through the due cleanup tasks
        for keys, _record in self.baser.cleanup_due.getTopItemIter(keys=()):
            due_key, task_kind, subject = keys[-3:]
            # Check if a task is due by checking if its key is 'superior' to the key we created with the current time
            if due_key > current_key:
                # Task is not due yet, break
                break
            if kind is not None and task_kind != kind:
                # Task is in a different scope, skip
                continue

            # Task is due, retrieve it
            task = self.getCleanupTask(task_kind, subject)
            if task is None or _dueIndexKey(task.due_at) != due_key:
                # Task cannot be found, append to stale
                stale.append(keys)
                continue

            # Append task to rows list
            rows.append(task)
            if limit is not None and len(rows) >= limit:
                # If the number of items inside the task list reaches the batch size limit, break
                break
        
        # Clean up stale tasks
        for keys in stale:
            self.baser.cleanup_due.rem(keys=keys)

        # Return the list of due tasks
        return rows

    def claimDueCleanupTask(
        self,
        *,
        now: str | None = None,
        kind: str | None = None,
    ) -> CleanupTaskRecord | None:
        """Claim one due cleanup task and mark it as in progress.

        In the supported single-runner model, claiming a task does not create a
        time-based lease. Instead, the task is simply removed from the due queue
        while it is being worked, and startup recovery requeues any task that
        still carries claim metadata after a crash.
        """

        # Get current time and create a key for it
        current = now or nowIso()
        current_key = _dueIndexKey(current)
        stale: list[tuple[str, ...]] = []

        # Iterate through due cleanup tasks
        for keys, _record in self.baser.cleanup_due.getTopItemIter(keys=()):
            
            # Check if a task is due by checking if its key is 'superior' to the key we created with the current time
            due_key, task_kind, subject = keys[-3:]
            if due_key > current_key:
                break
            if kind is not None and task_kind != kind:
                continue

            # Retrieve task
            task = self.getCleanupTask(task_kind, subject)
            if task is None or _dueIndexKey(task.due_at) != due_key:
                # Task is stale, append it to the stale list
                stale.append(keys)
                continue

            # Update the task fields
            previous_due_at = task.due_at
            
            # Remove the task from the due queue while the single runner is
            # actively processing it. Retries or startup recovery will assign a
            # new due_at if the work is not completed successfully.
            task.due_at = ""
            task.updated_at = current
            task.claimed_at = current
            task.last_attempt_at = current
            
            # Increase attempt count
            task.attempt_count += 1

            # Save record to DB
            self._saveCleanupTask(task, previous_due_at=previous_due_at)
            
            # Cleanup stale tasks
            for stale_keys in stale:
                self.baser.cleanup_due.rem(keys=stale_keys)

            # Return claimed due task
            return task
        
        # If no valid task was found, still clean up the stale tasks and return None
        for keys in stale:
            self.baser.cleanup_due.rem(keys=keys)

        return None

    def _saveCleanupTask(
        self,
        record: CleanupTaskRecord,
        *,
        previous_due_at: str | None = None,
    ) -> None:
        """Persist a cleanup task and keep the due-time table synchronized
        Responsibilities:
        - Write to cleanup_tasks.
        - Insert/update cleanup_due entry.
        - Remove stale due‑time entries when rescheduling
        """

        # Get previous due date if provided
        previous = previous_due_at if previous_due_at is not None else ""

        # If the task was rescheduled (due_at changed), remove the old index entry.
        # This prevents stale entries from polluting the due-time index.
        if previous and previous != record.due_at:
            self.baser.cleanup_due.rem(
                keys=(_dueIndexKey(previous), record.kind, record.subject)
            )
        # Update record
        self.baser.cleanup_tasks.pin(keys=(record.kind, record.subject), val=record)
        
        # Insert/update in the cleanup due table if the task has a due_at timestamp.
        if record.due_at:
            self.baser.cleanup_due.pin(
                keys=(_dueIndexKey(record.due_at), record.kind, record.subject),
                val=CleanupDueRecord(
                    kind=record.kind,
                    subject=record.subject,
                    due_at=record.due_at,
                ),
            )

    def _deleteCleanupTaskIfPresent(self, kind: str, subject: str) -> None:
        """Delete a cleanup task if it exists"""

        # Retrieve cleanup task, return if None
        record = self.getCleanupTask(kind, subject)
        if record is None:
            return

        # Mark as Complete
        self.completeCleanupTask(kind, subject)

    def _syncSessionTasks(self, record: SessionRecord) -> None:
        """Create a task queue based on the session state change.
        If a session is:
        - Expired/failed/cancelled and not cleaned => creates a cleanup task
        - Closed and cleaned => schedule final session deletion
        - Completed => schedule final session deletion
        - Still Active and has an expiry date => schedule session expiry with session_expire
        - In terminal or no expiry => remove expire task
        """
        cleanup_states = {
            SESSION_STATE_EXPIRED,
            SESSION_STATE_FAILED,
            SESSION_STATE_CANCELLED,
        }

        # Check if a session is in a cleanup state
        if record.state in cleanup_states:

            # Clean up stale task if it exists
            self._deleteCleanupTaskIfPresent(CLEANUP_TASK_SESSION_EXPIRE, record.session_id)
            
            # If the session's resources were cleaned up
            if record.resources_cleaned_at:

                # Clean up stale task
                self._deleteCleanupTaskIfPresent(CLEANUP_TASK_SESSION_CLEANUP, record.session_id)
                
                # Create a session delete task
                self.ensureCleanupTask(
                    CLEANUP_TASK_SESSION_DELETE,
                    record.session_id,
                    due_at=self.sessionDeleteDueAt(record),
                    now=record.resources_cleaned_at or record.updated_at or None,
                )
            # If the session's resources were not cleaned up
            else:
                # Clean up stale task
                self._deleteCleanupTaskIfPresent(CLEANUP_TASK_SESSION_DELETE, record.session_id)
                
                # Create a session clean up task
                self.ensureCleanupTask(
                    CLEANUP_TASK_SESSION_CLEANUP,
                    record.session_id,
                    due_at=record.expired_at or record.updated_at or nowIso(),
                    now=record.updated_at or None,
                )
            return

        # If the session is in a completed state
        if record.state == SESSION_STATE_COMPLETED:

            # Delete any session stale expiry or clean up task 
            self._deleteCleanupTaskIfPresent(CLEANUP_TASK_SESSION_EXPIRE, record.session_id)
            self._deleteCleanupTaskIfPresent(CLEANUP_TASK_SESSION_CLEANUP, record.session_id)
            
            # Create a session delete task
            # Sessions in completed state no longer have its hosted resources, it is just metadata
            # which justifies skipping cleaning straight to deletion
            self.ensureCleanupTask(
                CLEANUP_TASK_SESSION_DELETE,
                record.session_id,
                due_at=self.sessionDeleteDueAt(record),
                now=record.updated_at or None,
            )
            return
        
        # Clean up stale session cleanup and delete task
        self._deleteCleanupTaskIfPresent(CLEANUP_TASK_SESSION_CLEANUP, record.session_id)
        self._deleteCleanupTaskIfPresent(CLEANUP_TASK_SESSION_DELETE, record.session_id)

        # Defensive check or if we add another terminal state 
        # If session is in a terminal state or it does not have an expiry date
        if record.state in TERMINAL_SESSION_STATES or not record.expires_at:

            # Clean up any stale task for expiry and return
            self._deleteCleanupTaskIfPresent(CLEANUP_TASK_SESSION_EXPIRE, record.session_id)
            return

        # Create a a task for session expiry
        self.scheduleCleanupTask(
            CLEANUP_TASK_SESSION_EXPIRE,
            record.session_id,
            due_at=record.expires_at,
            now=record.updated_at or None,
            reset_attempts=True,
        )

    def _syncAccountTasks(self, record: AccountRecord) -> None:
        """Create a task queue based on the account state change.
        If a account is:
        - Expired/failed and not cleaned => schedule account cleanup with account_cleanup
        - Expired/failed and cleaned => schedule account delete with account_delete
        - Onboarded and has an expiry date => schedule account expiry with account_expire
        - Anything else => clear stale tasks
        """
        closed_states = {
            ACCOUNT_STATE_EXPIRED,
            ACCOUNT_STATE_FAILED,
        }

        # Failed pending accounts still tied to a failed/expired/cancelled session should
        # let the session cleanup phase own teardown. This avoids scheduling a second
        # account_cleanup task on the same resources while the session cleanup
        # task is already responsible for cleaning it.
        if (
            record.status == ACCOUNT_STATE_FAILED
            and record.session_id
            and not record.resources_cleaned_at
        ):
            linked_session = self.getSession(record.session_id)
            if (
                linked_session is not None
                and linked_session.state in {
                    SESSION_STATE_EXPIRED,
                    SESSION_STATE_FAILED,
                    SESSION_STATE_CANCELLED,
                }
                and not linked_session.resources_cleaned_at
            ):
                self._deleteCleanupTaskIfPresent(CLEANUP_TASK_ACCOUNT_EXPIRE, record.account_aid)
                self._deleteCleanupTaskIfPresent(CLEANUP_TASK_ACCOUNT_CLEANUP, record.account_aid)
                self._deleteCleanupTaskIfPresent(CLEANUP_TASK_ACCOUNT_DELETE, record.account_aid)
                return

        if record.status in closed_states:
            self._deleteCleanupTaskIfPresent(CLEANUP_TASK_ACCOUNT_EXPIRE, record.account_aid)
            if record.resources_cleaned_at:
                self._deleteCleanupTaskIfPresent(CLEANUP_TASK_ACCOUNT_CLEANUP, record.account_aid)
                self.ensureCleanupTask(
                    CLEANUP_TASK_ACCOUNT_DELETE,
                    record.account_aid,
                    due_at=self.accountDeleteDueAt(record),
                    now=record.resources_cleaned_at or None,
                )
            else:
                self._deleteCleanupTaskIfPresent(CLEANUP_TASK_ACCOUNT_DELETE, record.account_aid)
                self.ensureCleanupTask(
                    CLEANUP_TASK_ACCOUNT_CLEANUP,
                    record.account_aid,
                    due_at=record.expired_at or record.created_at or nowIso(),
                    now=record.expired_at or None,
                )
            return

        # Delete stale tasks
        self._deleteCleanupTaskIfPresent(CLEANUP_TASK_ACCOUNT_CLEANUP, record.account_aid)
        self._deleteCleanupTaskIfPresent(CLEANUP_TASK_ACCOUNT_DELETE, record.account_aid)
        
        if record.status != ACCOUNT_STATE_ONBOARDED or not record.expires_at:
            # If account is not onboarded or does not have an expiry date, return
            self._deleteCleanupTaskIfPresent(CLEANUP_TASK_ACCOUNT_EXPIRE, record.account_aid)
            return

        # If the account is onboarded and has an expiry date, schedule its expiry
        self.scheduleCleanupTask(
            CLEANUP_TASK_ACCOUNT_EXPIRE,
            record.account_aid,
            due_at=record.expires_at,
            now=record.onboarded_at or record.created_at or None,
            reset_attempts=True,
        )

    def accountDeleteDueAt(self, record: AccountRecord) -> str:
        """Compute when a closed account becomes eligible for final deletion."""

        anchor = record.expired_at or record.resources_cleaned_at or nowIso()
        retention = max(self.expired_account_retention_seconds, 0.0)
        if retention <= 0:
            return anchor
        return (_parseDt(anchor) + timedelta(seconds=retention)).isoformat()

    def sessionDeleteDueAt(self, record: SessionRecord) -> str:
        """Compute when a closed session becomes eligible for final deletion."""
        anchor = (
            record.resources_cleaned_at
            or record.updated_at
            or record.expired_at
            or record.created_at
            or nowIso()
        )
        retention = max(self.closed_session_retention_seconds, 0.0)
        if retention <= 0:
            return anchor
        return (_parseDt(anchor) + timedelta(seconds=retention)).isoformat()

    def createSession(
        self,
        *,
        ephemeral_aid: str,
        account_aid: str,
        account_alias: str,
        chosen_profile_code: str,
        client_ip: str,
        region_id: str,
        region_name: str,
        watcher_required: bool,
        witness_count: int,
        toad: int,
        account_tier: str,
    ) -> SessionRecord:
        created_at = nowIso()
        expires_at = (_parseDt(created_at) + timedelta(seconds=self.session_ttl_seconds)).isoformat()
        record = SessionRecord(
            session_id=_newSessionId(),
            ephemeral_aid=ephemeral_aid,
            account_aid=account_aid,
            account_alias=account_alias,
            state="started",
            created_at=created_at,
            updated_at=created_at,
            expires_at=expires_at,
            client_ip=client_ip,
            chosen_profile_code=chosen_profile_code,
            watcher_required=watcher_required,
            region_id=region_id,
            region_name=region_name,
            witness_count=witness_count,
            toad=toad,
            account_tier=account_tier,
        )
        self.saveSession(record)
        return record

    def saveSession(self, record: SessionRecord) -> None:
        if record.state not in TERMINAL_SESSION_STATES and record.expires_at:
            try:
                _parseDt(record.expires_at)
            except ValueError:
                # Fail closed on corrupted session expiry metadata so cleanup can
                # reclaim staged resources instead of leaving the session open forever.
                current = nowIso()
                record.state = SESSION_STATE_EXPIRED
                record.expired_at = record.expired_at or current
                record.updated_at = current

        # Update session 
        self.baser.sessions.pin(keys=(record.session_id,), val=record)

        # Check for session cleanup tasks based on the newly updated session state
        self._syncSessionTasks(record)

    def refreshSessionLease(self, record: SessionRecord, *, now: str | None = None) -> None:
        current = _parseDt(now or nowIso())
        record.updated_at = current.isoformat()
        if record.state not in TERMINAL_SESSION_STATES:
            record.expires_at = (
                current + timedelta(seconds=self.session_ttl_seconds)
            ).isoformat()
        self.saveSession(record)

    def refreshAccountLease(self, record: AccountRecord, *, now: str | None = None) -> None:
        """Extend the idle TTL for a still-active onboarded account."""

        # Get current time
        current = _parseDt(now or nowIso())

        # Check if account status is valid for refresh lease
        if record.status != ACCOUNT_STATE_ONBOARDED or self.account_ttl_seconds <= 0:
            return

        # Check if the account has an expiration date
        if record.expires_at:
            try:
                # Check if the account is expired, if so return to not refresh its lease
                if _parseDt(record.expires_at) <= current:
                    return
            except ValueError:
                return
        
        # Refresh the lease by setting its expiration date to current time + account TTL
        record.expires_at = (
            current + timedelta(seconds=self.account_ttl_seconds)
        ).isoformat()

        # Save it into DB
        self.saveAccount(record)

    def getSession(self, session_id: str) -> SessionRecord | None:
        return self.baser.sessions.get(keys=(session_id,))

    def findActiveSessionForEphemeral(self, ephemeral_aid: str) -> SessionRecord | None:
        latest: SessionRecord | None = None
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.ephemeral_aid != ephemeral_aid:
                continue
            if not self._sessionIsActive(record):
                continue
            if latest is None or record.created_at > latest.created_at:
                latest = record
        return latest

    def findSessionForEphemeral(self, ephemeral_aid: str) -> SessionRecord | None:
        latest: SessionRecord | None = None
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.ephemeral_aid != ephemeral_aid:
                continue
            if latest is None or record.created_at > latest.created_at:
                latest = record
        return latest

    def findSessionForAccount(self, account_aid: str) -> SessionRecord | None:
        latest: SessionRecord | None = None
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.account_aid != account_aid:
                continue
            if latest is None or record.created_at > latest.created_at:
                latest = record
        return latest

    def listSessionsForAccount(self, account_aid: str) -> list[SessionRecord]:
        rows = []
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.account_aid != account_aid:
                continue
            rows.append(record)
        rows.sort(key=lambda record: _sortValue(record.created_at), reverse=True)
        return rows

    def saveAccount(self, record: AccountRecord) -> None:
        if record.status == ACCOUNT_STATE_ONBOARDED and record.expires_at:
            try:
                _parseDt(record.expires_at)
            except ValueError:
                # Fail closed on corrupted account expiry metadata so the account
                # cannot remain indefinitely active while automatic cleanup is disabled.
                current = nowIso()
                record.status = ACCOUNT_STATE_EXPIRED
                record.expired_at = record.expired_at or current

        # Save the account in DB
        self.baser.accounts.pin(keys=(record.account_aid,), val=record)
        # Check for account task based on the newly saved session state
        self._syncAccountTasks(record)

    def getAccount(self, account_aid: str) -> AccountRecord | None:
        return self.baser.accounts.get(keys=(account_aid,))

    def deleteAccount(self, account_aid: str) -> None:
        # Remove cleanup task related to this account
        self._deleteCleanupTaskIfPresent(CLEANUP_TASK_ACCOUNT_EXPIRE, account_aid)
        self._deleteCleanupTaskIfPresent(CLEANUP_TASK_ACCOUNT_CLEANUP, account_aid)
        self._deleteCleanupTaskIfPresent(CLEANUP_TASK_ACCOUNT_DELETE, account_aid)

        # Remove Account from the DB
        self.baser.accounts.rem(keys=(account_aid,))

    def listAccounts(self) -> list[AccountRecord]:
        return [record for _, record in self.baser.accounts.getTopItemIter(keys=())]

    def listAccountsForAlias(self, account_alias: str) -> list[AccountRecord]:
        """Return a list of AccountRecords matching the given account alias"""
        rows: list[AccountRecord] = []
        for _, record in self.baser.accounts.getTopItemIter(keys=()):
            if record.account_alias == account_alias:
                rows.append(record)
        rows.sort(key=lambda record: _sortValue(record.created_at), reverse=True)
        return rows

    def listActiveSessionsForIp(self, client_ip: str) -> list[SessionRecord]:
        rows = []
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.client_ip != client_ip:
                continue
            if not self._sessionIsActive(record):
                continue
            rows.append(record)
        rows.sort(key=lambda record: _sortValue(record.created_at), reverse=True)
        return rows

    def listAdmissionSessionsForIp(self, client_ip: str, *, now: str | None = None) -> list[SessionRecord]:
        """Return sessions that still consume onboarding capacity for this IP."""
        rows = []
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.client_ip != client_ip:
                continue
            if not self._sessionConsumesAdmission(record, now=now):
                continue
            rows.append(record)
        rows.sort(key=lambda record: _sortValue(record.created_at), reverse=True)
        return rows

    def listActiveSessionsForAlias(self, account_alias: str) -> list[SessionRecord]:
        """Return a list of active SessionRecords matching the given account alias"""
        rows = []
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.account_alias != account_alias:
                continue
            if not self._sessionIsActive(record):
                continue
            rows.append(record)
        rows.sort(key=lambda record: _sortValue(record.created_at), reverse=True)
        return rows

    def listAdmissionSessionsForAlias(self, account_alias: str, *, now: str | None = None) -> list[SessionRecord]:
        """Return alias sessions that still occupy onboarding capacity."""
        rows = []
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.account_alias != account_alias:
                continue
            if not self._sessionConsumesAdmission(record, now=now):
                continue
            rows.append(record)
        rows.sort(key=lambda record: _sortValue(record.created_at), reverse=True)
        return rows

    def addBinding(self, principal: str, cid: str) -> None:
        self.baser.bindings.pin(
            keys=(principal, cid),
            val=BindingRecord(principal=principal, cid=cid),
        )

    def deleteBindingsForPrincipal(self, principal: str) -> None:
        matches = [
            keys
            for keys, _record in self.baser.bindings.getTopItemIter(keys=())
            if keys and keys[0] == principal
        ]
        for keys in matches:
            self.baser.bindings.rem(keys=keys)

    def addResource(self, record: ResourceRecord) -> None:
        self.baser.resources.pin(keys=(record.kind, record.eid), val=record)

    def saveResource(self, record: ResourceRecord) -> None:
        self.addResource(record)

    def getResource(self, kind: str, eid: str) -> ResourceRecord | None:
        return self.baser.resources.get(keys=(kind, eid))

    def getResources(self, kind: str, eids: Iterable[str]) -> list[ResourceRecord]:
        rows = []
        for eid in eids:
            record = self.getResource(kind, eid)
            if record is not None:
                rows.append(record)
        return rows

    def deleteResource(self, kind: str, eid: str) -> None:
        self.baser.resources.rem(keys=(kind, eid))

    def deleteSession(self, session_id: str) -> None:
        # Delete any cleanup task related to that session
        self._deleteCleanupTaskIfPresent(CLEANUP_TASK_SESSION_EXPIRE, session_id)
        self._deleteCleanupTaskIfPresent(CLEANUP_TASK_SESSION_CLEANUP, session_id)
        self._deleteCleanupTaskIfPresent(CLEANUP_TASK_SESSION_DELETE, session_id)

        # Remove session from the DB
        self.baser.sessions.rem(keys=(session_id,))

    def countResources(self, kind: str) -> int:
        return sum(1 for _, _ in self.baser.resources.getTopItemIter(keys=(kind,), topive=True))

    def listResourcesForAccount(self, *, kind: str, account_aid: str) -> list[ResourceRecord]:
        rows = []
        for _, record in self.baser.resources.getTopItemIter(keys=(kind,), topive=True):
            if _resourceValue(record, "principal", "") == account_aid:
                rows.append(record)
        rows.sort(key=lambda record: _sortValue(_resourceValue(record, "created_at", "")), reverse=True)
        return rows

    def listResourcesForSession(self, *, kind: str, session_id: str) -> list[ResourceRecord]:
        rows = []
        for _, record in self.baser.resources.getTopItemIter(keys=(kind,), topive=True):
            if _resourceValue(record, "session_id", "") == session_id:
                rows.append(record)
        rows.sort(key=lambda record: _sortValue(_resourceValue(record, "created_at", "")), reverse=True)
        return rows

    def bindResourcesToAccount(self, *, session: SessionRecord, account_aid: str) -> None:
        # Witnesses and watchers are allocated for the onboarding session first,
        # then become durable account resources when account creation succeeds.
        for record in self.getResources("witness", session.witness_eids):
            record.principal = account_aid
            record.cid = account_aid
            self.saveResource(record)

        if session.watcher_eid:
            watcher = self.getResource("watcher", session.watcher_eid)
            if watcher is not None:
                watcher.principal = account_aid
                watcher.cid = account_aid
                self.saveResource(watcher)

    def sessionPayload(self, session: SessionRecord) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "ephemeral_aid": session.ephemeral_aid,
            "account_aid": session.account_aid,
            "account_alias": session.account_alias,
            "account_tier": session.account_tier,
            "state": session.state,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "expires_at": session.expires_at,
            "chosen_profile_code": session.chosen_profile_code,
            "witness_eids": list(session.witness_eids),
            "watcher_eid": session.watcher_eid,
            "watcher_required": session.watcher_required,
            "region_id": session.region_id,
            "region_name": session.region_name,
            "witness_count": session.witness_count,
            "toad": session.toad,
            "failure_reason": session.failure_reason,
            "expired_at": session.expired_at,
            "resources_cleaned_at": session.resources_cleaned_at,
        }

    def accountPayload(self, account: AccountRecord) -> dict[str, Any]:
        return {
            "account_aid": account.account_aid,
            "account_alias": account.account_alias,
            "tier": account.tier,
            "status": account.status,
            "created_at": account.created_at,
            "onboarded_at": account.onboarded_at,
            "expires_at": account.expires_at,
            "witness_profile_code": account.witness_profile_code,
            "witness_count": account.witness_count,
            "toad": account.toad,
            "watcher_required": account.watcher_required,
            "region_id": account.region_id,
            "region_name": account.region_name,
            "session_id": account.session_id,
            "witness_eids": list(account.witness_eids),
            "watcher_eid": account.watcher_eid,
            "expired_at": account.expired_at,
            "resources_cleaned_at": account.resources_cleaned_at,
        }

    def buildAccount(
        self,
        *,
        account_aid: str,
        account_alias: str,
        witness_profile_code: str,
        witness_count: int,
        toad: int,
        watcher_required: bool,
        region_id: str,
        region_name: str,
        session_id: str,
        witness_eids: list[str],
        watcher_eid: str,
        tier: str = "",
        expires_at: str = "",
        onboarded: bool = False,
    ) -> AccountRecord:
        created_at = nowIso()
        account_expires_at = expires_at
        # If an account is onboarded, it should have an expiry date
        if onboarded and not account_expires_at and self.account_ttl_seconds > 0:
            account_expires_at = (
                _parseDt(created_at) + timedelta(seconds=self.account_ttl_seconds)
            ).isoformat()
        return AccountRecord(
            account_aid=account_aid,
            account_alias=account_alias,
            status=ACCOUNT_STATE_ONBOARDED if onboarded else ACCOUNT_STATE_PENDING_ONBOARDING,
            created_at=created_at,
            onboarded_at=created_at if onboarded else "",
            witness_profile_code=witness_profile_code,
            witness_count=witness_count,
            toad=toad,
            watcher_required=watcher_required,
            region_id=region_id,
            region_name=region_name,
            session_id=session_id,
            witness_eids=list(witness_eids),
            watcher_eid=watcher_eid,
            tier=tier,
            expires_at=account_expires_at,
        )


def makeRecord(
    *,
    kind: str,
    eid: str,
    backend_id: str = "",
    cid: str,
    principal: str,
    session_id: str,
    name: str,
    identifier_alias: str,
    region_id: str,
    region_name: str,
    public_url: str,
    boot_url: str,
    oobis: list[str],
    status: str = "",
) -> ResourceRecord:
    public_host, public_port = parsePublicUrl(public_url)
    return ResourceRecord(
        kind=kind,
        eid=eid,
        backend_id=backend_id,
        cid=cid,
        principal=principal,
        session_id=session_id,
        name=name,
        identifier_alias=identifier_alias,
        region_id=region_id,
        region_name=region_name,
        url=public_url,
        boot_url=boot_url,
        public_host=public_host,
        public_port=public_port,
        oobis=list(oobis),
        status=status,
        created_at=nowIso(),
    )


def resourcesToApi(
    records: Iterable[ResourceRecord],
    *,
    include_boot_url: bool = False,
) -> list[dict[str, Any]]:
    return [_resourceToApi(record, include_boot_url=include_boot_url) for record in records]


def sessionFailed(session: SessionRecord, reason: str) -> SessionRecord:
    session.state = "failed"
    session.updated_at = nowIso()
    session.failure_reason = reason
    return session


def accountFailed(account: AccountRecord | None) -> AccountRecord | None:
    if account is None:
        return None
    account.status = ACCOUNT_STATE_FAILED
    return account


def _parseDt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _dueIndexKey(value: str) -> str:
    """Convert an ISO timestamp into a lexicographically sortable UTC key."""
    return _parseDt(value).astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _newSessionId() -> str:
    return f"sess_{secrets.token_urlsafe(12)}"
