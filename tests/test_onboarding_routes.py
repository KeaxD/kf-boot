from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import kfboot.boot_exchanger as boot_exchanger
from keri.app import habbing

from kfboot.basing import (
    ACCOUNT_STATE_FAILED,
    ACCOUNT_STATE_ONBOARDED,
    ACCOUNT_STATE_PAUSED,
    ACCOUNT_STATE_EXPIRED,
    ACCOUNT_STATE_PENDING_ONBOARDING,
    SESSION_STATE_ACCOUNT_CREATED,
    SESSION_STATE_CANCELLED,
    SESSION_STATE_COMPLETED,
    SESSION_STATE_EXPIRED,
    SESSION_STATE_FAILED,
    SESSION_STATE_WITNESS_POOL_ALLOCATED,
)
from kfboot.config import AccountProfile
from .support import (
    FakeWatcherBoot,
    account_create_payload,
    assert_reply_frame,
    boot_error,
    build_exn,
    complete_session,
    create_account,
    freeze_boot_time,
    make_witness_backends,
    post_cesr,
    register_aid,
    start_payload,
    start_session,
    total_witness_create_calls,
    total_witness_created_eids,
    total_witness_delete_calls,
)


def test_onboarding_flow_persists_state_transitions_and_bound_resources(contract):
    with (
        habbing.openHab(name="flow-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="flow-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)

        session_id = start_reply.ked["a"]["session_id"]
        witness_id = start_reply.ked["a"]["witnesses"][0]["eid"]
        watcher_id = start_reply.ked["a"]["watcher"]["eid"]
        assert "boot_url" in start_reply.ked["a"]["witnesses"][0]
        assert "boot_url" in start_reply.ked["a"]["watcher"]

        session = contract.ctx.store.get_session(session_id)
        assert session.state == SESSION_STATE_WITNESS_POOL_ALLOCATED
        assert session.witness_eids == [witness_id]
        assert session.watcher_eid == watcher_id
        assert contract.ctx.store.get_resource("witness", witness_id).principal == ""
        assert contract.ctx.store.get_resource("witness", witness_id).cid == ""
        assert contract.ctx.store.get_resource("watcher", watcher_id).principal == ""
        assert contract.ctx.store.get_resource("watcher", watcher_id).cid == ""

        status_response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/status",
                payload={"session_id": session_id},
            ),
        )
        _, status_reply = assert_reply_frame(contract, status_response, route="/onboarding/session/status")
        assert status_reply.ked["a"]["state"] == SESSION_STATE_WITNESS_POOL_ALLOCATED
        assert "boot_url" in status_reply.ked["a"]["witnesses"][0]
        assert "boot_url" in status_reply.ked["a"]["watcher"]

        _, _, create_reply = create_account(contract, ephemeral, start_reply, account_aid=account.pre)
        assert create_reply.ked["a"]["account"]["account_aid"] == account.pre

        session = contract.ctx.store.get_session(session_id)
        account_record = contract.ctx.store.get_account(account.pre)
        assert session.state == SESSION_STATE_ACCOUNT_CREATED
        assert session.account_aid == account.pre
        assert account_record.status == ACCOUNT_STATE_PENDING_ONBOARDING
        assert contract.ctx.store.get_resource("witness", witness_id).principal == account.pre
        assert contract.ctx.store.get_resource("witness", witness_id).cid == account.pre
        assert contract.ctx.store.get_resource("watcher", watcher_id).principal == account.pre
        assert contract.ctx.store.get_resource("watcher", watcher_id).cid == account.pre

        _, _, complete_reply = complete_session(
            contract,
            ephemeral,
            session_id=session_id,
            account_aid=account.pre,
        )
        assert complete_reply.ked["a"]["state"] == SESSION_STATE_COMPLETED

        session = contract.ctx.store.get_session(session_id)
        account_record = contract.ctx.store.get_account(account.pre)
        assert session.state == SESSION_STATE_COMPLETED
        assert account_record.status == ACCOUNT_STATE_ONBOARDED
        assert account_record.onboarded_at


