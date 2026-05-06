from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import blake2b
from typing import Any

import falcon
from keri import help
from keri.peer.exchanging import Exchanger

from kfboot.basing import (
    ACCOUNT_STATE_ONBOARDED,
    ACCOUNT_STATE_PAUSED,
    ACCOUNT_STATE_PENDING_ONBOARDING,
    ACCOUNT_STATE_EXPIRED,
    AccountRecord,
    SESSION_STATE_ACCOUNT_CREATED,
    SESSION_STATE_CANCELLED,
    SESSION_STATE_COMPLETED,
    SESSION_STATE_EXPIRED,
    SESSION_STATE_FAILED,
    SESSION_STATE_WITNESS_POOL_ALLOCATED,
    TERMINAL_SESSION_STATES,
    SessionRecord,
)
from kfboot.boot_client import BootError
from kfboot.store import (
    account_failed,
    make_record,
    now_iso,
    parse_public_url,
    resources_to_api,
    session_failed,
)
from kfboot.limiting import Limiter
from kfboot.admitting import Admitter
from kfboot.provisioning import Provisioner
from kfboot.expiring import Expirer
from kfboot.utils import _payload, _optional_str, _required_str, _boot_error

logger = help.ogler.getLogger(__name__)


@dataclass
class BootContext:
    config: Any
    store: Any
    witness_boots: Any
    watcher_boot: Any
    host_hab: Any
    habery: Any


class RouteHandler:
    resource: str = ""

    def __init__(self, exchanger: "BootExchanger"):
        self.exchanger = exchanger

    def verify(self, serder, **kwa) -> bool:
        return True

    def handle(self, serder, **kwa):
        raise NotImplementedError


class SessionStartHandler(RouteHandler):
    resource = "/onboarding/session/start"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        option = self.exchanger.accountOption(payload.get("chosen_profile_code", ""))
        account_aid = _required_str(payload, "account_aid")
        alias = _optional_str(payload, "account_alias")
        region_id = _optional_str(payload, "region_id") or self.exchanger.ctx.config.region_id
        watcher_required = bool(
            payload.get("watcher_required", self.exchanger.ctx.config.bootstrap_watcher_required)
        )
        profile = self.exchanger.ctx.config.account_profile(option["code"])
        logger.info(
            f"Session start requested \n"
            f"Sender AID: {sender} \n"
            f"Account AID: {account_aid} \n"
            f"Account Alias: {alias} \n"
            f"Profile code: {option['code']}\n"
            f"Account tier: {getattr(profile, 'tier', '')}\n"
            f"Client IP: {self.exchanger.client_ip}\n"
        )
        if profile is None:
            logger.error(
                f"Profile not found for session start request for account AID {account_aid} from sender {sender}"
            )
            raise falcon.HTTPInternalServerError(
                title="Account profile missing",
                description="The selected witness profile has no configured account profile.",
            )
        if self.exchanger.ctx.config.bootstrap_watcher_required and not watcher_required:
            logger.warning(
                f"Session start rejected due to missing required watcher for account AID {account_aid}"
                f" from sender {sender}"
            )
            raise falcon.HTTPBadRequest(
                title="Watcher required",
                description="This boot service requires one hosted watcher per onboarded account.",
            )

        account = self.exchanger.ctx.store.get_account(account_aid)
        if account is not None and account.status == ACCOUNT_STATE_ONBOARDED:
            logger.warning(
                f"Session start rejected because account {account_aid} is already onboarded"
            )
            raise falcon.HTTPConflict(
                title="Account already onboarded",
                description="The permanent account AID already completed onboarding.",
            )

        session = None
        session_for_account = self.exchanger.ctx.store.find_session_for_account(account_aid)
        if session_for_account is not None and session_for_account.state not in TERMINAL_SESSION_STATES:
            if session_for_account.ephemeral_aid != sender:
                logger.warning(
                    f"Session start rejected due to active session with different onboarding principal"
                    f" for account AID {account_aid} from sender {sender}"
                )
                raise falcon.HTTPConflict(
                    title="Account session already active",
                    description="A different onboarding principal already owns the active session for this account AID.",
                )
            session = session_for_account
        elif (existing := self.exchanger.ctx.store.find_active_session_for_ephemeral(sender)) is not None:
            session = existing

        if session is not None:
            session = self.exchanger.admitter.reconcileExistingStartSession(
                session=session,
                account_aid=account_aid,
                account_alias=alias,
                option=option,
                region_id=region_id,
                watcher_required=watcher_required,
            )
            logger.info(
                f"Session start reconciled with existing session for account AID {account_aid}"
                f" from sender {sender}"
            )
        else:
            self.exchanger.admitter.enforceSessionStartAdmission(
                sender=sender,
                account_aid=account_aid,
                account_alias=alias,
                profile=self.exchanger.ctx.config.account_profile(option["code"]),
            )
            session = self.exchanger.ctx.store.create_session(
                ephemeral_aid=sender,
                account_aid=account_aid,
                account_alias=alias,
                chosen_profile_code=option["code"],
                client_ip=self.exchanger.client_ip,
                region_id=region_id,
                region_name=self.exchanger.ctx.config.region_name,
                watcher_required=watcher_required,
                witness_count=option["witness_count"],
                toad=option["toad"],
                account_tier=profile.tier,
            )
            logger.info(
                f"Session created for account {account_aid}",
            )

        try:
            self.exchanger.provisioner.provisionSessionResources(session=session)
        except BootError as exc:
            # mark session failed, attempt teardown, then map to HTTP error
            self.exchanger.expirer.failSession(session=session, reason=str(exc), teardown=True)
            raise _boot_error(exc)
        except Exception as exc:
            # unexpected error: mark session failed and re-raise
            self.exchanger.expirer.failSession(session=session, reason=str(exc), teardown=True)
            raise
        self.exchanger.expirer.refreshSessionLease(session)
        self.exchanger.replySession(self.resource, recipient=sender, session=session)


