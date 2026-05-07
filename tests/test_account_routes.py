from __future__ import annotations

from types import SimpleNamespace
import falcon
import pytest
import kfboot.boot_exchanger as boot_exchanger
from keri.app import habbing

from kfboot.basing import (
    ACCOUNT_STATE_EXPIRED,
    ACCOUNT_STATE_ONBOARDED,
    ACCOUNT_STATE_PAUSED,
    ACCOUNT_STATE_PENDING_ONBOARDING,
)
from kfboot.boot_client import BootError
from kfboot.config import AccountProfile
from kfboot.store import Store
from kfboot.limiting import Limiter

from .support import (
    assert_reply_frame,
    build_exn,
    complete_session,
    create_account,
    post_cesr,
    register_aid,
    start_session,
    total_witness_delete_calls,
    make_config,
)


def test_approved_account_routes_return_resources_update_status_and_delete_records(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]
    watcher_id = onboarded_bundle["watcher_id"]
    witness_record = contract.ctx.store.getResource("witness", witness_id)

    witnesses = post_cesr(
        contract,
        "/account",
        build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
    )
    _, witnesses_reply = assert_reply_frame(contract, witnesses, route="/account/witnesses")
    assert [row["eid"] for row in witnesses_reply.ked["a"]["witnesses"]] == [witness_id]
    assert witnesses_reply.ked["a"]["witnesses"][0]["witness_url"] == witness_record.url
    assert "boot_url" not in witnesses_reply.ked["a"]["witnesses"][0]

    watchers = post_cesr(
        contract,
        "/account",
        build_exn(account, route="/account/watchers", payload={"account_aid": account.pre}),
    )
    _, watchers_reply = assert_reply_frame(contract, watchers, route="/account/watchers")
    assert [row["eid"] for row in watchers_reply.ked["a"]["watchers"]] == [watcher_id]
    assert "boot_url" not in watchers_reply.ked["a"]["watchers"][0]

    watcherStatus = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/status",
            payload={"account_aid": account.pre, "watcher_eid": watcher_id},
        ),
    )
    _, status_reply = assert_reply_frame(contract, watcherStatus, route="/account/watchers/status")
    assert status_reply.ked["a"]["watcher"]["status"] == "connected"
    assert contract.ctx.store.getResource("watcher", watcher_id).status == "connected"
    assert contract.ctx.watcher_boot.status_calls == [watcher_id]

    witness_delete = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_eid": witness_id},
        ),
    )
    _, witness_delete_reply = assert_reply_frame(
        contract,
        witness_delete,
        route="/account/witnesses/delete",
    )
    assert witness_delete_reply.ked["a"]["account_aid"] == account.pre
    assert witness_delete_reply.ked["a"]["witness_id"] == witness_id
    assert witness_delete_reply.ked["a"]["deleted"] is True
    assert contract.ctx.store.getResource("witness", witness_id) is None
    assert contract.ctx.store.getAccount(account.pre).witness_eids == []

    watcher_delete = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/delete",
            payload={"account_aid": account.pre, "watcher_eid": watcher_id},
        ),
    )
    _, watcher_delete_reply = assert_reply_frame(contract, watcher_delete, route="/account/watchers/delete")
    assert watcher_delete_reply.ked["a"]["account_aid"] == account.pre
    assert watcher_delete_reply.ked["a"]["watcher_id"] == watcher_id
    assert watcher_delete_reply.ked["a"]["deleted"] is True
    assert contract.ctx.store.getResource("watcher", watcher_id) is None
    assert contract.ctx.store.getAccount(account.pre).watcher_eid == ""
    assert total_witness_delete_calls(contract.ctx) == [witness_id]
    for backend_id, boot in contract.ctx.witness_boots.items():
        expected = [witness_id] if backend_id == witness_record.backend_id else []
        assert boot.delete_calls == expected
    assert contract.ctx.watcher_boot.delete_calls == [watcher_id]