def test_session_start_is_idempotent_and_does_not_duplicate_allocations(contract):
    with habbing.openHab(name="start-idempotent", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)

        _, _, first = start_session(contract, ephemeral, chosen_profile_code="3-of-4")
        _, _, second = start_session(contract, ephemeral, chosen_profile_code="3-of-4")
        session = contract.ctx.store.get_session(first.ked["a"]["session_id"])
        records = [
            contract.ctx.store.get_resource("witness", row["eid"])
            for row in first.ked["a"]["witnesses"]
        ]
        backend_ids = [record.backend_id for record in records]

        assert first.ked["a"]["session_id"] == second.ked["a"]["session_id"]
        assert first.ked["a"]["witnesses"] == second.ked["a"]["witnesses"]
        assert first.ked["a"]["watcher"] == second.ked["a"]["watcher"]
        assert len(set(backend_ids)) == 4
        assert session.witness_backend_ids == backend_ids
        assert total_witness_create_calls(contract.ctx) == 4
        assert contract.ctx.watcher_boot.create_calls == 1
        assert contract.ctx.store.count_resources("witness") == 4
        assert contract.ctx.store.count_resources("watcher") == 1
        for backend_id in session.witness_backend_ids:
            assert contract.ctx.witness_boots[backend_id].create_cids == ["AID_ACCOUNT"]
        for record in records:
            backend = next(
                backend
                for backend in contract.ctx.config.witness_backends
                if backend.id == record.backend_id
            )
            assert record.url == backend.public_url
            assert record.boot_url == backend.boot_url
            assert record.cid == ""
            assert record.principal == ""

        watcher_record = contract.ctx.store.get_resource("watcher", first.ked["a"]["watcher"]["eid"])
        assert watcher_record.cid == ""
        assert watcher_record.principal == ""
        assert contract.ctx.watcher_boot.create_cids == ["AID_ACCOUNT"]


def test_session_start_rejects_fresh_ephemeral_retry_for_active_account(contract):
    with (
        habbing.openHab(name="start-active-account-owner", temp=True, transferable=False) as (_, owner),
        habbing.openHab(name="start-active-account-other", temp=True, transferable=False) as (_, other),
    ):
        register_aid(contract, "/onboarding", owner)
        register_aid(contract, "/onboarding", other)
        _, _, first = start_session(contract, owner)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(other, route="/onboarding/session/start", payload=start_payload()),
        )

    assert response.status_code == 409
    assert response.json["title"] == "Account session already active"
    assert total_witness_create_calls(contract.ctx) == 1
    assert contract.ctx.watcher_boot.create_calls == 1
    session = contract.ctx.store.get_session(first.ked["a"]["session_id"])
    assert session.ephemeral_aid == owner.pre


def test_session_start_rejects_already_onboarded_account(contract):
    with (
        habbing.openHab(name="start-onboarded-owner", temp=True, transferable=False) as (_, owner),
        habbing.openHab(name="start-onboarded-account", temp=True) as (_, account),
        habbing.openHab(name="start-onboarded-retry", temp=True, transferable=False) as (_, retry),
    ):
        register_aid(contract, "/onboarding", owner)
        register_aid(contract, "/onboarding", retry)
        _, _, start_reply = start_session(contract, owner, account_aid=account.pre)
        create_account(contract, owner, start_reply, account_aid=account.pre)
        complete_session(contract, owner, session_id=start_reply.ked["a"]["session_id"], account_aid=account.pre)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(retry, route="/onboarding/session/start", payload=start_payload(account_aid=account.pre)),
        )

    assert response.status_code == 409
    assert response.json["title"] == "Account already onboarded"
    assert total_witness_create_calls(contract.ctx) == 1
    assert contract.ctx.watcher_boot.create_calls == 1


@pytest.mark.parametrize("state", [SESSION_STATE_CANCELLED, SESSION_STATE_EXPIRED])
def test_session_start_rejects_closed_existing_session_without_new_allocations(contract, state):
    with habbing.openHab(name=f"start-closed-{state}", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral)
        session_id = start_reply.ked["a"]["session_id"]
        session = contract.ctx.store.get_session(session_id)
        session.state = state
        contract.ctx.store.save_session(session)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )

    assert response.status_code == 409
    assert response.json["title"] == "Session closed"
    assert contract.ctx.store.get_session(session_id).state == state
    assert total_witness_create_calls(contract.ctx) == 1
    assert contract.ctx.watcher_boot.create_calls == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_aid", "different-account"),
        ("account_alias", "different-alias"),
        ("chosen_profile_code", "3-of-4"),
        ("region_id", "different-region"),
    ],
)
def test_session_start_retry_parameter_mismatch_conflicts_without_new_allocations(contract, field, value):
    with habbing.openHab(name=f"start-mismatch-{field}", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, first = start_session(contract, ephemeral)
        session_id = first.ked["a"]["session_id"]
        original_session = contract.ctx.store.get_session(session_id)

        retry_payload = start_payload()
        retry_payload[field] = value
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=retry_payload),
        )

        assert response.status_code == 409
        assert response.json["title"] == "Session parameter mismatch"
        assert total_witness_create_calls(contract.ctx) == 1
        assert contract.ctx.watcher_boot.create_calls == 1
        assert contract.ctx.store.get_session(session_id).account_alias == original_session.account_alias


def test_session_start_retry_rejects_watcher_requirement_mismatch_when_optional(contract_factory):
    contract = contract_factory(bootstrap_watcher_required=False)

    with habbing.openHab(name="start-mismatch-watcher-required", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        start_session(contract, ephemeral, watcher_required=True)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/start",
                payload=start_payload(watcher_required=False),
            ),
        )

    assert response.status_code == 409
    assert response.json["title"] == "Session parameter mismatch"
    assert total_witness_create_calls(contract.ctx) == 1
    assert contract.ctx.watcher_boot.create_calls == 1