class SessionStatusHandler(RouteHandler):
    resource = "/onboarding/session/status"

    def handle(self, serder, **kwa):
        sender = serder.pre
        session = self.exchanger.requireSession(_required_str(_payload(serder), "session_id"))
        self.exchanger.requireOnboardingPrincipal(sender=sender, session=session)
        self.exchanger.expirer.refreshSessionLease(session)
        logger.info(
            f"Session status requested for session {session.session_id}"
            f" from sender {sender}"
        )
        self.exchanger.replySession(self.resource, recipient=sender, session=session)


class AccountCreateHandler(RouteHandler):
    resource = "/onboarding/account/create"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        session = self.exchanger.requireSession(_required_str(payload, "session_id"))
        self.exchanger.requireOpenSession(session)
        self.exchanger.requireEphemeralPrincipal(sender=sender, session=session)

        account_aid = _required_str(payload, "account_aid")
        logger.info(
            f"Account creation requested for account {account_aid} in session {session.session_id}",
        )

        if session.account_aid and session.account_aid != account_aid:
            logger.warning(
                f"Account creation rejected due to session already bound to account AID {session.account_aid}"
            )
            raise falcon.HTTPConflict(
                title="Session already bound",
                description="This onboarding session is already bound to a different account AID.",
            )

        if not session.witness_eids or (session.watcher_required and not session.watcher_eid):
            logger.error(
                f"Hosted resources missing during account creation for session {session.session_id}"
            )
            self.exchanger.expirer.failSession(
                session=session,
                reason="Hosted resources were not fully allocated before account creation.",
                teardown=True,
            )
            raise falcon.HTTPConflict(
                title="Resources missing",
                description="Witness or watcher allocation is incomplete for this session.",
            )

        account = self.exchanger.ctx.store.get_account(account_aid)
        
        # Check account state before attempting to create or update account records
        if account is not None and account.status in {ACCOUNT_STATE_PAUSED, ACCOUNT_STATE_EXPIRED}:
            logger.warning(
                f"Account creation rejected due to account {account_aid} being paused or expired",
            )
            raise falcon.HTTPConflict(
                title="Account not available",
                description="The permanent account AID is currently paused or expired and cannot be reused for onboarding.",
            )
        if account is not None and account.session_id not in {"", session.session_id}:
            logger.warning(
                f"Account creation rejected due to account {account_aid} already bound to different session {account.session_id}"
            )
            raise falcon.HTTPConflict(
                title="Account already exists",
                description="The permanent account AID is already bound to a different onboarding session.",
            )

        if session.state in {SESSION_STATE_ACCOUNT_CREATED, SESSION_STATE_COMPLETED} and session.account_aid == account_aid:
            logger.info(
                f"Account creation request reconciled with existing account"
                f" for account {account_aid} in session {session.session_id}",
            )
            self.exchanger.replyAccount(self.resource, recipient=sender, session=session)
            return

        try:
            if account is None:
                account = self.exchanger.ctx.store.build_account(
                    account_aid=account_aid,
                    account_alias=_optional_str(payload, "account_alias") or session.account_alias,
                    witness_profile_code=session.chosen_profile_code,
                    witness_count=session.witness_count,
                    toad=session.toad,
                    watcher_required=session.watcher_required,
                    region_id=session.region_id,
                    region_name=session.region_name,
                    session_id=session.session_id,
                    witness_eids=list(session.witness_eids),
                    watcher_eid=session.watcher_eid,
                    tier=session.account_tier,
                    onboarded=False,
                )
            else:
                account.account_alias = _optional_str(payload, "account_alias") or account.account_alias
                account.status = ACCOUNT_STATE_PENDING_ONBOARDING
                account.witness_eids = list(session.witness_eids)
                account.watcher_eid = session.watcher_eid
                account.session_id = session.session_id

            session.account_aid = account_aid
            session.state = SESSION_STATE_ACCOUNT_CREATED
            session.updated_at = now_iso()

            self.exchanger.ctx.store.save_account(account)
            self.exchanger.ctx.store.bind_resources_to_account(session=session, account_aid=account_aid)
            self.exchanger.expirer.refreshSessionLease(session)
            logger.info(
                f"Account {account.status} for account AID {account_aid}",
            )
        except Exception as exc:
            logger.exception(
                f"Account creation failed for account AID {account_aid}",
            )
            self.exchanger.expirer.failSession(
                session=session,
                reason=str(exc),
                account=account,
                teardown=True,
            )
            raise

        self.exchanger.replyAccount(self.resource, recipient=sender, session=session)