def test_account_delete_route_removes_account_state_and_is_idempotent(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    session_id = onboarded_bundle["session_id"]
    witness_ids = onboarded_bundle["witness_ids"]
    watcher_id = onboarded_bundle["watcher_id"]
    contract.ctx.store.addBinding(account.pre, "cid-to-delete")

    first = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/delete",
            payload={"account_aid": account.pre},
        ),
    )
    _, first_reply = assert_reply_frame(contract, first, route="/account/delete")
    assert first_reply.ked["a"]["account_aid"] == account.pre
    assert first_reply.ked["a"]["deleted"] is True
    assert contract.ctx.store.baser.bindings.get(keys=(account.pre, "cid-to-delete")) is None
    assert contract.ctx.store.getAccount(account.pre) is None
    assert contract.ctx.store.getSession(session_id) is None
    assert contract.ctx.store.getResource("watcher", watcher_id) is None
    for witness_id in witness_ids:
        assert contract.ctx.store.getResource("witness", witness_id) is None
    assert total_witness_delete_calls(contract.ctx) == witness_ids
    assert contract.ctx.watcher_boot.delete_calls == [watcher_id]

    second = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/delete",
            payload={"account_aid": account.pre},
        ),
    )
    _, second_reply = assert_reply_frame(contract, second, route="/account/delete")
    assert second_reply.ked["a"]["account_aid"] == account.pre
    assert second_reply.ked["a"]["deleted"] is True
    assert contract.ctx.store.getAccount(account.pre) is None
    assert contract.ctx.store.getSession(session_id) is None
    assert total_witness_delete_calls(contract.ctx) == witness_ids
    assert contract.ctx.watcher_boot.delete_calls == [watcher_id]


def test_account_delete_failure_keeps_remaining_state_retryable(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    session_id = onboarded_bundle["session_id"]
    witness_id = onboarded_bundle["witness_ids"][0]
    watcher_id = onboarded_bundle["watcher_id"]
    witness_record = contract.ctx.store.getResource("witness", witness_id)
    witness_boot = contract.ctx.witness_boots[witness_record.backend_id]
    witness_boot.delete_error = BootError("simulated witness delete failure", status_code=503)

    failed = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/delete",
            payload={"account_aid": account.pre},
        ),
    )

    assert failed.status_code == 502
    assert failed.json["title"] == "Boot API call failed"
    account_record = contract.ctx.store.getAccount(account.pre)
    assert account_record is not None
    assert account_record.status == ACCOUNT_STATE_ONBOARDED
    assert account_record.watcher_eid == ""
    assert account_record.witness_eids == [witness_id]
    assert contract.ctx.store.getSession(session_id) is not None
    assert contract.ctx.store.getResource("watcher", watcher_id) is None
    assert contract.ctx.store.getResource("witness", witness_id) is not None
    assert contract.ctx.watcher_boot.delete_calls == [watcher_id]
    assert witness_boot.delete_calls == [witness_id]

    witness_boot.delete_error = None
    retry = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/delete",
            payload={"account_aid": account.pre},
        ),
    )

    _, retry_reply = assert_reply_frame(contract, retry, route="/account/delete")
    assert retry_reply.ked["a"]["deleted"] is True
    assert contract.ctx.store.getAccount(account.pre) is None
    assert contract.ctx.store.getSession(session_id) is None
    assert contract.ctx.store.getResource("witness", witness_id) is None
    assert contract.ctx.watcher_boot.delete_calls == [watcher_id]
    assert witness_boot.delete_calls == [witness_id, witness_id]


@pytest.mark.parametrize(
    ("status_response", "expected_status"),
    [
        ({"summary": {"total_witnesses": 0, "responsive_witnesses": 0}}, "created"),
        ({"summary": {"total_witnesses": 3, "responsive_witnesses": 1}}, "query_pending"),
        (
            {
                "status": "lagging",
                "summary": {"total_witnesses": 3, "responsive_witnesses": 3},
            },
            "lagging",
        ),
    ],
)
def test_account_watcherStatus_derives_non_happy_path_labels(
    onboarded_bundle,
    status_response,
    expected_status,
):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    watcher_id = onboarded_bundle["watcher_id"]

    contract.ctx.watcher_boot.status_response = {
        "controller_id": account.pre,
        **status_response,
    }

    response = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/status",
            payload={"account_aid": account.pre, "watcher_id": watcher_id},
        ),
    )

    _, reply = assert_reply_frame(contract, response, route="/account/watchers/status")
    assert reply.ked["a"]["watcher_id"] == watcher_id
    assert reply.ked["a"]["watcher"]["status"] == expected_status
    assert contract.ctx.store.getResource("watcher", watcher_id).status == expected_status