@pytest.mark.parametrize(
    ("payload", "expected_title"),
    [
        ({"chosen_profile_code": "2-of-3"}, "Unsupported witness profile"),
        ({"watcher_required": False}, "Watcher required"),
    ],
)
def test_session_start_rejects_invalid_profile_and_missing_required_watcher(contract, payload, expected_title):
    with habbing.openHab(name=f"invalid-start-{expected_title}", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload(**payload)),
        )

        assert response.status_code == 400
        assert response.json["title"] == expected_title
        assert contract.ctx.store.find_active_session_for_ephemeral(ephemeral.pre) is None


def test_session_start_rejects_profile_not_supported_by_configured_witness_pool(contract_factory):
    contract = contract_factory(witness_backends=make_witness_backends(1))

    with habbing.openHab(name="invalid-start-configured-pool", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/start",
                payload=start_payload(chosen_profile_code="3-of-4"),
            ),
        )

    assert response.status_code == 400
    assert response.json["title"] == "Unsupported witness profile"
    assert contract.ctx.store.find_active_session_for_ephemeral(ephemeral.pre) is None


def test_session_status_requires_existing_session_and_onboarding_principal(contract):
    with (
        habbing.openHab(name="status-owner", temp=True, transferable=False) as (_, owner),
        habbing.openHab(name="status-other", temp=True, transferable=False) as (_, other),
    ):
        register_aid(contract, "/onboarding", owner)
        register_aid(contract, "/onboarding", other)
        _, _, start_reply = start_session(contract, owner)
        session_id = start_reply.ked["a"]["session_id"]

        missing = post_cesr(
            contract,
            "/onboarding",
            build_exn(owner, route="/onboarding/session/status", payload={"session_id": "sess_missing"}),
        )
        assert missing.status_code == 404
        assert missing.json["title"] == "Session not found"

        wrong_principal = post_cesr(
            contract,
            "/onboarding",
            build_exn(other, route="/onboarding/session/status", payload={"session_id": session_id}),
        )
        assert wrong_principal.status_code == 401
        assert wrong_principal.json["title"] == "Wrong principal"


def test_session_status_refreshes_session_lease(contract):
    with habbing.openHab(name="status-refresh", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral)
        session_id = start_reply.ked["a"]["session_id"]
        session = contract.ctx.store.get_session(session_id)
        session.expires_at = "2099-01-01T00:00:00+00:00"
        contract.ctx.store.save_session(session)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/status",
                payload={"session_id": session_id},
            ),
        )
        assert response.status_code == 200

        refreshed = contract.ctx.store.get_session(session_id)
        assert refreshed.expires_at != "2099-01-01T00:00:00+00:00"


def test_partial_downstream_failure_marks_session_failed_and_blind_retry_does_not_duplicate_resources(contract_factory):
    contract = contract_factory(
        watcher_boot=FakeWatcherBoot(create_error=boot_error(502, "simulated watcher failure"))
    )

    with habbing.openHab(name="partial-failure", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/start",
                payload=start_payload(chosen_profile_code="3-of-4"),
            ),
        )
        assert response.status_code == 502

        session = contract.ctx.store.find_active_session_for_ephemeral(ephemeral.pre)
        assert session.state == SESSION_STATE_FAILED
        assert session.witness_eids == []
        assert session.watcher_eid == ""
        assert total_witness_create_calls(contract.ctx) == 4
        assert contract.ctx.watcher_boot.create_calls == 1
        assert contract.ctx.store.count_resources("witness") == 0
        assert contract.ctx.store.count_resources("watcher") == 0
        assert sorted(total_witness_delete_calls(contract.ctx)) == sorted(total_witness_created_eids(contract.ctx))

        retry = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/start",
                payload=start_payload(chosen_profile_code="3-of-4"),
            ),
        )
        assert retry.status_code == 409
        assert total_witness_create_calls(contract.ctx) == 4
        assert contract.ctx.watcher_boot.create_calls == 1