class CompleteHandler(RouteHandler):
    resource = "/onboarding/complete"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        session = self.exchanger.requireSession(_required_str(payload, "session_id"))
        self.exchanger.requireOpenSession(session, allow_completed=True)
        self.exchanger.requireEphemeralPrincipal(sender=sender, session=session)

        account_aid = _required_str(payload, "account_aid")
        logger.info(
            f"Onboarding completed for account AID {account_aid}",
        )
        if session.account_aid and session.account_aid != account_aid:
            logger.warning(
                f"Onboarding rejected due to session already bound to a different account AID",
            )
            raise falcon.HTTPConflict(
                title="Session already bound",
                description="This onboarding session is already bound to a different account AID.",
            )

        account = self.exchanger.ctx.store.get_account(account_aid)
        if account is None:
            logger.warning(
                f"Onboarding rejected due to account record not found for account AID {account_aid}"
            )
            raise falcon.HTTPNotFound(
                title="Account not found",
                description="No account record exists for the requested permanent account AID.",
            )

        if session.state == SESSION_STATE_COMPLETED and account.status == ACCOUNT_STATE_ONBOARDED:
            logger.info(
                f"Onboarding request reconciled with existing completed session and onboarded account {account_aid}"
            )
            self.exchanger.replyAccount(self.resource, recipient=sender, session=session)
            return

        if session.watcher_required and not session.watcher_eid:
            logger.error(
                f"Onboarding rejected due to missing hosted watcher for account AID {account_aid}"
            )
            self.exchanger.expirer.failSession(
                session=session,
                reason="Hosted watcher is required before onboarding can complete.",
                account=account,
                teardown=True,
            )
            raise falcon.HTTPConflict(
                title="Watcher missing",
                description="This boot service requires one hosted watcher before onboarding completes.",
            )

        session.state = SESSION_STATE_COMPLETED
        session.updated_at = now_iso()
        account.status = ACCOUNT_STATE_ONBOARDED
        account.onboarded_at = now_iso()
        self.exchanger.ctx.store.save_session(session)
        self.exchanger.ctx.store.save_account(account)
        logger.info(
            f"Onboarding completed for account AID {account_aid}"
        )
        self.exchanger.replyAccount(self.resource, recipient=sender, session=session)


