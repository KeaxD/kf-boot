from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kfboot.basing import (
    ACCOUNT_STATE_FAILED,
    ACCOUNT_STATE_ONBOARDED,
    SESSION_STATE_COMPLETED,
    SESSION_STATE_EXPIRED,
    SessionRecord,
)
from kfboot.store import (
    Store,
    account_failed,
    make_record,
    parse_public_url,
    resources_to_api,
    session_failed,
)


@pytest.fixture
def store(tmp_path):
    instance = Store(str(tmp_path / "store" / "kf-boot"), session_ttl_seconds=60)
    yield instance
    instance.close()


def test_session_creation_lookup_and_payload_integrity(store):
    older = store.create_session(
        ephemeral_aid="E1",
        account_aid="A1",
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
    older.created_at = "2024-01-01T00:00:00+00:00"
    older.updated_at = older.created_at
    older.expires_at = "2099-01-01T00:00:00+00:00"
    older.account_aid = "A1"
    older.witness_eids = ["W0"]
    store.save_session(older)

    newer = store.create_session(
        ephemeral_aid="E1",
        account_aid="A1",
        account_alias="beta",
        chosen_profile_code="3-of-4",
        client_ip="127.0.0.1",
        region_id="test-region",
        region_name="Test Region",
        watcher_required=True,
        witness_count=4,
        toad=3,
        account_tier="org",
    )
    newer.created_at = "2024-01-01T00:00:01+00:00"
    newer.updated_at = newer.created_at
    newer.expires_at = "2099-01-01T00:00:01+00:00"
    newer.account_aid = "A1"
    newer.witness_backend_ids = ["wit-1", "wit-2", "wit-3", "wit-4"]
    newer.witness_eids = ["W1", "W2", "W3", "W4"]
    store.save_session(newer)

    assert newer.session_id.startswith("sess_")
    assert store.get_session(newer.session_id).session_id == newer.session_id
    assert store.find_active_session_for_ephemeral("E1").session_id == newer.session_id
    assert store.find_session_for_account("A1").session_id == newer.session_id

    payload = store.session_payload(newer)
    assert payload["session_id"] == newer.session_id
    assert payload["account_aid"] == "A1"
    assert payload["account_tier"] == "org"
    assert payload["witness_count"] == 4
    assert "witness_backend_ids" not in payload
    payload["witness_eids"].append("EXTRA")
    assert newer.witness_eids == ["W1", "W2", "W3", "W4"]

    account = store.build_account(
        account_aid="A1",
        account_alias="beta",
        witness_profile_code="3-of-4",
        witness_count=4,
        toad=3,
        watcher_required=True,
        region_id="test-region",
        region_name="Test Region",
        session_id=newer.session_id,
        witness_eids=["W1", "W2", "W3", "W4"],
        watcher_eid="WA1",
        tier="org",
        onboarded=True,
    )
    store.save_account(account)

    account_payload = store.account_payload(account)
    assert account_payload["status"] == ACCOUNT_STATE_ONBOARDED
    assert account_payload["tier"] == "org"
    assert account_payload["watcher_eid"] == "WA1"
    account_payload["witness_eids"].append("EXTRA")
    assert account.witness_eids == ["W1", "W2", "W3", "W4"]


def test_expire_sessions_marks_only_non_terminal_records(store):
    open_session = store.create_session(
        ephemeral_aid="E-open",
        account_aid="A-open",
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
    open_session.expires_at = "2024-01-01T00:00:00+00:00"
    store.save_session(open_session)

    terminal = store.create_session(
        ephemeral_aid="E-terminal",
        account_aid="A-terminal",
        account_alias="beta",
        chosen_profile_code="1-of-1",
        client_ip="127.0.0.1",
        region_id="test-region",
        region_name="Test Region",
        watcher_required=True,
        witness_count=1,
        toad=1,
        account_tier="trial",
    )
    terminal.state = SESSION_STATE_COMPLETED
    terminal.expires_at = "2024-01-01T00:00:00+00:00"
    store.save_session(terminal)

    expired = store.expire_sessions(now="2024-01-01T00:00:01+00:00")

    assert store.get_session(open_session.session_id).state == SESSION_STATE_EXPIRED
    assert store.get_session(terminal.session_id).state == SESSION_STATE_COMPLETED
    assert [record.session_id for record in expired] == [open_session.session_id]


def test_refresh_session_lease_extends_expiry_and_tracks_active_ip_sessions(store):
    first = store.create_session(
        ephemeral_aid="E1",
        account_aid="A1",
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
    second = store.create_session(
        ephemeral_aid="E2",
        account_aid="A2",
        account_alias="beta",
        chosen_profile_code="1-of-1",
        client_ip="127.0.0.1",
        region_id="test-region",
        region_name="Test Region",
        watcher_required=True,
        witness_count=1,
        toad=1,
        account_tier="trial",
    )
    third = store.create_session(
        ephemeral_aid="E3",
        account_aid="A3",
        account_alias="gamma",
        chosen_profile_code="1-of-1",
        client_ip="127.0.0.2",
        region_id="test-region",
        region_name="Test Region",
        watcher_required=True,
        witness_count=1,
        toad=1,
        account_tier="trial",
    )
    second.state = SESSION_STATE_COMPLETED
    store.save_session(second)

    first.expires_at = "2024-01-01T00:00:00+00:00"
    store.save_session(first)

    store.refresh_session_lease(first, now="2024-01-01T00:01:00+00:00")

    refreshed = store.get_session(first.session_id)
    assert refreshed.updated_at == "2024-01-01T00:01:00+00:00"
    assert refreshed.expires_at == "2024-01-01T00:02:00+00:00"

    active = store.list_active_sessions_for_ip("127.0.0.1")
    assert [record.session_id for record in active] == [first.session_id]
    assert store.list_active_sessions_for_ip("127.0.0.2")[0].session_id == third.session_id


def test_resource_binding_listing_and_api_payloads(store):
    session = store.create_session(
        ephemeral_aid="E1",
        account_aid="A1",
        account_alias="alpha",
        chosen_profile_code="1-of-1",
        client_ip="127.0.0.1",
        region_id="test-region",
        region_name="Test Region",
        watcher_required=True,
        witness_count=2,
        toad=1,
        account_tier="trial",
    )

    witness_older = make_record(
        kind="witness",
        eid="W1",
        backend_id="wit-1",
        cid="",
        principal="",
        session_id=session.session_id,
        name="Witness 1",
        identifier_alias="alpha",
        region_id="test-region",
        region_name="Test Region",
        public_url="https://witness.example:5632",
        boot_url="http://boot.local/witnesses",
        oobis=["https://witness.example/oobi/W1/controller"],
    )
    witness_newer = make_record(
        kind="witness",
        eid="W2",
        backend_id="wit-2",
        cid="",
        principal="",
        session_id=session.session_id,
        name="Witness 2",
        identifier_alias="alpha",
        region_id="test-region",
        region_name="Test Region",
        public_url="https://witness.example:5632",
        boot_url="http://boot.local/witnesses",
        oobis=["https://witness.example/oobi/W2/controller"],
    )
    watcher = make_record(
        kind="watcher",
        eid="WA1",
        cid="",
        principal="",
        session_id=session.session_id,
        name="Watcher 1",
        identifier_alias="alpha",
        region_id="test-region",
        region_name="Test Region",
        public_url="https://watcher.example",
        boot_url="http://boot.local/watchers",
        oobis=["https://watcher.example/oobi/WA1/controller"],
        status="created",
    )
    witness_older.created_at = "2024-01-01T00:00:00+00:00"
    witness_newer.created_at = "2024-01-01T00:00:01+00:00"
    watcher.created_at = "2024-01-01T00:00:02+00:00"

    store.add_resource(witness_older)
    store.add_resource(witness_newer)
    store.add_resource(watcher)
    session.witness_eids = ["W1", "W2"]
    session.watcher_eid = "WA1"
    store.save_session(session)

    store.bind_resources_to_account(session=session, account_aid="A1")

    assert store.get_resource("witness", "W1").principal == "A1"
    assert store.get_resource("witness", "W1").cid == "A1"
    assert store.get_resource("witness", "W2").cid == "A1"
    assert store.get_resource("watcher", "WA1").principal == "A1"
    assert store.get_resource("watcher", "WA1").cid == "A1"

    ordered = store.list_resources_for_account(kind="witness", account_aid="A1")
    assert [record.eid for record in ordered] == ["W2", "W1"]
    session_rows = store.list_resources_for_session(kind="watcher", session_id=session.session_id)
    assert [record.eid for record in session_rows] == ["WA1"]

    api_rows = resources_to_api([witness_newer, watcher])
    assert api_rows[0]["witness_url"] == "https://witness.example:5632"
    assert "backend_id" not in api_rows[0]
    assert "boot_url" not in api_rows[0]
    assert "principal" not in api_rows[0]
    assert "session_id" not in api_rows[0]
    assert api_rows[1]["watcher_url"] == "https://watcher.example"
    assert "boot_url" not in api_rows[1]
    assert api_rows[1]["status"] == "created"

    onboarding_rows = resources_to_api([witness_newer, watcher], include_boot_url=True)
    assert onboarding_rows[0]["boot_url"] == "http://boot.local/witnesses"
    assert onboarding_rows[1]["boot_url"] == "http://boot.local/watchers"

    assert store.count_resources("witness") == 2
    assert [record.eid for record in store.get_resources("witness", ["W2", "missing", "W1"])] == ["W2", "W1"]
    store.delete_resource("witness", "W1")
    assert store.count_resources("witness") == 1
    assert store.get_resource("witness", "W1") is None


def test_helper_functions_cover_parse_urls_bindings_and_failure_transitions(store):
    assert parse_public_url("https://witness.example:5632") == ("witness.example", 5632)
    assert parse_public_url("https://watcher.example") == ("watcher.example", None)

    record = make_record(
        kind="witness",
        eid="W1",
        backend_id="wit-1",
        cid="E1",
        principal="",
        session_id="sess_1",
        name="Witness 1",
        identifier_alias="alpha",
        region_id="test-region",
        region_name="Test Region",
        public_url="https://witness.example:5632",
        boot_url="http://boot.local/witnesses",
        oobis=["https://witness.example/oobi/W1/controller"],
    )
    assert record.public_host == "witness.example"
    assert record.public_port == 5632

    store.add_binding("principal-1", "cid-1")
    binding = store.baser.bindings.get(keys=("principal-1", "cid-1"))
    assert binding.principal == "principal-1"
    assert binding.cid == "cid-1"

    session = SessionRecord(session_id="sess_1")
    before = datetime.now(UTC)
    failed_session = session_failed(session, "boom")
    after = datetime.now(UTC)
    assert failed_session.state == "failed"
    assert failed_session.failure_reason == "boom"
    assert before <= datetime.fromisoformat(failed_session.updated_at) <= after + timedelta(seconds=1)

    account = store.build_account(
        account_aid="A1",
        account_alias="alpha",
        witness_profile_code="1-of-1",
        witness_count=1,
        toad=1,
        watcher_required=True,
        region_id="test-region",
        region_name="Test Region",
        session_id="sess_1",
        witness_eids=["W1"],
        watcher_eid="WA1",
    )
    assert account_failed(None) is None
    assert account_failed(account).status == ACCOUNT_STATE_FAILED