def test_expired_sessions_reclaim_hosted_resources_and_fail_pending_account(contract):
    with (
        habbing.openHab(name="expiry-owner-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="expiry-owner-account", temp=True) as (_, account),
        habbing.openHab(name="expiry-next-ephemeral", temp=True, transferable=False) as (_, next_ephemeral),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        create_account(contract, ephemeral, start_reply, account_aid=account.pre)

        session_id = start_reply.ked["a"]["session_id"]
        witness_id = start_reply.ked["a"]["witnesses"][0]["eid"]
        watcher_id = start_reply.ked["a"]["watcher"]["eid"]

        expired = contract.ctx.store.get_session(session_id)
        expired.expires_at = "2024-01-01T00:00:00+00:00"
        contract.ctx.store.save_session(expired)

        register_aid(contract, "/onboarding", next_ephemeral)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(next_ephemeral, route="/onboarding/session/start", payload=start_payload(account_aid="AID_NEXT")),
        )

        assert response.status_code == 200

        expired = contract.ctx.store.get_session(session_id)
        account_record = contract.ctx.store.get_account(account.pre)
        assert expired.state == SESSION_STATE_EXPIRED
        assert account_record.status == ACCOUNT_STATE_FAILED
        assert contract.ctx.store.get_resource("witness", witness_id) is None
        assert contract.ctx.store.get_resource("watcher", watcher_id) is None
        assert witness_id in total_witness_delete_calls(contract.ctx)
        assert watcher_id in contract.ctx.watcher_boot.delete_calls


def test_session_start_enforces_witness_capacity(contract_factory):
    contract = contract_factory(witness_limit=0)

    with habbing.openHab(name="witness-capacity", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )

    assert response.status_code == 409
    session = contract.ctx.store.find_active_session_for_ephemeral(ephemeral.pre)
    assert session.state == SESSION_STATE_FAILED
    assert session.witness_eids == []
    assert session.watcher_eid == ""
    assert contract.ctx.store.count_resources("witness") == 0


def test_session_start_enforces_watcher_capacity_without_duplicate_witnesses(contract_factory):
    contract = contract_factory(watcher_limit=0)

    with habbing.openHab(name="watcher-capacity", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )
        assert response.status_code == 409

        session = contract.ctx.store.find_active_session_for_ephemeral(ephemeral.pre)
        assert session.state == SESSION_STATE_FAILED
        assert session.witness_eids == []
        assert session.watcher_eid == ""
        assert contract.ctx.store.count_resources("witness") == 0
        assert contract.ctx.store.count_resources("watcher") == 0
        assert len(total_witness_delete_calls(contract.ctx)) == 1

        retry = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )
        assert retry.status_code == 409
        assert total_witness_create_calls(contract.ctx) == 1
        assert contract.ctx.watcher_boot.create_calls == 0


def test_session_start_enforces_per_ip_account_limit(contract_factory):
    contract = contract_factory(bootstrap_accounts_per_ip=1, bootstrap_aids_per_ip=10)

    with (
        habbing.openHab(name="ip-account-owner", temp=True, transferable=False) as (_, first),
        habbing.openHab(name="ip-account-other", temp=True, transferable=False) as (_, second),
    ):
        register_aid(contract, "/onboarding", first)
        register_aid(contract, "/onboarding", second)
        first_response = post_cesr(
            contract,
            "/onboarding",
            build_exn(first, route="/onboarding/session/start", payload=start_payload()),
            remote_addr="127.0.0.1",
        )
        assert first_response.status_code == 200

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(second, route="/onboarding/session/start", payload=start_payload(account_aid="AID_OTHER")),
            remote_addr="127.0.0.1",
        )

    assert response.status_code == 429
    assert response.json["title"] == "Per-IP onboarding account limit exceeded"


def test_session_start_enforces_per_ip_onboarding_principal_limit(contract_factory):
    contract = contract_factory(bootstrap_accounts_per_ip=10, bootstrap_aids_per_ip=1)

    with (
        habbing.openHab(name="ip-principal-owner", temp=True, transferable=False) as (_, first),
        habbing.openHab(name="ip-principal-other", temp=True, transferable=False) as (_, second),
    ):
        register_aid(contract, "/onboarding", first)
        register_aid(contract, "/onboarding", second)
        post_cesr(
            contract,
            "/onboarding",
            build_exn(first, route="/onboarding/session/start", payload=start_payload()),
            remote_addr="127.0.0.1",
        )

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(second, route="/onboarding/session/start", payload=start_payload(account_aid="AID_OTHER")),
            remote_addr="127.0.0.1",
        )

    assert response.status_code == 429
    assert response.json["title"] == "Per-IP onboarding principal limit exceeded"


def test_session_start_enforces_account_request_rate_limit(contract_factory):
    """Verify that onboarding session-start requests are throttled per account tier."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=10,
        bootstrap_aids_per_ip=10,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=1,
                max_requests_per_minute=2,
                kel_budget=100
            ),
        ),
    )

    with habbing.openHab(name="rate-limit-ephemeral", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)

        # Make the maximum allowed number of requests by starting 2 sessions
        start_session(contract, ephemeral)
        start_session(contract, ephemeral)

        # The 3rd request exceeds the max_requests_per_minute limitand should be rejected
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )

    assert response.status_code == 429
    assert response.json["title"] == "Account request rate limit exceeded"


def test_session_start_request_rate_limit_resets_after_minute(contract_factory, monkeypatch):
    """Tests per-account request throttles clear when the minute window rolls over."""
    # Instantiate boot time
    clock = freeze_boot_time(monkeypatch, datetime(2026, 1, 1, tzinfo=UTC))
    contract = contract_factory(
        bootstrap_accounts_per_ip=10,
        bootstrap_aids_per_ip=10,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=2,
                kel_budget=100
            ),
        ),
    )

    with habbing.openHab(name="rate-rollover-ephemeral", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)

        # Send 2 requests to reach the max_requests_per_minute limit
        start_session(contract, ephemeral)
        start_session(contract, ephemeral)

        rejected = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )
        assert rejected.status_code == 429
        assert rejected.json["title"] == "Account request rate limit exceeded"

        # Advance clock by 61 seconds to reset the limit window 
        clock.value += timedelta(seconds=61)
        accepted = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )
    # Assert the request is accepted 
    assert accepted.status_code == 200


def test_session_start_kel_budget_exhausts_fixed_quota(contract_factory, monkeypatch):
    """Tests fixed KEL budgets are enforced without resetting over time."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=10,
        bootstrap_aids_per_ip=10,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=100,
                kel_budget=2,
            ),
        ),
    )

    with habbing.openHab(name="kel-exhaustion-ephemeral", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)

        # Send 2 requests to exhaust the account's fixed KEL budget
        start_session(contract, ephemeral)
        start_session(contract, ephemeral)

        rejected = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )

    assert rejected.status_code == 429
    assert rejected.json["title"] == "Account key event budget exceeded"