def test_witness_delete_routes_to_the_persisted_backend_id(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]
    record = contract.ctx.store.getResource("witness", witness_id)
    target_backend = next(
        backend
        for backend in reversed(contract.ctx.config.witness_backends)
        if backend.id != record.backend_id
    )
    record.backend_id = target_backend.id
    record.boot_url = target_backend.boot_url
    record.url = target_backend.public_url
    contract.ctx.store.saveResource(record)

    response = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_eid": witness_id},
        ),
    )

    _, reply = assert_reply_frame(contract, response, route="/account/witnesses/delete")
    assert reply.ked["a"]["witness_id"] == witness_id
    for backend_id, boot in contract.ctx.witness_boots.items():
        expected = [witness_id] if backend_id == target_backend.id else []
        assert boot.delete_calls == expected


def test_witness_delete_routes_legacy_records_by_public_url_when_backend_fields_are_missing(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]
    record = contract.ctx.store.getResource("witness", witness_id)
    expected_backend_id = record.backend_id

    for boot in contract.ctx.witness_boots.values():
        boot.delete_calls.clear()

    record.backend_id = ""
    record.boot_url = ""
    contract.ctx.store.saveResource(record)

    response = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_eid": witness_id},
        ),
    )

    _, reply = assert_reply_frame(contract, response, route="/account/witnesses/delete")
    assert reply.ked["a"]["witness_id"] == witness_id
    for backend_id, boot in contract.ctx.witness_boots.items():
        expected = [witness_id] if backend_id == expected_backend_id else []
        assert boot.delete_calls == expected


def test_account_witnesses_route_tolerates_legacy_resource_rows(onboarded_bundle, monkeypatch):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]
    original_iter = contract.ctx.store.baser.resources.getTopItemIter

    junk_row = SimpleNamespace(
        kind="witness",
        eid="LEGACY_JUNK",
        url="https://legacy-junk.example",
        oobis=["https://legacy-junk.example/oobi/LEGACY_JUNK/controller"],
        status="allocated",
    )
    legacy_row = SimpleNamespace(
        kind="witness",
        eid="LEGACY_MATCH",
        principal=account.pre,
        cid=account.pre,
        name="Legacy Witness",
        identifier_alias="legacy",
        region_id="legacy-region",
        region_name="Legacy Region",
        url="https://legacy.example",
        oobis=["https://legacy.example/oobi/LEGACY_MATCH/controller"],
        status="allocated",
    )

    def fake_iter(*args, **kwargs):
        keys = kwargs.get("keys", args[0] if args else ())
        if keys == ("witness",):
            yield (("witness", "LEGACY_JUNK"), junk_row)
            yield (("witness", "LEGACY_MATCH"), legacy_row)
        yield from original_iter(*args, **kwargs)

    monkeypatch.setattr(contract.ctx.store.baser.resources, "getTopItemIter", fake_iter)

    response = post_cesr(
        contract,
        "/account",
        build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
    )

    _, reply = assert_reply_frame(contract, response, route="/account/witnesses")
    rows = reply.ked["a"]["witnesses"]
    assert {row["eid"] for row in rows} == {witness_id, "LEGACY_MATCH"}
    assert all(row["eid"] != "LEGACY_JUNK" for row in rows)
    legacy_api = next(row for row in rows if row["eid"] == "LEGACY_MATCH")
    assert legacy_api["created_at"] == ""
    assert legacy_api["witness_url"] == "https://legacy.example"


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        ("/account/witnesses", lambda bundle: {"account_aid": bundle["account"].pre}),
        ("/account/watchers", lambda bundle: {"account_aid": bundle["account"].pre}),
        ("/account/delete", lambda bundle: {"account_aid": bundle["account"].pre}),
        (
            "/account/watchers/status",
            lambda bundle: {"account_aid": bundle["account"].pre, "watcher_id": bundle["watcher_id"]},
        ),
        (
            "/account/witnesses/delete",
            lambda bundle: {"account_aid": bundle["account"].pre, "witness_id": bundle["witness_ids"][0]},
        ),
        (
            "/account/watchers/delete",
            lambda bundle: {"account_aid": bundle["account"].pre, "watcher_id": bundle["watcher_id"]},
        ),
    ],
)
def test_approved_account_routes_require_an_onboarded_account(pending_account_bundle, route, payload):
    contract = pending_account_bundle["contract"]
    account = pending_account_bundle["account"]

    response = post_cesr(
        contract,
        "/account",
        build_exn(account, route=route, payload=payload(pending_account_bundle)),
    )

    assert response.status_code == 409
    assert response.json["title"] == "Account not onboarded"
    assert contract.ctx.store.getAccount(account.pre).status == ACCOUNT_STATE_PENDING_ONBOARDING


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        ("/account/witnesses", {"account_aid": "different-account"}),
        ("/account/watchers", {"account_aid": "different-account"}),
        ("/account/delete", {"account_aid": "different-account"}),
        (
            "/account/watchers/status",
            {"account_aid": "different-account", "watcher_id": "ignored"},
        ),
        (
            "/account/witnesses/delete",
            {"account_aid": "different-account", "witness_id": "ignored"},
        ),
        (
            "/account/watchers/delete",
            {"account_aid": "different-account", "watcher_id": "ignored"},
        ),
    ],
)
def test_approved_account_routes_reject_account_principal_mismatch(onboarded_bundle, route, payload):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]

    response = post_cesr(contract, "/account", build_exn(account, route=route, payload=payload))

    assert response.status_code == 401
    assert response.json["title"] == "Account principal mismatch"


