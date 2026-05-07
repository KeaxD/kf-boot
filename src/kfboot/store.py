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
    def __init__(self, path: str, *, session_ttl_seconds: int = 300):
        self.baser = open_baser(path)
        self.session_ttl_seconds = session_ttl_seconds

    def close(self) -> None:
        self.baser.close()

    def getQuota(self, scope: str, subject: str) -> QuotaRecord | None:
        return self.baser.quotas.get(keys=(scope, subject))

    def saveQuota(self, record: QuotaRecord) -> None:
        self.baser.quotas.pin(keys=(record.scope, record.subject), val=record)

    def deleteQuota(self, scope: str, subject: str) -> None:
        self.baser.quotas.rem(keys=(scope, subject))

    def expireSessions(self, *, now: str | None = None) -> list[SessionRecord]:
        current = _parseDt(now or nowIso())
        expired: list[SessionRecord] = []
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.state in TERMINAL_SESSION_STATES:
                continue
            if _parseDt(record.expires_at) <= current:
                record.state = SESSION_STATE_EXPIRED
                record.updated_at = current.isoformat()
                self.saveSession(record)
                expired.append(record)
        return expired

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
        self.baser.sessions.pin(keys=(record.session_id,), val=record)

    def refreshSessionLease(self, record: SessionRecord, *, now: str | None = None) -> None:
        current = _parseDt(now or nowIso())
        record.updated_at = current.isoformat()
        if record.state not in TERMINAL_SESSION_STATES:
            record.expires_at = (
                current + timedelta(seconds=self.session_ttl_seconds)
            ).isoformat()
        self.saveSession(record)

    def getSession(self, session_id: str) -> SessionRecord | None:
        return self.baser.sessions.get(keys=(session_id,))

    def findActiveSessionForEphemeral(self, ephemeral_aid: str) -> SessionRecord | None:
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
        self.baser.accounts.pin(keys=(record.account_aid,), val=record)

    def getAccount(self, account_aid: str) -> AccountRecord | None:
        return self.baser.accounts.get(keys=(account_aid,))

    def deleteAccount(self, account_aid: str) -> None:
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
            if record.state in TERMINAL_SESSION_STATES:
                continue
            if record.client_ip != client_ip:
                continue
            rows.append(record)
        rows.sort(key=lambda record: _sortValue(record.created_at), reverse=True)
        return rows

    def listActiveSessionsForAlias(self, account_alias: str) -> list[SessionRecord]:
        """Return a list of active SessionRecords matching the given account alias"""
        rows = []
        for _, record in self.baser.sessions.getTopItemIter(keys=()):
            if record.state in TERMINAL_SESSION_STATES:
                continue
            if record.account_alias != account_alias:
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
            expires_at=expires_at,
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


def _newSessionId() -> str:
    return f"sess_{secrets.token_urlsafe(12)}"