def test_session_start_request_rate_limit_is_scoped_per_account(contract_factory):
    """Verify one account exhausting request rate does not throttle another account."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=10,
        bootstrap_aids_per_ip=10,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=2,
                kel_budget=100
            ),
        ),
    )

    with (
        habbing.openHab(name="rate-isolated-account-a", temp=True, transferable=False) as (_, first),
        habbing.openHab(name="rate-isolated-account-b", temp=True, transferable=False) as (_, second),
    ):
        register_aid(contract, "/onboarding", first)
        register_aid(contract, "/onboarding", second)

        start_session(contract, first, account_aid="AID_RATE_A", account_alias="alpha-a")
        start_session(contract, first, account_aid="AID_RATE_A", account_alias="alpha-a")
        rejected = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                first,
                route="/onboarding/session/start",
                payload=start_payload(account_aid="AID_RATE_A", account_alias="alpha-a"),
            ),
        )
        assert rejected.status_code == 429
        assert rejected.json["title"] == "Account request rate limit exceeded"

        accepted = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                second,
                route="/onboarding/session/start",
                payload=start_payload(account_aid="AID_RATE_B", account_alias="alpha-b"),
            ),
        )

    assert accepted.status_code == 200


def test_session_start_rejects_account_alias_over_limit(contract_factory):
    """Verify onboarding rejects a new session when the alias already has the max onboarded accounts."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=1,
                max_requests_per_minute=100,
                kel_budget=100
            ),
        ),
    )

    with (
        habbing.openHab(name="alias-limit-ephemeral-1", temp=True, transferable=False) as (_, ephemeral1),
        habbing.openHab(name="alias-limit-account-1", temp=True) as (_, account1),
        habbing.openHab(name="alias-limit-ephemeral-2", temp=True, transferable=False) as (_, ephemeral2),
    ):
        register_aid(contract, "/onboarding", ephemeral1)
        register_aid(contract, "/account", account1)

        # Start and complete a session 
        _, _, start_reply = start_session(contract, ephemeral1, account_aid=account1.pre)
        create_account(contract, ephemeral1, start_reply, account_aid=account1.pre)
        _, _, _ = complete_session(
            contract,
            ephemeral1,
            session_id=start_reply.ked["a"]["session_id"],
            account_aid=account1.pre,
        )

        register_aid(contract, "/onboarding", ephemeral2)

        # Attempt to start a session with a different ephemeral but the same alias
        # which should be rejected because the alias already has the max onboarded accounts
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral2, route="/onboarding/session/start", payload=start_payload(account_aid="AID_SECOND", account_alias="alpha")),
        )

    assert response.status_code == 429
    assert response.json["title"] == "Account alias limit exceeded"
    assert "configured limit for tier 'trial' is 1" in response.json["description"]