class CancelHandler(RouteHandler):
    resource = "/onboarding/cancel"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        session = self.exchanger.requireSession(_required_str(payload, "session_id"))
        self.exchanger.requireEphemeralPrincipal(sender=sender, session=session)
        logger.info(
            f"Session cancellation requested for session {session.session_id}"
        )
        if session.state == SESSION_STATE_COMPLETED:
            logger.warning(
                f"Session cancellation rejected because session is already completed"
            )
            raise falcon.HTTPConflict(
                title="Session completed",
                description="A completed onboarding session cannot be cancelled.",
            )
        if session.state != SESSION_STATE_CANCELLED:
            account = self.exchanger.ctx.store.get_account(session.account_aid) if session.account_aid else None
            try:
                self.exchanger.provisioner.teardownSessionResources(session=session, account=account)
            except BootError as exc:
                self.exchanger.expirer.failSession(
                    session=session,
                    reason=f"Hosted resource teardown failed during cancellation: {exc}",
                    account=account,
                )
                logger.warning(
                    f"Session cancellation failed during resource teardown for session {session.session_id}"
                )
                raise _boot_error(exc)

            session.state = SESSION_STATE_CANCELLED
            session.updated_at = now_iso()
            self.exchanger.ctx.store.save_session(session)
            logger.info(
                f"Session cancelled for session {session.session_id}"
            )

            failed = account_failed(account)
            if failed is not None:
                self.exchanger.ctx.store.save_account(failed)
                logger.info(
                    f"Account failed due to session cancellation for account AID {account.account_aid}"
                )

        self.exchanger.replySession(self.resource, recipient=sender, session=session)


class AccountWitnessesHandler(RouteHandler):
    resource = "/account/witnesses"

    def handle(self, serder, **kwa):
        sender = serder.pre
        self.exchanger.requireOnboardedAccount(sender, _payload(serder))
        rows = resources_to_api(
            self.exchanger.ctx.store.list_resources_for_account(kind="witness", account_aid=sender)
        )
        logger.info(
            f"Query response for witnesses for account AID {sender}: {rows}"
        )
        self.exchanger.queueReply(self.resource, sender, {"account_aid": sender, "witnesses": rows})


class AccountWatchersHandler(RouteHandler):
    resource = "/account/watchers"

    def handle(self, serder, **kwa):
        sender = serder.pre
        self.exchanger.requireOnboardedAccount(sender, _payload(serder))
        rows = resources_to_api(
            self.exchanger.ctx.store.list_resources_for_account(kind="watcher", account_aid=sender)
        )
        logger.info(
            f"Query response for watchers for account AID {sender}: {rows}"
        )
        self.exchanger.queueReply(self.resource, sender, {"account_aid": sender, "watchers": rows})


class AccountWatcherStatusHandler(RouteHandler):
    resource = "/account/watchers/status"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        self.exchanger.requireOnboardedAccount(sender, payload)

        watcher_id = _optional_str(payload, "watcher_eid") or _required_str(payload, "watcher_id")
        logger.info(
            f"Query status for watcher {watcher_id} from {sender}"
        )
        record = self.exchanger.ctx.store.get_resource("watcher", watcher_id)
        if record is None or record.principal != sender:
            logger.warning(
                f"Query for watcher status failed because watcher {watcher_id} was not found"
            )
            raise falcon.HTTPNotFound(title="Watcher not found")

        try:
            status = self.exchanger.ctx.watcher_boot.watcher_status(watcher_id)
        except BootError as exc:
            logger.warning(
                f"Query for watcher status failed for watcher {watcher_id} due to boot API error: {exc}"
            )
            raise _boot_error(exc)

        derived_status = _watcherStatusLabel(status)
        if derived_status:
            record.status = derived_status
            self.exchanger.ctx.store.save_resource(record)
            logger.info(
                f"Watcher status updated for watcher {watcher_id} to {derived_status} from {sender}"
            )

        watcher = resources_to_api([record])[0]
        if isinstance(status, dict):
            watcher.update(status)
        self.exchanger.queueReply(
            self.resource,
            sender,
            {"account_aid": sender, "watcher": watcher, "watcher_id": watcher_id},
        )


