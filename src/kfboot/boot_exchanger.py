from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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

logger = help.ogler.getLogger(__name__)

ONBOARDING_ROUTES = {
    "/onboarding/session/start",
    "/onboarding/session/status",
    "/onboarding/account/create",
    "/onboarding/complete",
    "/onboarding/cancel",
}

ACCOUNT_ROUTES = {
    "/account/witnesses",
    "/account/watchers",
    "/account/watchers/status",
    "/account/delete",
    "/account/witnesses/delete",
    "/account/watchers/delete",
}


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
        option = self.exchanger.account_option(payload.get("chosen_profile_code", ""))
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
            session = self.exchanger._reconcile_existing_start_session(
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
            self.exchanger.enforce_session_start_admission(
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
                "Session created for account {account_aid}",
            )

        self.exchanger.provision_session_resources(session=session)
        self.exchanger.refresh_session_lease(session)
        self.exchanger.reply_session(self.resource, recipient=sender, session=session)


class SessionStatusHandler(RouteHandler):
    resource = "/onboarding/session/status"

    def handle(self, serder, **kwa):
        sender = serder.pre
        session = self.exchanger.require_session(_required_str(_payload(serder), "session_id"))
        self.exchanger.require_onboarding_principal(sender=sender, session=session)
        self.exchanger.refresh_session_lease(session)
        logger.info(
            f"Session status requested for session {session.session_id}"
            f" from sender {sender}"
        )
        self.exchanger.reply_session(self.resource, recipient=sender, session=session)


class AccountCreateHandler(RouteHandler):
    resource = "/onboarding/account/create"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        session = self.exchanger.require_session(_required_str(payload, "session_id"))
        self.exchanger.require_open_session(session)
        self.exchanger.require_ephemeral_principal(sender=sender, session=session)

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
            self.exchanger.fail_session(
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
            self.exchanger.reply_account(self.resource, recipient=sender, session=session)
            return

        try:
            account_record_created = account is None
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
            self.exchanger.refresh_session_lease(session)
            logger.info(
                f"Account {account.status} for account AID {account_aid}",
            )
        except Exception as exc:
            logger.exception(
                f"Account creation failed for account AID {account_aid}",
            )
            self.exchanger.fail_session(
                session=session,
                reason=str(exc),
                account=account,
                teardown=True,
            )
            raise

        self.exchanger.reply_account(self.resource, recipient=sender, session=session)


class CompleteHandler(RouteHandler):
    resource = "/onboarding/complete"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        session = self.exchanger.require_session(_required_str(payload, "session_id"))
        self.exchanger.require_open_session(session, allow_completed=True)
        self.exchanger.require_ephemeral_principal(sender=sender, session=session)

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
            self.exchanger.reply_account(self.resource, recipient=sender, session=session)
            return

        if session.watcher_required and not session.watcher_eid:
            logger.error(
                f"Onboarding rejected due to missing hosted watcher for account AID {account_aid}"
            )
            self.exchanger.fail_session(
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
            "Onboarding completed for account AID {account_aid}"
        )
        self.exchanger.reply_account(self.resource, recipient=sender, session=session)


class CancelHandler(RouteHandler):
    resource = "/onboarding/cancel"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        session = self.exchanger.require_session(_required_str(payload, "session_id"))
        self.exchanger.require_ephemeral_principal(sender=sender, session=session)
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
                self.exchanger.teardown_session_resources(session=session, account=account)
            except BootError as exc:
                self.exchanger.fail_session(
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

        self.exchanger.reply_session(self.resource, recipient=sender, session=session)


class AccountWitnessesHandler(RouteHandler):
    resource = "/account/witnesses"

    def handle(self, serder, **kwa):
        sender = serder.pre
        self.exchanger.require_onboarded_account(sender, _payload(serder))
        rows = resources_to_api(
            self.exchanger.ctx.store.list_resources_for_account(kind="witness", account_aid=sender)
        )
        logger.info(
            f"Query response for witnesses for account AID {sender}: {rows}"
        )
        self.exchanger.queue_reply(self.resource, sender, {"account_aid": sender, "witnesses": rows})


class AccountWatchersHandler(RouteHandler):
    resource = "/account/watchers"

    def handle(self, serder, **kwa):
        sender = serder.pre
        self.exchanger.require_onboarded_account(sender, _payload(serder))
        rows = resources_to_api(
            self.exchanger.ctx.store.list_resources_for_account(kind="watcher", account_aid=sender)
        )
        logger.info(
            f"Query response for watchers for account AID {sender}: {rows}"
        )
        self.exchanger.queue_reply(self.resource, sender, {"account_aid": sender, "watchers": rows})


class AccountWatcherStatusHandler(RouteHandler):
    resource = "/account/watchers/status"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        self.exchanger.require_onboarded_account(sender, payload)

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

        derived_status = _watcher_status_label(status)
        if derived_status:
            record.status = derived_status
            self.exchanger.ctx.store.save_resource(record)
            logger.info(
                f"Watcher status updated for watcher {watcher_id} to {derived_status} from {sender}"
            )

        watcher = resources_to_api([record])[0]
        if isinstance(status, dict):
            watcher.update(status)
        self.exchanger.queue_reply(
            self.resource,
            sender,
            {"account_aid": sender, "watcher": watcher, "watcher_id": watcher_id},
        )


class AccountWitnessDeleteHandler(RouteHandler):
    resource = "/account/witnesses/delete"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        account = self.exchanger.require_onboarded_account(sender, payload)
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
            self.exchanger._delete_hosted_resource(
                kind="witness",
                eid=witness_id,
                account=account,
            )
        except BootError as exc:
            logger.warning(
                f"Account witness delete failed for witness {witness_id} from {sender}: {exc}"
            )
            raise _boot_error(exc)

        self.exchanger.queue_reply(
            self.resource,
            sender,
            {"account_aid": sender, "witness_id": witness_id, "deleted": True},
        )


class AccountWatcherDeleteHandler(RouteHandler):
    resource = "/account/watchers/delete"

    def handle(self, serder, **kwa):
        sender = serder.pre
        payload = _payload(serder)
        account = self.exchanger.require_onboarded_account(sender, payload)
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
            self.exchanger._delete_hosted_resource(
                kind="watcher",
                eid=watcher_id,
                account=account,
            )
        except BootError as exc:
            logger.warning(
                f"Account watcher delete failed for watcher {watcher_id} from {sender}: {str(exc)}"
            )
            raise _boot_error(exc)

        self.exchanger.queue_reply(
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
            self.exchanger.delete_account(account_aid=sender, account=account)
        except BootError as exc:
            logger.warning(
                f"Account delete failed for account AID {account_aid}: {exc}"
            )
            raise _boot_error(exc)

        logger.info(
            f"Account deleted for account AID {sender}"
        )
        self.exchanger.queue_reply(
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
        self._account_request_windows: dict[str, dict[str, Any]] = {}
        self._account_kel_usage: dict[str, int] = {}

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

    def clear_replies(self) -> None:
        self.reply_streams.clear()
        self.last_error = None

    def set_client_ip(self, client_ip: str) -> None:
        self.client_ip = client_ip

    def expire_sessions(self) -> None:
        for session in self.ctx.store.expire_sessions():
            logger.info(
                f"Session expired for session {session.session_id}"
            )
            account = self.ctx.store.get_account(session.account_aid) if session.account_aid else None
            try:
                self.teardown_session_resources(session=session, account=account)
            except BootError as exc:
                session.failure_reason = (
                    f"{session.failure_reason} Cleanup failed: {exc}".strip()
                    if session.failure_reason
                    else f"Cleanup failed after expiry: {exc}"
                )
                self.ctx.store.save_session(session)
                logger.warning(
                    f"Session resource teardown failed during expiry for session {session.session_id}: "
                    f"{exc}"
                )
            else:
                failed = account_failed(account)
                if failed is not None:
                    self.ctx.store.save_account(failed)
                    logger.info(
                        f"Account failed due to session expiry for account {account.account_aid}"
                    )

    def expire_accounts(self) -> None:
        """ Expire accounts that have passed their expiration time, if configured. """
        # Get the current time
        now = datetime.now(UTC)

        # Iterate through onboarded accounts 
        for account in self.ctx.store.list_accounts():

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
                self.ctx.store.save_account(account)
                logger.info(
                    f"Account expired at {account.expires_at} for account AID {account.account_aid}",
                )

    def take_reply(self) -> bytes | None:
        if not self.reply_streams:
            return None
        return self.reply_streams.pop(0)

    def account_option(self, code: str) -> dict[str, Any]:
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

    def require_session(self, session_id: str) -> SessionRecord:
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

    def require_onboarding_principal(self, *, sender: str, session: SessionRecord) -> None:
        if sender in {session.ephemeral_aid, session.account_aid}:
            return
        logger.warning(
            f"Session principal mismatch between sender {sender} and session principals {session.ephemeral_aid}, {session.account_aid}"
        )
        raise falcon.HTTPUnauthorized(
            title="Wrong principal",
            description="The authenticated sender does not match the onboarding session principal.",
        )

    def require_ephemeral_principal(self, *, sender: str, session: SessionRecord) -> None:
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

    def require_account_principal(self, *, sender: str, session: SessionRecord) -> None:
        if sender and sender == session.account_aid:
            return
        logger.warning(
            f"Session account principal mismatch, the authenticated sender does not match the session's account AID"
        )
        raise falcon.HTTPUnauthorized(
            title="Wrong account principal",
            description="The authenticated sender must be the permanent account AID.",
        )

    def require_open_session(self, session: SessionRecord, *, allow_completed: bool = False) -> None:
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

    def require_onboarded_account(self, sender: str, payload: dict[str, Any]) -> AccountRecord:
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
            logger.warning(f"Account {account.account_aid} is not onboarded and cannot accesss approved account routes")
            raise falcon.HTTPConflict(
                title="Account not onboarded",
                description="Approved-account routes require an onboarded account principal.",
            )
        return account

    def fail_session(
        self,
        *,
        session: SessionRecord,
        reason: str,
        account=None,
        teardown: bool = False,
    ) -> None:
        session_failed(session, reason)
        self.ctx.store.save_session(session)
        logger.warning(
            f"Session {session.session_id} failed: {reason}",
        )

        failed = account_failed(account)
        if failed is not None:
            self.ctx.store.save_account(failed)
            logger.warning(
                f"Account failed due to session failure for account AID {failed.account_aid}",
            )

        if teardown:
            logger.info(
                f"Session resource teardown initiated due to session failure for session {session.session_id}"
            )
            self._teardown_failed_session_resources(
                session=session,
                failure_reason=reason,
                account=failed,
            )

    def refresh_session_lease(self, session: SessionRecord) -> None:
        self.ctx.store.refresh_session_lease(session)
        logger.debug(f"Session lease refreshed for session {session.session_id}")

    def enforce_session_start_admission(
        self,
        *,
        sender: str,
        account_aid: str,
        account_alias: str,
        profile: Any,
    ) -> None:
        client_ip = (self.client_ip or "").strip()
        if not client_ip:
            return

        active = self.ctx.store.list_active_sessions_for_ip(client_ip)
        active_accounts = {record.account_aid for record in active if record.account_aid}
        active_ephemerals = {record.ephemeral_aid for record in active if record.ephemeral_aid}

        account_limit = self.ctx.config.bootstrap_accounts_per_ip
        if account_limit > 0 and account_aid and account_aid not in active_accounts and len(active_accounts) >= account_limit:
            logger.warning(
                f"Account creation rejected due to per-IP onboarding account limit exceeded for client IP {client_ip}."
                f" Current limit is {account_limit} active onboarding accounts, and there are currently {len(active_accounts)} active accounts"
            )
            raise falcon.HTTPTooManyRequests(
                title="Per-IP onboarding account limit exceeded",
                description=(
                    f"Client IP {client_ip} already has {len(active_accounts)} active onboarding "
                    f"account session(s); the configured limit is {account_limit}."
                ),
            )

        aid_limit = self.ctx.config.bootstrap_aids_per_ip
        if aid_limit > 0 and sender and sender not in active_ephemerals and len(active_ephemerals) >= aid_limit:
            logger.warning(
                f"AID creation rejected due to per-IP onboarding principal limit exceeded for client IP {client_ip}."
                f" Current limit is {aid_limit} active onboarding principals, and there are currently {len(active_ephemerals)} active ephemeral AIDs"
            )
            raise falcon.HTTPTooManyRequests(
                title="Per-IP onboarding principal limit exceeded",
                description=(
                    f"Client IP {client_ip} already has {len(active_ephemerals)} active onboarding "
                    f"principal(s); the configured limit is {aid_limit}."
                ),
            )

        # Check account alias limits when provided
        if profile is not None and account_alias:
            alias_accounts = self.ctx.store.list_accounts_for_alias(account_alias)
            # Count accounts that are pending onboarding or already onboarded
            pending_and_onboarded = [
                record
                for record in alias_accounts
                if record.status in {ACCOUNT_STATE_PENDING_ONBOARDING, ACCOUNT_STATE_ONBOARDED}
            ]
            # Add any active sessions for the alias
            active_alias_sessions = self.ctx.store.list_active_sessions_for_alias(account_alias)
            active_alias_session_count = len(
                [
                    session
                    for session in active_alias_sessions
                    if session.account_aid not in {record.account_aid for record in alias_accounts}
                ]
            )
            # The total alias usage is the sum of pending/onboarded accounts and active sessions
            # this prevents a user from avoiding alias limits by starting multiple sessions with
            # the same alias before fully onboarding an account that would enforce the alias limit
            alias_usage = len(pending_and_onboarded) + active_alias_session_count

            # Enforce the max accounts per alias limit
            if profile.max_accounts > 0 and alias_usage >= profile.max_accounts:
                logger.warning(
                    f"Account creation rejected due to account alias usage limit exceeded for client IP {client_ip}"
                    f" and account alias '{account_alias}'. Current limit is {profile.max_accounts} accounts per alias"
                    f" for tier '{profile.tier}', and there are currently {alias_usage} pending/onboarded accounts and active sessions under this alias"
                )
                raise falcon.HTTPTooManyRequests(
                    title="Account alias limit exceeded",
                    description=(
                        f"The account alias '{account_alias}' already has {alias_usage} account(s) in use; "
                        f"the configured limit for tier '{profile.tier}' is {profile.max_accounts}."
                    ),
                )

    def provision_session_resources(self, *, session: SessionRecord) -> None:
        if session.state in TERMINAL_SESSION_STATES:
            if session.state == SESSION_STATE_FAILED:
                logger.warning(
                    f"Session in failed state during resource provisioning"
                )
                raise falcon.HTTPConflict(
                    title="Session failed",
                    description=session.failure_reason or "The onboarding session is in a failed state.",
                )
            logger.info(
                f"Session in terminal state {session.state} during resource provisioning"
            )
            return

        try:
            missing_witnesses = max(session.witness_count - len(session.witness_eids), 0)
            if missing_witnesses:
                logger.info(
                    f"Witness(es) requested for session {session.session_id}"
                    f" with {missing_witnesses} missing witness(es)"
                )
                self._ensure_capacity(kind="witness", requested=missing_witnesses)
                planned_backends = self._planned_witness_backends(session=session)
                start_index = len(session.witness_eids)
                for index, backend in enumerate(planned_backends[start_index:], start=start_index):
                    logger.info(
                        f"Witness allocation start for session {session.session_id}"
                    )
                    created = self._witness_client(backend.id).allocate_witness(session.account_aid)
                    record = make_record(
                        kind="witness",
                        eid=str(created.get("eid", "")),
                        backend_id=backend.id,
                        cid="",
                        principal="",
                        session_id=session.session_id,
                        name=str(created.get("name", "") or f"witness-{index + 1}"),
                        identifier_alias=session.account_alias,
                        region_id=session.region_id,
                        region_name=session.region_name,
                        public_url=backend.public_url,
                        boot_url=backend.boot_url,
                        oobis=list(created.get("oobis", []) or []),
                        status=str(created.get("status", "") or "allocated"),
                    )
                    self.ctx.store.add_resource(record)
                    session.witness_eids.append(record.eid)
                    session.updated_at = now_iso()
                    self.ctx.store.save_session(session)
                    logger.info(
                        f"Witness allocated for session {session.session_id}: witness {record.eid}"
                    )

                session.state = SESSION_STATE_WITNESS_POOL_ALLOCATED
                session.updated_at = now_iso()
                self.ctx.store.save_session(session)
                logger.info(
                    f"Witness pool allocated for session {session.session_id}: witness EIDs {session.witness_eids}"
                )

            if session.watcher_required and not session.watcher_eid:
                logger.info(
                    f"Watcher requested for session {session.session_id}"
                )
                self._ensure_capacity(kind="watcher", requested=1)
                first_witness = self.ctx.store.get_resource("witness", session.witness_eids[0])
                oobi = first_witness.oobis[0] if first_witness and first_witness.oobis else None
                logger.info(
                    f"Watcher allocation start for session {session.session_id}"
                )
                created = self.ctx.watcher_boot.allocate_watcher(session.account_aid, oobi=oobi)
                record = make_record(
                    kind="watcher",
                    eid=str(created.get("eid", "")),
                    cid="",
                    principal="",
                    session_id=session.session_id,
                    name=str(created.get("name", "") or "watcher"),
                    identifier_alias=session.account_alias,
                    region_id=session.region_id,
                    region_name=session.region_name,
                    public_url=self.ctx.config.wat_public_url,
                    boot_url=self.ctx.watcher_boot.base_url,
                    oobis=list(created.get("oobis", []) or []),
                    status=str(created.get("status", "") or "created"),
                )
                self.ctx.store.add_resource(record)
                session.watcher_eid = record.eid
                session.updated_at = now_iso()
                self.ctx.store.save_session(session)
                logger.info(
                    f"Watcher allocated for session {session.session_id}: watcher {record.eid}"
                )
        except BootError as exc:
            logger.warning(
                f"Boot API error during session resource provisioning: {exc}"
            )
            self.fail_session(session=session, reason=str(exc), teardown=True)
            raise _boot_error(exc)
        except Exception as exc:
            logger.exception(
                f"Unexpected error during session resource provisioning: {exc}"
            )
            self.fail_session(session=session, reason=str(exc), teardown=True)
            raise

    def reply_session(self, route: str, *, recipient: str, session: SessionRecord) -> None:
        payload = self.session_payload(session)
        self.queue_reply(route, recipient, payload)

    def reply_account(self, route: str, *, recipient: str, session: SessionRecord) -> None:
        payload = self.session_payload(session)
        if session.account_aid:
            account = self.ctx.store.get_account(session.account_aid)
            if account is not None:
                payload["account"] = self.ctx.store.account_payload(account)
        self.queue_reply(route, recipient, payload)

    def session_payload(self, session: SessionRecord) -> dict[str, Any]:
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

    def queue_reply(self, route: str, recipient: str, payload: dict[str, Any]) -> None:
        stream = bytearray(self.host_hab.replay())
        stream.extend(self.host_hab.exchange(route=route, payload=payload, recipient=recipient or None))
        self.reply_streams.append(bytes(stream))
        logger.debug(
            "Reply queued for route {route} to recipient {recipient}",
        )

    def processEvent(self, serder, tsgs=None, cigars=None, ptds=None, essrs=None, **kwa):
        try:
            # First enforce account quotas before processing the event
            self._enforce_account_quotas(serder)
            return super().processEvent(serder, tsgs=tsgs, cigars=cigars, ptds=ptds, essrs=essrs, **kwa)
        except falcon.HTTPError as exc:
            self.last_error = exc
            logger.warning(
                f"Exchange event processing failed with HTTP error: {exc}"
            )
            return None

    def _enforce_account_quotas(self, serder) -> None:
        """Apply account quota enforcement for onboarding and account-side requests."""

        # Apply quotas only to onboarding and account routes
        route = str(serder.ked.get("r", "") or "")
        if route not in ONBOARDING_ROUTES and route not in ACCOUNT_ROUTES:
            return

        # Check account context for the request
        payload = _payload(serder)
        account_aid, profile = self._account_context_for_route(serder, payload)
        # TODO - we may want to enforce some limits even without account context
        if not account_aid or profile is None:
            logger.debug(
                "No account context found for route {route}"
            )
            return

        # Enforce request rate and KEL budget for the account
        self._enforce_account_request_rate(account_aid, profile)
        self._enforce_account_kel_budget(account_aid, profile)

    def _account_context_for_route(self, serder, payload: dict[str, Any]) -> tuple[str, Any]:
        """Resolve the account AID and tier profile for the current request."""
        route = str(serder.ked.get("r", "") or "")
        sender = serder.pre

        # For onboarding start, return the account AID and profile based on session context
        if route == "/onboarding/session/start":
            account_aid = _optional_str(payload, "account_aid")
            profile = self.ctx.config.account_profile(payload.get("chosen_profile_code", ""))
            return account_aid, profile

        # For other onboarding routes, resolve the account AID and profile from the session context
        session_id = _optional_str(payload, "session_id")
        if session_id:
            session = self.ctx.store.get_session(session_id)
            if session is not None:
                profile = self.ctx.config.account_profile(session.chosen_profile_code)
                account_aid = session.account_aid or _optional_str(payload, "account_aid")
                return account_aid, profile

        # For account routes, resolve the account AID and profile based on the authenticated sender
        if route in ACCOUNT_ROUTES:
            # Account routes are authenticated by the account AID sender.
            account_aid = sender
            account = self.ctx.store.get_account(account_aid)
            profile = self.ctx.config.account_profile(account.witness_profile_code) if account is not None else None
            return account_aid, profile

        return "", None

    def _enforce_account_request_rate(self, account_aid: str, profile: Any) -> None:
        """Enforce per-account request limits on onboarding and account routes."""
        
        # Get the current time and the request window for this account
        now = datetime.now(UTC)
        window = self._account_request_windows.setdefault(
            account_aid,
            {"start": now, "count": 0},
        )

        # Reset the window if more than 60 seconds have elapsed since the start
        elapsed = (now - window["start"]).total_seconds()
        if elapsed >= 60:
            window["start"] = now
            window["count"] = 0

        # Check if the request count exceeds the profile limit for the window
        if window["count"] >= profile.max_requests_per_minute > 0:
            logger.warning(
                f"Account request per minute rate limit exceeded."
                f" User is limited to {profile.max_requests_per_minute} requests per minute under tier '{profile.tier}'"
            )
            raise falcon.HTTPTooManyRequests(
                title="Account request rate limit exceeded",
                description=(
                    f"Account {account_aid} exceeded {profile.max_requests_per_minute} requests in the rolling minute window. "
                    "Retry later or request a higher staging tier."
                ),
            )

        # If not exceeded, increment the count and log if approaching soft limits
        window["count"] += 1
        ratio = window["count"] / max(profile.max_requests_per_minute, 1)
        if ratio >= 0.95:
            logger.warning("high_request_rate")
        elif ratio >= 0.85:
            logger.info("approaching_request_rate_limit")

    def _enforce_account_kel_budget(self, account_aid: str, profile: Any) -> None:
        """Enforce a fixed per-account KEL event quota on onboarding and account routes."""

        if profile.kel_budget <= 0:
            return

        count = self._account_kel_usage.get(account_aid, 0)
        if count >= profile.kel_budget:
            logger.warning(
                f"Account KEL budget exceeded for account {account_aid} under tier '{profile.tier}'",
            )
            raise falcon.HTTPTooManyRequests(
                title="Account key event budget exceeded",
                description=(
                    f"Account {account_aid} exceeded {profile.kel_budget} key events. "
                    "Request quota has been exhausted for this account tier."
                ),
            )

        count += 1
        self._account_kel_usage[account_aid] = count
        ratio = count / max(profile.kel_budget, 1)
        if ratio >= 0.95:
            logger.warning("high_kel_usage")
        elif ratio >= 0.85:
            logger.info("approaching_kel_budget")

    def _ensure_capacity(self, *, kind: str, requested: int) -> None:
        if requested <= 0:
            return
        count = self.ctx.store.count_resources(kind)
        limit = (
            self.ctx.config.witness_limit
            if kind == "witness"
            else self.ctx.config.watcher_limit
        )
        
        # Log warnings if the projected resource count approaches or exceeds the limit,
        # but still allow the request to proceed until the hard limit is reached
        projected = count + requested
        ratio = projected / max(limit, 1)
        if ratio >= 0.95:
            logger.warning(
                f"Projected {kind} usage is at 95% of capacity limit",
            )
        elif ratio >= 0.85:
            logger.info(
                f"Projected {kind} usage is at 85% of capacity limit",
            )
        elif ratio >= 0.7:
            logger.info(
                f"Projected {kind} usage is at 70% of capacity limit",
            )
        if projected > limit:
            logger.error(
                f"Capacity for {kind} exceeded: cannot provision {requested} as it would exceed the limit of {limit}"
                f" with current count at {count}"
            )
            raise falcon.HTTPConflict(
                title="Capacity exceeded",
                description=(
                    f"{kind} limit is {limit}, current count is {count}, "
                    f"requested {requested} additional"
                ),
            )

    def _planned_witness_backends(self, *, session: SessionRecord) -> list[Any]:
        if session.witness_backend_ids:
            if len(session.witness_backend_ids) != session.witness_count:
                logger.error(
                    "Session witness backend selection does not match witness count",
                )
                raise BootError(
                    "Session witness backend selection does not match the configured witness count.",
                    status_code=503,
                )
            logger.debug(
                "Witness backends already selected for session",
            )
            return [self._witness_backend(backend_id) for backend_id in session.witness_backend_ids]

        backends = self._select_witness_backends(
            count=session.witness_count,
            seed=session.session_id,
        )
        session.witness_backend_ids = [backend.id for backend in backends]
        session.updated_at = now_iso()
        self.ctx.store.save_session(session)
        logger.info(
            "Witness backends selected for session {session.session_id}"
        )
        return backends

    def _select_witness_backends(self, *, count: int, seed: str) -> list[Any]:
        ordered = sorted(self.ctx.config.witness_backends, key=lambda backend: backend.id)
        if count > len(ordered):
            logger.error(
                f"Witness backend selection failed due to insufficient backends: {count} requested but only {len(ordered)} available"
            )
            raise BootError(
                f"Witness profile requires {count} backends but only {len(ordered)} are configured.",
                status_code=503,
            )
        if count <= 0:
            return []

        digest = blake2b(seed.encode("utf-8"), digest_size=8).digest()
        start = int.from_bytes(digest, "big") % len(ordered)
        return [ordered[(start + index) % len(ordered)] for index in range(count)]

    def _witness_backend(self, backend_id: str) -> Any:
        for backend in self.ctx.config.witness_backends:
            if backend.id == backend_id:
                return backend
        raise BootError(f"Witness backend '{backend_id}' is not configured.", status_code=503)

    def _witness_client(self, backend_id: str) -> Any:
        client = self.ctx.witness_boots.get(backend_id)
        if client is None:
            raise BootError(f"Witness backend '{backend_id}' is not configured.", status_code=503)
        return client

    def _witness_client_for_record(self, record) -> Any:
        if record.backend_id:
            return self._witness_client(record.backend_id)

        if record.boot_url:
            for backend in self.ctx.config.witness_backends:
                if backend.boot_url == record.boot_url:
                    return self._witness_client(backend.id)

        public_url = (record.url or "").rstrip("/")
        if public_url:
            matches = [
                backend for backend in self.ctx.config.witness_backends if backend.public_url == public_url
            ]
            if len(matches) == 1:
                return self._witness_client(matches[0].id)

        if record.public_host:
            matches = [
                backend
                for backend in self.ctx.config.witness_backends
                if parse_public_url(backend.public_url) == (record.public_host, record.public_port)
            ]
            if len(matches) == 1:
                return self._witness_client(matches[0].id)

        if len(self.ctx.witness_boots) == 1:
            return next(iter(self.ctx.witness_boots.values()))

        raise BootError(
            f"No witness backend matches stored routing data for witness '{record.eid}'.",
            status_code=503,
        )

    def teardown_session_resources(self, *, session: SessionRecord, account=None) -> None:
        errors: list[BootError] = []
        watcher_ids = self._collect_session_resource_ids(kind="watcher", session=session, account=account)
        witness_ids = self._collect_session_resource_ids(kind="witness", session=session, account=account)
        logger.info(
            f"Session resource teardown started for session {session.session_id}"
        )

        for watcher_id in watcher_ids:
            try:
                self._delete_hosted_resource(
                    kind="watcher",
                    eid=watcher_id,
                    session=session,
                    account=account,
                    tolerate_missing_remote=True,
                )
            except BootError as exc:
                errors.append(exc)
                logger.warning(
                    f"Failed to delete watcher {watcher_id} during teardown for session {session.session_id}"
                )

        for witness_id in witness_ids:
            try:
                self._delete_hosted_resource(
                    kind="witness",
                    eid=witness_id,
                    session=session,
                    account=account,
                    tolerate_missing_remote=True,
                )
            except BootError as exc:
                errors.append(exc)
                logger.warning(
                    f"Failed to delete witness {witness_id} during teardown of session {session.session_id}"
                )

        if errors:
            first = errors[0]
            detail = "; ".join(str(error) for error in errors)
            logger.warning(
                f"Session resource teardown completed with errors ({len(errors)})for session {session.session_id}: {detail}"
            )
            raise BootError(detail, status_code=first.status_code)
        logger.info(
            f"Session resources teardown completed for session {session.session_id}"
        )

    def delete_account(self, *, account_aid: str, account=None) -> None:
        sessions = self.ctx.store.list_sessions_for_account(account_aid)
        errors: list[BootError] = []
        watcher_ids = self._collect_account_resource_ids(
            kind="watcher",
            account_aid=account_aid,
            account=account,
            sessions=sessions,
        )
        witness_ids = self._collect_account_resource_ids(
            kind="witness",
            account_aid=account_aid,
            account=account,
            sessions=sessions,
        )
        logger.info(
            f"Account deletion started for account AID {account_aid}"
        )

        for watcher_id in watcher_ids:
            try:
                self._delete_hosted_resource(
                    kind="watcher",
                    eid=watcher_id,
                    account=account,
                    tolerate_missing_remote=True,
                )
            except BootError as exc:
                errors.append(exc)
                logger.warning(
                    f"Account deletion of watcher resource failed for watcher {watcher_id}: {exc}"
                )

        for witness_id in witness_ids:
            try:
                self._delete_hosted_resource(
                    kind="witness",
                    eid=witness_id,
                    account=account,
                    tolerate_missing_remote=True,
                )
            except BootError as exc:
                errors.append(exc)
                logger.warning(
                    f"Account deletion of witness resource failed for witness {witness_id}: {exc}"
                )

        if errors:
            first = errors[0]
            detail = "; ".join(str(error) for error in errors)
            logger.warning(
                f"Account deletion completed with errors ({len(errors)}) for account AID {account_aid}: {detail}"
            )
            raise BootError(detail, status_code=first.status_code)

        self.ctx.store.delete_bindings_for_principal(account_aid)
        self.ctx.store.delete_account(account_aid)
        for session in sessions:
            self.ctx.store.delete_session(session.session_id)
        logger.info(
            f"Account deletion completed for account AID {account_aid}"
            f" with {len(sessions)} sessions, {len(watcher_ids)} watchers, and {len(witness_ids)} witnesses deleted"
        )

    def _collect_account_resource_ids(
        self,
        *,
        kind: str,
        account_aid: str,
        account=None,
        sessions: list[SessionRecord] | None = None,
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        sessions = sessions or []

        candidates: list[str] = []
        if kind == "witness":
            if account is not None:
                candidates.extend(account.witness_eids)
            for session in sessions:
                candidates.extend(session.witness_eids)
        else:
            if account is not None and account.watcher_eid:
                candidates.append(account.watcher_eid)
            for session in sessions:
                if session.watcher_eid:
                    candidates.append(session.watcher_eid)

        candidates.extend(
            record.eid
            for record in self.ctx.store.list_resources_for_account(
                kind=kind,
                account_aid=account_aid,
            )
        )
        for session in sessions:
            candidates.extend(
                record.eid
                for record in self.ctx.store.list_resources_for_session(
                    kind=kind,
                    session_id=session.session_id,
                )
            )

        for eid in candidates:
            if not eid or eid in seen:
                continue
            seen.add(eid)
            ordered.append(eid)

        return ordered

    def _collect_session_resource_ids(self, *, kind: str, session: SessionRecord, account=None) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        candidates: list[str] = []
        if kind == "witness":
            candidates.extend(session.witness_eids)
            if account is not None:
                candidates.extend(account.witness_eids)
        else:
            if session.watcher_eid:
                candidates.append(session.watcher_eid)
            if account is not None and account.watcher_eid:
                candidates.append(account.watcher_eid)

        candidates.extend(
            record.eid
            for record in self.ctx.store.list_resources_for_session(
                kind=kind,
                session_id=session.session_id,
            )
        )

        for eid in candidates:
            if not eid or eid in seen:
                continue
            seen.add(eid)
            ordered.append(eid)

        return ordered

    def _delete_hosted_resource(
        self,
        *,
        kind: str,
        eid: str,
        session: SessionRecord | None = None,
        account=None,
        tolerate_missing_remote: bool = False,
    ) -> None:
        if not eid:
            return

        record = self.ctx.store.get_resource(kind, eid)
        if record is None:
            logger.info(
                f"Resource record not found for deletion for {kind} with EID {eid}"
            )
            if session is not None:
                if kind == "witness":
                    session.witness_eids = [item for item in session.witness_eids if item != eid]
                elif session.watcher_eid == eid:
                    session.watcher_eid = ""
            if account is not None:
                if kind == "witness":
                    account.witness_eids = [item for item in account.witness_eids if item != eid]
                elif account.watcher_eid == eid:
                    account.watcher_eid = ""
            self._persist_owner_state(session=session, account=account)
            return

        if kind == "witness":
            delete_remote = self._witness_client_for_record(record).delete_witness
        else:
            delete_remote = self.ctx.watcher_boot.delete_watcher
        logger.info(
            f"Resource deletion started for {kind} with EID {eid}",
        )
        try:
            delete_remote(eid)
        except BootError as exc:
            if not (tolerate_missing_remote and exc.status_code == 404):
                logger.warning(
                    f"Resource deletion failed for {kind} with EID {eid}: {exc}",
                )
                raise
            logger.info(
                f"Resource not found during deletion for {kind} with EID {eid}, but tolerated: {exc}",
            )

        self.ctx.store.delete_resource(kind, eid)
        if session is not None:
            if kind == "witness":
                session.witness_eids = [item for item in session.witness_eids if item != eid]
            elif session.watcher_eid == eid:
                session.watcher_eid = ""

        if account is not None:
            if kind == "witness":
                account.witness_eids = [item for item in account.witness_eids if item != eid]
            elif account.watcher_eid == eid:
                account.watcher_eid = ""

        self._persist_owner_state(session=session, account=account)
        logger.info(
            f"Resource deletion completed for {kind} with EID {eid}"
        )

    def _persist_owner_state(self, *, session: SessionRecord | None = None, account=None) -> None:
        if session is not None:
            session.updated_at = now_iso()
            self.ctx.store.save_session(session)
        if account is not None:
            self.ctx.store.save_account(account)

    def _teardown_failed_session_resources(
        self,
        *,
        session: SessionRecord,
        failure_reason: str,
        account=None,
    ) -> None:
        try:
            self.teardown_session_resources(session=session, account=account)
        except BootError as exc:
            session.failure_reason = f"{failure_reason} Cleanup failed: {exc}"
            session.updated_at = now_iso()
            self.ctx.store.save_session(session)
            logger.warning(
                f"Session resource teardown failed for {session.session_id}: {exc}",
            )

    def _reconcile_existing_start_session(
        self,
        *,
        session: SessionRecord,
        account_aid: str,
        account_alias: str,
        option: dict[str, Any],
        region_id: str,
        watcher_required: bool,
    ) -> SessionRecord:
        if session.state == SESSION_STATE_FAILED:
            logger.warning(f"Session start rejected: {session.failure_reason} {session.session_id}")
            raise falcon.HTTPConflict(
                title="Session failed",
                description=session.failure_reason or "Blind retry would duplicate hosted resources.",
            )
        if session.state in {SESSION_STATE_CANCELLED, SESSION_STATE_EXPIRED}:
            logger.warning(f"Session {session.session_id} was closed because no longer active")
            raise falcon.HTTPConflict(
                title="Session closed",
                description="The onboarding session is no longer active.",
            )
        if session.account_aid and session.account_aid != account_aid:
            logger.warning(
                f"Account AID mismatch for session {session.session_id}: has account AID {session.account_aid}"
                f" but request specified account AID {account_aid}",
            )
            raise falcon.HTTPConflict(
                title="Session parameter mismatch",
                description="The existing onboarding session was started with a different permanent account AID.",
            )
        if account_alias and session.account_alias and session.account_alias != account_alias:
            logger.warning(
                f"Account alias mismatch for session {session.session_id}: account alias {session.account_alias}"
                f" but request specified account alias {account_alias}"
            )
            raise falcon.HTTPConflict(
                title="Session parameter mismatch",
                description="The existing onboarding session was started with a different account alias.",
            )
        if session.chosen_profile_code and session.chosen_profile_code != option["code"]:
            logger.warning(
                f"Witness profile mismatch for session {session.session_id}: witness profile code {session.chosen_profile_code}"
                f" but request specified witness profile code {option['code']}",
            )
            raise falcon.HTTPConflict(
                title="Session parameter mismatch",
                description="The existing onboarding session uses a different witness profile.",
            )
        if session.region_id and session.region_id != region_id:
            logger.warning(
                f"Region mismatch for session {session.session_id}: session region {session.region_id} but request specified region {region_id}",
            )
            raise falcon.HTTPConflict(
                title="Session parameter mismatch",
                description="The existing onboarding session uses a different region.",
            )
        if session.watcher_required != watcher_required:
            logger.warning(
                f"Watcher requirement mismatch for session {session.session_id}: session watcher requirement {session.watcher_required}"
                f" but request specified watcher requirement {watcher_required}"
            )
            raise falcon.HTTPConflict(
                title="Session parameter mismatch",
                description="The existing onboarding session uses a different watcher requirement.",
            )
        return session


def _payload(serder) -> dict[str, Any]:
    payload = serder.ked.get("a", {})
    return payload if isinstance(payload, dict) else {}


def _optional_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = _optional_str(payload, key)
    if value:
        return value
    raise falcon.HTTPBadRequest(
        title="Invalid request payload",
        description=f"{key} is required.",
    )


def _watcher_status_label(status: Any) -> str:
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


def _boot_error(exc: BootError) -> falcon.HTTPError:
    if exc.status_code == 400:
        return falcon.HTTPBadRequest(
            title="Boot API rejected request",
            description=str(exc),
        )
    if exc.status_code == 404:
        return falcon.HTTPNotFound(
            title="Upstream resource not found",
            description=str(exc),
        )
    if exc.status_code == 409:
        return falcon.HTTPConflict(
            title="Boot API conflict",
            description=str(exc),
        )
    return falcon.HTTPBadGateway(
        title="Boot API call failed",
        description=str(exc),
    )