def test_session_start_rejects_alias_when_existing_account_is_pending(contract_factory):
    """Verify onboarding rejects a new session when the alias already has an account pending onboarding."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=1,
                max_requests_per_minute=100,
                kel_budget=100
            ),
        ),
    )

    with (
        habbing.openHab(name="alias-pending-ephemeral-1", temp=True, transferable=False) as (_, ephemeral1),
        habbing.openHab(name="alias-pending-account-1", temp=True) as (_, account1),
        habbing.openHab(name="alias-pending-ephemeral-2", temp=True, transferable=False) as (_, ephemeral2),
    ):
        # Don't complete the session to leave the account in pending onboarding status
        register_aid(contract, "/onboarding", ephemeral1)
        register_aid(contract, "/account", account1)

        _, _, start_reply = start_session(
            contract,
            ephemeral1,
            account_aid=account1.pre,
            account_alias="alpha",
        )
        
        create_account(contract, ephemeral1, start_reply, account_aid=account1.pre)

        # Register and start a session with a different ephemeral but the same alias
        register_aid(contract, "/onboarding", ephemeral2)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral2,
                route="/onboarding/session/start",
                payload=start_payload(account_aid="AID_SECOND", account_alias="alpha"),
            ),
        )
    # Assert the second session is rejected due to the alias already having an account pending onboarding
    assert response.status_code == 429
    assert response.json["title"] == "Account alias limit exceeded"
    assert "configured limit for tier 'trial' is 1" in response.json["description"]


@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [
        (ACCOUNT_STATE_PAUSED, "paused"),
        (ACCOUNT_STATE_EXPIRED, "expired"),
    ],
)
def test_account_create_rejects_paused_or_expired_permanent_account(contract_factory, status, expected_reason):
    """Verify that onboarding rejects account creation when the account is paused or expired."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
    )

    with (
        habbing.openHab(name=f"{status}-account-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name=f"{status}-account", temp=True) as (_, account),
    ):
        # Onboard the account and then manually set it to the desired status
        register_aid(contract, "/onboarding", ephemeral)
        register_aid(contract, "/account", account)

        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        create_account(contract, ephemeral, start_reply, account_aid=account.pre)

        record = contract.ctx.store.get_account(account.pre)
        assert record is not None

        # Set the account status to paused/expired 
        record.status = status
        contract.ctx.store.save_account(record)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/account/create",
                payload=account_create_payload(start_reply, account.pre),
            ),
        )
    # Assert the account creation is rejected
    assert response.status_code == 409
    assert response.json["title"] == "Account not available"
    assert expected_reason in response.json["description"]


def test_account_request_rate_soft_warning_thresholds_before_hard_limit(contract_factory, monkeypatch):
    """Verify request-rate soft warnings are logged before the hard throttle is applied."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=1,
                max_requests_per_minute=10,
                kel_budget=100
            ),
        ),
    )

    with habbing.openHab(name="rate-warning-ephemeral", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)

        info_calls: list[str] = []
        warning_calls: list[str] = []
        monkeypatch.setattr(
            boot_exchanger.logger,
            "info",
            lambda message, **kwargs: info_calls.append(message),
        )
        monkeypatch.setattr(
            boot_exchanger.logger,
            "warning",
            lambda message, **kwargs: warning_calls.append(message),
        )

        for _ in range(9):
            start_session(contract, ephemeral)

        assert "approaching_request_rate_limit" in info_calls

        start_session(contract, ephemeral)
        assert "high_request_rate" in warning_calls

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )

    assert response.status_code == 429
    assert response.json["title"] == "Account request rate limit exceeded"


def test_account_kel_budget_soft_warning_thresholds_before_hard_limit(contract_factory, monkeypatch):
    """Verify KEL budget soft warnings are logged before the final budget exhaustion."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=1,
                max_requests_per_minute=100,
                kel_budget=10
            ),
        ),
    )

    with habbing.openHab(name="kel-warning-ephemeral", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)

        info_calls: list[str] = []
        warning_calls: list[str] = []
        monkeypatch.setattr(
            boot_exchanger.logger,
            "info",
            lambda message, **kwargs: info_calls.append(message),
        )
        monkeypatch.setattr(
            boot_exchanger.logger,
            "warning",
            lambda message, **kwargs: warning_calls.append(message),
        )

        for _ in range(9):
            start_session(contract, ephemeral)

        assert "approaching_kel_budget" in info_calls

        start_session(contract, ephemeral)
        assert "high_kel_usage" in warning_calls

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )

    assert response.status_code == 429
    assert response.json["title"] == "Account key event budget exceeded"