class AccountWitnessDeleteHandler(RouteHandler):
    resource = "/account/witnesses/delete"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        account = self.exchanger.requireOnboardedAccount(sender, payload)
        witness_id = _optional_str(payload, "witness_eid") or _required_str(payload, "witness_id")
        logger.info(
            f"Account witness delete requested for witness {witness_id} from {sender}"
        )
        record = self.exchanger.ctx.store.get_resource("witness", witness_id)
        if record is None or record.principal != sender:
            logger.warning(
                f"Account witness delete request failed because witness {witness_id} was not found"
            )
            raise falcon.HTTPNotFound(title="Witness not found")

        try:
            self.exchanger.provisioner.deleteHostedResource(
                kind="witness",
                eid=witness_id,
                account=account,
            )
        except BootError as exc:
            logger.warning(
                f"Account witness delete failed for witness {witness_id} from {sender}: {exc}"
            )
            raise _boot_error(exc)

        self.exchanger.queueReply(
            self.resource,
            sender,
            {"account_aid": sender, "witness_id": witness_id, "deleted": True},
        )


class AccountWatcherDeleteHandler(RouteHandler):
    resource = "/account/watchers/delete"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        account = self.exchanger.requireOnboardedAccount(sender, payload)
        watcher_id = _optional_str(payload, "watcher_eid") or _required_str(payload, "watcher_id")
        logger.info(
            f"Account watcher delete requested for watcher {watcher_id} from {sender}"
        )
        record = self.exchanger.ctx.store.get_resource("watcher", watcher_id)
        if record is None or record.principal != sender:
            logger.warning(
                f"Account watcher delete request failed because watcher {watcher_id} was not found"
            )
            raise falcon.HTTPNotFound(title="Watcher not found")

        try:
            self.exchanger.provisioner.deleteHostedResource(
                kind="watcher",
                eid=watcher_id,
                account=account,
            )
        except BootError as exc:
            logger.warning(
                f"Account watcher delete failed for watcher {watcher_id} from {sender}: {str(exc)}"
            )
            raise _boot_error(exc)

        self.exchanger.queueReply(
            self.resource,
            sender,
            {"account_aid": sender, "watcher_id": watcher_id, "deleted": True},
        )


class AccountDeleteHandler(RouteHandler):
    resource = "/account/delete"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        account_aid = _optional_str(payload, "account_aid") or sender
        logger.info(
            f"Account delete requested for account AID {account_aid}"
        )
        if account_aid != sender:
            logger.warning(
                f"Account delete request rejected, authenticated sender {sender} does not match requested account AID {account_aid}"
            )
            raise falcon.HTTPUnauthorized(
                title="Account principal mismatch",
                description="The authenticated sender must match account_aid.",
            )

        account = self.exchanger.ctx.store.get_account(sender)
        
        # Check account state
        if account is not None and account.status not in {
            ACCOUNT_STATE_ONBOARDED,
            ACCOUNT_STATE_PAUSED,
            ACCOUNT_STATE_EXPIRED,
        }:
            logger.warning(
                f"Account delete request rejected due to account {account_aid} being in invalid state: {account.status}"
            )
            raise falcon.HTTPConflict(
                title="Account not onboarded",
                description="Approved-account routes require an onboarded account principal.",
            )

        try:
            self.exchanger.provisioner.deleteAccount(account_aid=sender, account=account)
        except BootError as exc:
            logger.warning(
                f"Account delete failed for account AID {account_aid}: {exc}"
            )
            raise _boot_error(exc)

        logger.info(
            f"Account deleted for account AID {sender}"
        )
        self.exchanger.queueReply(
            self.resource,
            sender,
            {"account_aid": sender, "deleted": True},
        )


