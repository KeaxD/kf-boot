from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable
from urllib.parse import urlsplit

from kfboot.basing import (
    ACCOUNT_STATE_FAILED,
    ACCOUNT_STATE_ONBOARDED,
    ACCOUNT_STATE_PENDING_ONBOARDING,
    SESSION_STATE_EXPIRED,
    TERMINAL_SESSION_STATES,
    AccountRecord,
    BindingRecord,
    ResourceRecord,
    SessionRecord,
    open_baser,
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_public_url(url: str) -> tuple[str, int | None]:
    parts = urlsplit(url)
    return parts.hostname or "", parts.port


def _sort_value(value: Any) -> Any:
    if value is None:
        return ""
    return value


def _resource_value(record: Any, field: str, default: Any = "") -> Any:
    return getattr(record, field, default)


def _resource_to_api(record: ResourceRecord, *, include_boot_url: bool = False) -> dict[str, Any]:
    data = {
        "kind": _resource_value(record, "kind", ""),
        "eid": _resource_value(record, "eid", ""),
        "cid": _resource_value(record, "cid", ""),
        "name": _resource_value(record, "name", ""),
        "identifier_alias": _resource_value(record, "identifier_alias", ""),
        "region_id": _resource_value(record, "region_id", ""),
        "region_name": _resource_value(record, "region_name", ""),
        "url": _resource_value(record, "url", ""),
        "boot_url": _resource_value(record, "boot_url", ""),
        "public_host": _resource_value(record, "public_host", ""),
        "public_port": _resource_value(record, "public_port", None),
        "status": _resource_value(record, "status", ""),
        "created_at": _resource_value(record, "created_at", ""),
    }
    if not include_boot_url:
        data.pop("boot_url", None)
    data["oobis"] = list(_resource_value(record, "oobis", []) or [])
    if _resource_value(record, "kind", "") == "witness":
        data["witness_url"] = _resource_value(record, "url", "")
    elif _resource_value(record, "kind", "") == "watcher":
        data["watcher_url"] = _resource_value(record, "url", "")
    return data


class Store:
    def __init__(self, path: str, *, session_ttl_seconds: int = 300):
        self.baser = open_baser(path)
        self.session_ttl_seconds = session_ttl_seconds

    def close(self) -> None:
        self.baser.close()

    def expire_sessions(self, *, now: str | None = None) -> list[SessionRecord]:
        current = _parse_dt(now or now_iso())
        expired: list[SessionRecord] = []
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.state in TERMINAL_SESSION_STATES:
                continue
            if _parse_dt(record.expires_at) <= current:
                record.state = SESSION_STATE_EXPIRED
                record.updated_at = current.isoformat()
                self.save_session(record)
                expired.append(record)
        return expired

    def create_session(
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
    ) -> SessionRecord:
        created_at = now_iso()
        expires_at = (_parse_dt(created_at) + timedelta(seconds=self.session_ttl_seconds)).isoformat()
        record = SessionRecord(
            session_id=_new_session_id(),
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
        )
        self.save_session(record)
        return record

    def save_session(self, record: SessionRecord) -> None:
        self.baser.sessions.pin(keys=(record.session_id,), val=record)

    def refresh_session_lease(self, record: SessionRecord, *, now: str | None = None) -> None:
        current = _parse_dt(now or now_iso())
        record.updated_at = current.isoformat()
        if record.state not in TERMINAL_SESSION_STATES:
            record.expires_at = (
                current + timedelta(seconds=self.session_ttl_seconds)
            ).isoformat()
        self.save_session(record)

    def get_session(self, session_id: str) -> SessionRecord | None:
        return self.baser.sessions.get(keys=(session_id,))

    def find_active_session_for_ephemeral(self, ephemeral_aid: str) -> SessionRecord | None:
        latest: SessionRecord | None = None
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.ephemeral_aid != ephemeral_aid:
                continue
            if latest is None or record.created_at > latest.created_at:
                latest = record
        return latest

    def find_session_for_account(self, account_aid: str) -> SessionRecord | None:
        latest: SessionRecord | None = None
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.account_aid != account_aid:
                continue
            if latest is None or record.created_at > latest.created_at:
                latest = record
        return latest

    def list_sessions_for_account(self, account_aid: str) -> list[SessionRecord]:
        rows = []
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.account_aid != account_aid:
                continue
            rows.append(record)
        rows.sort(key=lambda record: _sort_value(record.created_at), reverse=True)
        return rows

    def save_account(self, record: AccountRecord) -> None:
        self.baser.accounts.pin(keys=(record.account_aid,), val=record)

    def get_account(self, account_aid: str) -> AccountRecord | None:
        return self.baser.accounts.get(keys=(account_aid,))

    def delete_account(self, account_aid: str) -> None:
        self.baser.accounts.rem(keys=(account_aid,))

    def list_accounts(self) -> list[AccountRecord]:
        return [record for _, record in self.baser.accounts.getTopItemIter(keys=())]

    def list_active_sessions_for_ip(self, client_ip: str) -> list[SessionRecord]:
        rows = []
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.state in TERMINAL_SESSION_STATES:
                continue
            if record.client_ip != client_ip:
                continue
            rows.append(record)
        rows.sort(key=lambda record: _sort_value(record.created_at), reverse=True)
        return rows

    def add_binding(self, principal: str, cid: str) -> None:
        self.baser.bindings.pin(
            keys=(principal, cid),
            val=BindingRecord(principal=principal, cid=cid),
        )

    def delete_bindings_for_principal(self, principal: str) -> None:
        matches = [
            keys
            for keys, _record in self.baser.bindings.getTopItemIter(keys=())
            if keys and keys[0] == principal
        ]
        for keys in matches:
            self.baser.bindings.rem(keys=keys)

    def add_resource(self, record: ResourceRecord) -> None:
        self.baser.resources.pin(keys=(record.kind, record.eid), val=record)

    def save_resource(self, record: ResourceRecord) -> None:
        self.add_resource(record)

    def get_resource(self, kind: str, eid: str) -> ResourceRecord | None:
        return self.baser.resources.get(keys=(kind, eid))

    def get_resources(self, kind: str, eids: Iterable[str]) -> list[ResourceRecord]:
        rows = []
        for eid in eids:
            record = self.get_resource(kind, eid)
            if record is not None:
                rows.append(record)
        return rows

    def delete_resource(self, kind: str, eid: str) -> None:
        self.baser.resources.rem(keys=(kind, eid))

    def delete_session(self, session_id: str) -> None:
        self.baser.sessions.rem(keys=(session_id,))

    def count_resources(self, kind: str) -> int:
        return sum(1 for _, _ in self.baser.resources.getTopItemIter(keys=(kind,), topive=True))

    def list_resources_for_account(self, *, kind: str, account_aid: str) -> list[ResourceRecord]:
        rows = []
        for _, record in self.baser.resources.getTopItemIter(keys=(kind,), topive=True):
            if _resource_value(record, "principal", "") == account_aid:
                rows.append(record)
        rows.sort(key=lambda record: _sort_value(_resource_value(record, "created_at", "")), reverse=True)
        return rows

    def list_resources_for_session(self, *, kind: str, session_id: str) -> list[ResourceRecord]:
        rows = []
        for _, record in self.baser.resources.getTopItemIter(keys=(kind,), topive=True):
            if _resource_value(record, "session_id", "") == session_id:
                rows.append(record)
        rows.sort(key=lambda record: _sort_value(_resource_value(record, "created_at", "")), reverse=True)
        return rows

    def bind_resources_to_account(self, *, session: SessionRecord, account_aid: str) -> None:
        # Witnesses and watchers are allocated for the onboarding session first,
        # then become durable account resources when account creation succeeds.
        for record in self.get_resources("witness", session.witness_eids):
            record.principal = account_aid
            record.cid = account_aid
            self.save_resource(record)

        if session.watcher_eid:
            watcher = self.get_resource("watcher", session.watcher_eid)
            if watcher is not None:
                watcher.principal = account_aid
                watcher.cid = account_aid
                self.save_resource(watcher)

    def session_payload(self, session: SessionRecord) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "ephemeral_aid": session.ephemeral_aid,
            "account_aid": session.account_aid,
            "account_alias": session.account_alias,
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
        }

    def account_payload(self, account: AccountRecord) -> dict[str, Any]:
        return {
            "account_aid": account.account_aid,
            "account_alias": account.account_alias,
            "status": account.status,
            "created_at": account.created_at,
            "onboarded_at": account.onboarded_at,
            "witness_profile_code": account.witness_profile_code,
            "witness_count": account.witness_count,
            "toad": account.toad,
            "watcher_required": account.watcher_required,
            "region_id": account.region_id,
            "region_name": account.region_name,
            "session_id": account.session_id,
            "witness_eids": list(account.witness_eids),
            "watcher_eid": account.watcher_eid,
        }

    def build_account(
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
        onboarded: bool = False,
    ) -> AccountRecord:
        created_at = now_iso()
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
        )


def make_record(
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
    public_host, public_port = parse_public_url(public_url)
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
        created_at=now_iso(),
    )


def resources_to_api(
    records: Iterable[ResourceRecord],
    *,
    include_boot_url: bool = False,
) -> list[dict[str, Any]]:
    return [_resource_to_api(record, include_boot_url=include_boot_url) for record in records]


def session_failed(session: SessionRecord, reason: str) -> SessionRecord:
    session.state = "failed"
    session.updated_at = now_iso()
    session.failure_reason = reason
    return session


def account_failed(account: AccountRecord | None) -> AccountRecord | None:
    if account is None:
        return None
    account.status = ACCOUNT_STATE_FAILED
    return account


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _new_session_id() -> str:
    return f"sess_{secrets.token_urlsafe(12)}"
