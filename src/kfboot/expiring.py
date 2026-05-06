# expiring.py

from __future__ import annotations

from datetime import datetime

from keri import help
from kfboot.boot_client import BootError
from kfboot.basing import (
    ACCOUNT_STATE_ONBOARDED,
    ACCOUNT_STATE_EXPIRED,
    SessionRecord,
)
from kfboot.store import nowIso, accountFailed, sessionFailed

logger = help.ogler.getLogger(__name__)


class Expirer:
    def __init__(self, ctx, provisioner):
        self.ctx = ctx
        self.provisioner = provisioner

    def expireSessions(self) -> None:
        for session in self.ctx.store.expire_sessions():
            logger.info(
                f"Session expired for session {session.session_id}"
            )
            account = self.ctx.store.getAccount(session.account_aid) if session.account_aid else None
            try:
                self.provisioner.teardownSessionResources(session=session, account=account)
            except BootError as exc:
                session.failure_reason = (
                    f"{session.failure_reason} Cleanup failed: {exc}".strip()
                    if session.failure_reason
                    else f"Cleanup failed after expiry: {exc}"
                )
                self.ctx.store.saveSession(session)
                logger.warning(
                    f"Session resource teardown failed during expiry for session {session.session_id}: "
                    f"{exc}"
                )
            else:
                failed = accountFailed(account)
                if failed is not None:
                    self.ctx.store.saveAccount(failed)
                    logger.info(
                        f"Account failed due to session expiry for account {account.account_aid}"
                    )

    def expireAccounts(self) -> None:
        """ Expire accounts that have passed their expiration time, if configured. """
        # Get the current time
        now = datetime.fromisoformat(nowIso())

        # Iterate through onboarded accounts 
        for account in self.ctx.store.listAccounts():

            # Only consider accounts that are onboarded and have an expiration time set
            if account.status != ACCOUNT_STATE_ONBOARDED or not account.expires_at:
                continue
            try:
                # Format the expiration time and compare to current time
                expires_at = datetime.fromisoformat(account.expires_at)
            except ValueError:
                logger.warning(
                    f"Account {account.account_aid} has invalid expires_at format: {account.expires_at}",
                )
                continue
            if expires_at <= now:
                account.status = ACCOUNT_STATE_EXPIRED
                self.ctx.store.saveAccount(account)
                logger.info(
                    f"Account expired at {account.expires_at} for account AID {account.account_aid}",
                )
                # Begin account resources teardown for exprired account
                try:
                    self.provisioner.teardownAccountResources(account_aid=account.account_aid, account=account)
                except BootError as exc:
                    logger.warning(
                        f"Resource teardown failed for expired account {account.account_aid}: {exc}"
                    )

    def failSession(
        self,
        *,
        session: SessionRecord,
        reason: str,
        account=None,
        teardown: bool = False,
    ) -> None:
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
            self.provisioner._teardownFailedSessionResources(
                session=session,
                failure_reason=reason,
                account=failed,
            )

    def refreshSessionLease(self, session: SessionRecord) -> None:
        self.ctx.store.refreshSessionLease(session)
        logger.debug(f"Session lease refreshed for session {session.session_id}")