class BootExchanger(Exchanger):
    def __init__(self, ctx: BootContext):
        super().__init__(hby=ctx.habery, handlers=[])
        self.ctx = ctx
        self.host_hab = ctx.host_hab
        self.reply_streams: list[bytes] = []
        self.client_ip = ""
        self.last_error: falcon.HTTPError | None = None
        self.limiter = Limiter(ctx)
        self.admitter = Admitter(ctx, self)
        self.provisioner = Provisioner(ctx, self)
        self.expirer = Expirer(ctx, self.provisioner)

        handlers = (
            SessionStartHandler(self),
            SessionStatusHandler(self),
            AccountCreateHandler(self),
            CompleteHandler(self),
            CancelHandler(self),
            AccountWitnessesHandler(self),
            AccountWatchersHandler(self),
            AccountWatcherStatusHandler(self),
            AccountDeleteHandler(self),
            AccountWitnessDeleteHandler(self),
            AccountWatcherDeleteHandler(self),
        )
        for handler in handlers:
            self.addHandler(handler)
        logger.info(
            f"Exchanger initialized \n"
            f"Handlers: {[type(h).__name__ for h in handlers]}\n"
            f"Witness Count: {len(ctx.witness_boots)}\n"
            f"Watcher Boot URL: {getattr(ctx.watcher_boot, "base_url", "")}",
        )

    def clearReplies(self) -> None:
        self.reply_streams.clear()
        self.last_error = None

    def setClientIp(self, client_ip: str) -> None:
        self.client_ip = client_ip

    

    def takeReply(self) -> bytes | None:
        if not self.reply_streams:
            return None
        return self.reply_streams.pop(0)

    def accountOption(self, code: str) -> dict[str, Any]:
        option = self.ctx.config.account_option(code or "")
        if option is None:
            logger.warning(
                f"Account option is unsupported for code '{code or ''}'"
            )
            raise falcon.HTTPBadRequest(
                title="Unsupported witness profile",
                description=f"Unknown account profile '{code or ''}'.",
            )
        return option

    def requireSession(self, session_id: str) -> SessionRecord:
        session = self.ctx.store.get_session(session_id)
        if session is None:
            logger.warning(
                f"Session not found for session ID {session_id}"
            )
            raise falcon.HTTPNotFound(
                title="Session not found",
                description=f"No onboarding session exists for '{session_id}'.",
            )
        return session

    def requireOnboardingPrincipal(self, *, sender: str, session: SessionRecord) -> None:
        if sender in {session.ephemeral_aid, session.account_aid}:
            return
        logger.warning(
            f"Session principal mismatch between sender {sender} and session principals {session.ephemeral_aid}, {session.account_aid}"
        )
        raise falcon.HTTPUnauthorized(
            title="Wrong principal",
            description="The authenticated sender does not match the onboarding session principal.",
        )

    def requireEphemeralPrincipal(self, *, sender: str, session: SessionRecord) -> None:
        if sender and sender == session.ephemeral_aid:
            return
        logger.warning(
            f"Session ephemeral principal mismatch for session {session.session_id}"
            f" between sender {sender} and session ephemeral principal {session.ephemeral_aid}"
        )
        raise falcon.HTTPUnauthorized(
            title="Wrong onboarding principal",
            description="The authenticated sender must be the session's hidden onboarding AID.",
        )

    def requireAccountPrincipal(self, *, sender: str, session: SessionRecord) -> None:
        if sender and sender == session.account_aid:
            return
        logger.warning(
            f"Session account principal mismatch, the authenticated sender does not match the session's account AID"
        )
        raise falcon.HTTPUnauthorized(
            title="Wrong account principal",
            description="The authenticated sender must be the permanent account AID.",
        )

    def requireOpenSession(self, session: SessionRecord, *, allow_completed: bool = False) -> None:
        if session.state == SESSION_STATE_EXPIRED:
            logger.warning(f"Session {session.session_id} expired")
            raise falcon.HTTPGone(title="Session expired")
        if session.state == SESSION_STATE_FAILED:
            logger.warning(f"Session {session.session_id} failed")
            raise falcon.HTTPConflict(
                title="Session failed",
                description=session.failure_reason or "The onboarding session is in a failed state.",
            )
        if session.state == SESSION_STATE_CANCELLED:
            logger.warning(f"Session {session.session_id} cancelled")
            raise falcon.HTTPConflict(title="Session cancelled")
        if session.state == SESSION_STATE_COMPLETED and not allow_completed:
            logger.warning(f"Session {session.session_id} completed")
            raise falcon.HTTPConflict(title="Session completed")

    def requireOnboardedAccount(self, sender: str, payload: dict[str, Any]) -> AccountRecord:
        account_aid = _optional_str(payload, "account_aid")
        if account_aid and account_aid != sender:
            logger.warning(
                f"Account principal mismatch, authenticated sender {sender} does not match"
                f" the requested account AID {account_aid}"
            )
            raise falcon.HTTPUnauthorized(
                title="Account principal mismatch",
                description="The authenticated sender must match account_aid.",
            )
        account = self.ctx.store.get_account(sender)
        if account is None:
            logger.warning(
                f"Account not found for authenticated sender {sender}"
            )
            raise falcon.HTTPNotFound(
                title="Account not found",
                description="No account exists for the authenticated sender.",
            )
        if account.status == ACCOUNT_STATE_PAUSED:
            logger.warning(f"Account {account.account_aid} is paused and cannot access approved account routes")
            raise falcon.HTTPConflict(
                title="Account paused",
                description="This account is currently paused and cannot access approved account routes.",
            )
        if account.status == ACCOUNT_STATE_EXPIRED:
            logger.warning(f"Account {account.account_aid} has expired and cannot access account routes")
            raise falcon.HTTPConflict(
                title="Account expired",
                description="This account has expired and must be renewed or deleted before accessing account routes.",
            )
        if account.status != ACCOUNT_STATE_ONBOARDED:
            logger.warning(f"Account {account.account_aid} is not onboarded and cannot access approved account routes")
            raise falcon.HTTPConflict(
                title="Account not onboarded",
                description="Approved-account routes require an onboarded account principal.",
            )
        return account


    def replySession(self, route: str, *, recipient: str, session: SessionRecord) -> None:
        payload = self.sessionPayload(session)
        self.queueReply(route, recipient, payload)

    def replyAccount(self, route: str, *, recipient: str, session: SessionRecord) -> None:
        payload = self.sessionPayload(session)
        if session.account_aid:
            account = self.ctx.store.get_account(session.account_aid)
            if account is not None:
                payload["account"] = self.ctx.store.account_payload(account)
        self.queueReply(route, recipient, payload)

    def sessionPayload(self, session: SessionRecord) -> dict[str, Any]:
        witnesses = resources_to_api(
            self.ctx.store.get_resources("witness", session.witness_eids),
            include_boot_url=True,
        )
        watcher = None
        if session.watcher_eid:
            rows = resources_to_api(
                self.ctx.store.get_resources("watcher", [session.watcher_eid]),
                include_boot_url=True,
            )
            watcher = rows[0] if rows else None

        payload = self.ctx.store.session_payload(session)
        payload["session"] = dict(payload)
        payload["witnesses"] = witnesses
        payload["watcher"] = watcher
        payload["witness_count"] = session.witness_count or len(witnesses)
        payload["toad"] = session.toad
        payload["region_id"] = session.region_id
        payload["region_name"] = session.region_name
        if session.account_aid:
            payload["account_aid"] = session.account_aid
        return payload

    def queueReply(self, route: str, recipient: str, payload: dict[str, Any]) -> None:
        stream = bytearray(self.host_hab.replay())
        stream.extend(self.host_hab.exchange(route=route, payload=payload, recipient=recipient or None))
        self.reply_streams.append(bytes(stream))
        logger.debug(
            f"Reply queued for route {route} to recipient {recipient}",
        )

    def processEvent(self, serder, tsgs=None, cigars=None, ptds=None, essrs=None, **kwa):
        try:
            # First enforce account quotas before processing the event
            self.limiter.enforceAccountQuotas(serder)
            return super().processEvent(serder, tsgs=tsgs, cigars=cigars, ptds=ptds, essrs=essrs, **kwa)
        except falcon.HTTPError as exc:
            self.last_error = exc
            logger.warning(
                f"Exchange event processing failed with HTTP error: {exc}"
            )
            return None


def _watcherStatusLabel(status: Any) -> str:
    if not isinstance(status, dict):
        return ""
    if "status" in status and status.get("status"):
        return str(status.get("status"))

    summary = status.get("summary", {})
    if not isinstance(summary, dict):
        return ""

    total = int(summary.get("total_witnesses", 0) or 0)
    responsive = int(summary.get("responsive_witnesses", 0) or 0)
    if total <= 0:
        return "created"
    if responsive >= total:
        return "connected"
    return "query_pending"