def test_account_resource_routes_return_404_for_missing_resources(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]

    missing_watcher = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/status",
            payload={"account_aid": account.pre, "watcher_id": "missing-watcher"},
        ),
    )
    assert missing_watcher.status_code == 404
    assert missing_watcher.json["title"] == "Watcher not found"

    missing_witness_delete = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_id": "missing-witness"},
        ),
    )
    assert missing_witness_delete.status_code == 404
    assert missing_witness_delete.json["title"] == "Witness not found"

    missing_watcher_delete = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/delete",
            payload={"account_aid": account.pre, "watcher_id": "missing-watcher"},
        ),
    )
    assert missing_watcher_delete.status_code == 404
    assert missing_watcher_delete.json["title"] == "Watcher not found"


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_title"),
    [
        (400, 400, "Boot API rejected request"),
        (404, 404, "Upstream resource not found"),
        (409, 409, "Boot API conflict"),
        (503, 502, "Boot API call failed"),
    ],
)
def test_account_routes_map_downstreambootErrors_to_http_statuses(
    onboarded_bundle,
    status_code,
    expected_status,
    expected_title,
):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]

    contract.ctx.watcher_boot.status_error = BootError(f"downstream {status_code}", status_code=status_code)

    response = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/status",
            payload={"account_aid": account.pre, "watcher_id": onboarded_bundle["watcher_id"]},
        ),
    )

    assert response.status_code == expected_status
    assert response.json["title"] == expected_title