def test_expire_sessions_cleans_up_stale_staging_allocations(contract_factory, monkeypatch):
    """Ensure expired staging sessions trigger cleanup of stale allocations."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
    )

    session = contract.ctx.store.create_session(
        ephemeral_aid="E-STALE",
        account_aid="AID_STALE",
        account_alias="alpha",
        chosen_profile_code="1-of-1",
        client_ip="127.0.0.1",
        region_id="test-region",
        region_name="Test Region",
        watcher_required=True,
        witness_count=1,
        toad=1,
        account_tier="trial",
    )
    session.expires_at = "2000-01-01T00:00:00+00:00"
    contract.ctx.store.save_session(session)

    cleaned: list[tuple] = []

    def fake_teardown(*, session: Any, account=None) -> None:
        cleaned.append((session.session_id, account))

    monkeypatch.setattr(contract.ctx.exchanger, "teardown_session_resources", fake_teardown)

    contract.ctx.exchanger.expire_sessions()

    expired_session = contract.ctx.store.get_session(session.session_id)
    assert expired_session is not None
    assert expired_session.state == SESSION_STATE_EXPIRED
    assert cleaned == [(session.session_id, None)]


def test_account_create_rejects_wrong_onboarding_principal(contract):
    with (
        habbing.openHab(name="create-mismatch-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="create-mismatch-account", temp=True) as (_, account),
        habbing.openHab(name="create-mismatch-other", temp=True, transferable=False) as (_, other),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        register_aid(contract, "/onboarding", other)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                other,
                route="/onboarding/account/create",
                payload=account_create_payload(start_reply, account.pre),
            ),
        )

        assert response.status_code == 401
        assert response.json["title"] == "Wrong onboarding principal"
        session = contract.ctx.store.get_session(start_reply.ked["a"]["session_id"])
        assert session.state == SESSION_STATE_WITNESS_POOL_ALLOCATED


def test_account_create_marks_session_failed_when_resources_are_incomplete(contract):
    with (
        habbing.openHab(name="resources-incomplete-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="resources-incomplete-account", temp=True) as (_, account),
    ):
        session = contract.ctx.store.create_session(
            ephemeral_aid=ephemeral.pre,
            account_aid=account.pre,
            account_alias="alpha",
            chosen_profile_code="1-of-1",
            client_ip="127.0.0.1",
            region_id="test-region",
            region_name="Test Region",
            watcher_required=True,
            witness_count=1,
            toad=1,
            account_tier="trial",
        )
        register_aid(contract, "/onboarding", ephemeral)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/account/create",
                payload={"session_id": session.session_id, "account_aid": account.pre},
            ),
        )

        assert response.status_code == 409
        assert response.json["title"] == "Resources missing"
        saved = contract.ctx.store.get_session(session.session_id)
        assert saved.state == SESSION_STATE_FAILED
        assert "Hosted resources were not fully allocated" in saved.failure_reason


def test_account_create_rejects_account_bound_to_other_session(contract):
    with (
        habbing.openHab(name="existing-account-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="existing-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)

        existing = contract.ctx.store.build_account(
            account_aid=account.pre,
            account_alias="existing",
            witness_profile_code="1-of-1",
            witness_count=1,
            toad=1,
            watcher_required=True,
            region_id="test-region",
            region_name="Test Region",
            session_id="sess_other",
            witness_eids=["W1"],
            watcher_eid="WA1",
        )
        contract.ctx.store.save_account(existing)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/account/create",
                payload=account_create_payload(start_reply, account.pre),
            ),
        )

        assert response.status_code == 409
        assert response.json["title"] == "Account already exists"


@pytest.mark.parametrize(
    ("state", "expected_status", "expected_title"),
    [
        ("expired", 410, "Session expired"),
        ("failed", 409, "Session failed"),
        (SESSION_STATE_CANCELLED, 409, "Session cancelled"),
        (SESSION_STATE_COMPLETED, 409, "Session completed"),
    ],
)
def test_account_create_rejects_closed_sessions(contract, state, expected_status, expected_title):
    with (
        habbing.openHab(name=f"closed-create-{state}-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name=f"closed-create-{state}-account", temp=True) as (_, account),
    ):
        session = contract.ctx.store.create_session(
            ephemeral_aid=ephemeral.pre,
            account_aid=account.pre,
            account_alias="alpha",
            chosen_profile_code="1-of-1",
            client_ip="127.0.0.1",
            region_id="test-region",
            region_name="Test Region",
            watcher_required=True,
            witness_count=1,
            toad=1,
            account_tier="trial",
        )
        session.state = state
        if state == "failed":
            session.failure_reason = "downstream failed"
        contract.ctx.store.save_session(session)
        register_aid(contract, "/onboarding", ephemeral)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/account/create",
                payload={"session_id": session.session_id, "account_aid": account.pre},
            ),
        )

        assert response.status_code == expected_status
        assert response.json["title"] == expected_title


def test_complete_rejects_before_account_exists(contract):
    with (
        habbing.openHab(name="complete-before-account-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="complete-before-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        session_id = start_reply.ked["a"]["session_id"]

        session = contract.ctx.store.get_session(session_id)
        session.account_aid = account.pre
        session.state = SESSION_STATE_ACCOUNT_CREATED
        contract.ctx.store.save_session(session)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/complete",
                payload={"session_id": session_id, "account_aid": account.pre},
            ),
        )

        assert response.status_code == 404
        assert response.json["title"] == "Account not found"


def test_complete_rejects_missing_watcher_and_wrong_principal(pending_account_bundle):
    contract = pending_account_bundle["contract"]
    session_id = pending_account_bundle["session_id"]
    account = pending_account_bundle["account"]
    ephemeral = pending_account_bundle["ephemeral"]
    with habbing.openHab(name="complete-wrong-other", temp=True, transferable=False) as (_, other):
        register_aid(contract, "/onboarding", other)

        session = contract.ctx.store.get_session(session_id)
        account_record = contract.ctx.store.get_account(account.pre)
        session.watcher_eid = ""
        account_record.watcher_eid = ""
        contract.ctx.store.save_session(session)
        contract.ctx.store.save_account(account_record)

        wrong_principal = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                other,
                route="/onboarding/complete",
                payload={"session_id": session_id, "account_aid": account.pre},
            ),
        )
        assert wrong_principal.status_code == 401
        assert wrong_principal.json["title"] == "Wrong onboarding principal"

        missing_watcher = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/complete",
                payload={"session_id": session_id, "account_aid": account.pre},
            ),
        )
        assert missing_watcher.status_code == 409
        assert missing_watcher.json["title"] == "Watcher missing"

        session = contract.ctx.store.get_session(session_id)
        account_record = contract.ctx.store.get_account(account.pre)
        assert session.state == SESSION_STATE_FAILED
        assert session.witness_eids == []
        assert session.watcher_eid == ""
        assert account_record.status == ACCOUNT_STATE_FAILED
        assert account_record.witness_eids == []
        assert account_record.watcher_eid == ""
        assert sorted(total_witness_delete_calls(contract.ctx)) == sorted(pending_account_bundle["witness_ids"])
        assert contract.ctx.store.count_resources("witness") == 0
        assert contract.ctx.store.count_resources("watcher") == 0


def test_cancel_marks_session_cancelled_is_idempotent_and_fails_pending_account(contract):
    with (
        habbing.openHab(name="cancel-idempotent-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="cancel-idempotent-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        session_id = start_reply.ked["a"]["session_id"]
        create_account(contract, ephemeral, start_reply, account_aid=account.pre)

        first = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/cancel",
                payload={"session_id": session_id},
            ),
        )
        _, first_reply = assert_reply_frame(contract, first, route="/onboarding/cancel")
        assert first_reply.ked["a"]["state"] == SESSION_STATE_CANCELLED

        second = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/cancel",
                payload={"session_id": session_id},
            ),
        )
        _, second_reply = assert_reply_frame(contract, second, route="/onboarding/cancel")
        assert second_reply.ked["a"]["state"] == SESSION_STATE_CANCELLED
        session = contract.ctx.store.get_session(session_id)
        account_record = contract.ctx.store.get_account(account.pre)
        assert session.state == SESSION_STATE_CANCELLED
        assert session.witness_eids == []
        assert session.watcher_eid == ""
        assert account_record.status == ACCOUNT_STATE_FAILED
        assert account_record.witness_eids == []
        assert account_record.watcher_eid == ""
        assert contract.ctx.store.count_resources("witness") == 0
        assert contract.ctx.store.count_resources("watcher") == 0
        assert len(total_witness_delete_calls(contract.ctx)) == 1
        assert contract.ctx.watcher_boot.delete_calls == ["WAT_1"]


def test_cancel_returns_error_when_teardown_fails_and_leaves_session_retryable(contract_factory):
    contract = contract_factory()

    with (
        habbing.openHab(name="cancel-fail-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="cancel-fail-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        session_id = start_reply.ked["a"]["session_id"]
        witness_id = start_reply.ked["a"]["witnesses"][0]["eid"]
        witness_record = contract.ctx.store.get_resource("witness", witness_id)
        contract.ctx.witness_boots[witness_record.backend_id].delete_error = boot_error(
            503,
            "simulated witness delete failure",
        )
        create_account(contract, ephemeral, start_reply, account_aid=account.pre)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/cancel",
                payload={"session_id": session_id},
            ),
        )

    assert response.status_code == 502
    session = contract.ctx.store.get_session(session_id)
    account_record = contract.ctx.store.get_account(account.pre)
    assert session.state == SESSION_STATE_FAILED
    assert "teardown failed during cancellation" in session.failure_reason.lower()
    assert len(session.witness_eids) == 1
    assert session.watcher_eid == ""
    assert account_record.status == ACCOUNT_STATE_FAILED
    assert len(account_record.witness_eids) == 1
    assert account_record.watcher_eid == ""
    assert contract.ctx.store.count_resources("witness") == 1
    assert contract.ctx.store.count_resources("watcher") == 0
    assert contract.ctx.watcher_boot.delete_calls == ["WAT_1"]
    assert len(total_witness_delete_calls(contract.ctx)) == 1


def test_cancel_rejects_wrong_principal_and_completed_session(contract):
    with (
        habbing.openHab(name="cancel-wrong-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="cancel-wrong-account", temp=True) as (_, account),
        habbing.openHab(name="cancel-wrong-other", temp=True, transferable=False) as (_, other),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        register_aid(contract, "/onboarding", other)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        session_id = start_reply.ked["a"]["session_id"]

        wrong = post_cesr(
            contract,
            "/onboarding",
            build_exn(other, route="/onboarding/cancel", payload={"session_id": session_id}),
        )
        assert wrong.status_code == 401
        assert wrong.json["title"] == "Wrong onboarding principal"

        create_account(contract, ephemeral, start_reply, account_aid=account.pre)
        complete_session(contract, ephemeral, session_id=session_id, account_aid=account.pre)

        completed = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/cancel", payload={"session_id": session_id}),
        )
        assert completed.status_code == 409
        assert completed.json["title"] == "Session completed"