def test_account_routes_enforce_persisted_witness_profile_code(contract_factory):
    """Tests account-route quotas use the account's stored witness profile."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=100,
                kel_budget=100
            ),
            AccountProfile(
                tier="org",
                code="3-of-4",
                max_accounts=100,
                max_requests_per_minute=4,
                kel_budget=100
            ),
        ),
    )

    with (
        habbing.openHab(name="persisted-profile-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="persisted-profile-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        register_aid(contract, "/account", account)

        # Complete onboarding with "3-of-4" profile
        _, _, start_reply = start_session(
            contract,
            ephemeral,
            account_aid=account.pre,
            account_alias="org-alpha",
            chosen_profile_code="3-of-4",
        )
        create_account(contract, ephemeral, start_reply, account_aid=account.pre)
        complete_session(
            contract,
            ephemeral,
            session_id=start_reply.ked["a"]["session_id"],
            account_aid=account.pre,
        )

        record = contract.ctx.store.getAccount(account.pre)
        assert record.witness_profile_code == "3-of-4"

        accepted = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
        )
        assert accepted.status_code == 200

        # Request gets rejected based on the "3-of-4" profile's max_requests_per_minute limit
        rejected = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
        )

    assert rejected.status_code == 429
    assert rejected.json["title"] == "Account request rate limit exceeded"


def test_expire_accounts_transitions_onboarded_account_to_expired(onboarded_bundle):
    """Ensure onboarded accounts are moved to expired status when their expiry date passes."""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    record = contract.ctx.store.getAccount(account.pre)
    
    # Set the account expiry date to the past to trigger expiration
    record.expires_at = "2000-01-01T00:00:00+00:00"
    contract.ctx.store.saveAccount(record)

    # Manually trigger the expiration process
    contract.ctx.exchanger.expirer.expireAccounts()

    updated = contract.ctx.store.getAccount(account.pre)
    assert updated is not None
    assert updated.status == ACCOUNT_STATE_EXPIRED


def test_account_route_expires_past_due_account_on_ingress(onboarded_bundle):
    """Tests account requests trigger lifecycle expiry before route handling."""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    record = contract.ctx.store.getAccount(account.pre)
    record.expires_at = "2000-01-01T00:00:00+00:00"
    contract.ctx.store.saveAccount(record)

    response = post_cesr(
        contract,
        "/account",
        build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
    )

    updated = contract.ctx.store.getAccount(account.pre)
    assert updated is not None
    assert updated.status == ACCOUNT_STATE_EXPIRED
    assert response.status_code == 409
    assert response.json["title"] == "Account expired"


@pytest.mark.parametrize(
    ("status", "title"),
    [
        (ACCOUNT_STATE_PAUSED, "Account paused"),
        (ACCOUNT_STATE_EXPIRED, "Account expired"),
    ],
)
def test_account_routes_reject_paused_or_expired_accounts(onboarded_bundle, status, title):
    """Ensure paused or expired accounts cannot use account routes."""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    record = contract.ctx.store.getAccount(account.pre)

    # Set status to paused or expired to trigger rejection of account routes
    record.status = status
    contract.ctx.store.saveAccount(record)

    response = post_cesr(
        contract,
        "/account",
        build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
    )

    # Assert that the request is rejected with the appropriate status code and message
    assert response.status_code == 409
    assert response.json["title"] == title

def test_expire_accounts_triggers_resource_teardown(contract_factory, monkeypatch):
    """Ensure expired accounts trigger cleanup of allocated resources."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
    )

    # Create an onboarded account with resources
    account = contract.ctx.store.buildAccount(
        account_aid="AID_EXPIRED",
        account_alias="beta",
        witness_profile_code="1-of-1",
        witness_count=1,
        toad=1,
        watcher_required=True,
        region_id="test-region",
        region_name="Test Region",
        session_id="SESSION123",
        witness_eids=["WITNESS123"],
        watcher_eid="WATCHER123",
        tier="trial",
        onboarded=True,
    )
    account.status = ACCOUNT_STATE_ONBOARDED
    account.expires_at = "2000-01-01T00:00:00+00:00"
    contract.ctx.store.saveAccount(account)

    cleaned: list[tuple] = []

    def fake_teardown(*, account_aid: str, account=None) -> None:
        # Simulate what teardown_accountResources would do
        account.watcher_eid = ""
        account.witness_eids = []
        account.session_id = ""
        contract.ctx.store.saveAccount(account)
        cleaned.append((account_aid, account))

    monkeypatch.setattr(contract.ctx.exchanger.provisioner, "teardownAccountResources", fake_teardown)

    # Run expiration logic
    contract.ctx.exchanger.expirer.expireAccounts()

    expired = contract.ctx.store.getAccount("AID_EXPIRED")
    assert expired is not None
    assert expired.status == ACCOUNT_STATE_EXPIRED

    # Ensure teardown was invoked exactly once with correct args
    assert cleaned == [("AID_EXPIRED", expired)]

    # Assert that resources were actually cleared
    assert expired.watcher_eid == ""
    assert expired.witness_eids == []
    assert expired.session_id == ""

def test_account_request_quota_survives_store_reopen(tmp_path):
    """Test account quotas throttle are persistent even after closing"""
    # Create a config with 2 max requests per minute
    config = make_config(
        tmp_path,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=2,
                kel_budget=100,
            ),
        ),
    )
    store_path = config.db_path

    # Create an element that runs session/start 
    serder = SimpleNamespace(
        pre="AID_DURABLE_ACCOUNT",
        ked={
            "r": "/onboarding/session/start",
            "a": {
                "account_aid": "AID_DURABLE_ACCOUNT",
                "chosen_profile_code": "1-of-1",
            },
        },
    )

    first_store = Store(store_path, session_ttl_seconds=config.session_ttl_seconds)
    try:
        # Create limiter
        limiter = Limiter(SimpleNamespace(config=config, store=first_store))

        # Run /session/start 2 times
        limiter.enforceAccountQuotas(serder)
        limiter.enforceAccountQuotas(serder)
    finally:
        # Close the store
        first_store.close()
    
    # Create the same store with the same path and config
    reopened_store = Store(store_path, session_ttl_seconds=config.session_ttl_seconds)
    try:
        # Create Limiter with the same config and the reopened store
        limiter = Limiter(SimpleNamespace(config=config, store=reopened_store))
        with pytest.raises(falcon.HTTPTooManyRequests) as excinfo:
            # Try to run /session/start again
            limiter.enforceAccountQuotas(serder)
        # Assert request was rejected and correct warning produced
        assert excinfo.value.title == "Account request rate limit exceeded"
    finally:
        reopened_store.close()
