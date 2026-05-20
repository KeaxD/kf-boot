from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kfboot.basing import (
    ACCOUNT_STATE_FAILED,
    ACCOUNT_STATE_EXPIRED,
    ACCOUNT_STATE_ONBOARDED,
    CLEANUP_TASK_ACCOUNT_CLEANUP,
    CLEANUP_TASK_ACCOUNT_DELETE,
    CLEANUP_TASK_SESSION_CLEANUP,
    CLEANUP_TASK_SESSION_DELETE,
    CLEANUP_TASK_SESSION_EXPIRE,
    QuotaRecord,
    SESSION_STATE_COMPLETED,
    SESSION_STATE_EXPIRED,
    SESSION_STATE_FAILED,
    SessionRecord,
)
from kfboot.store import (
    Store,
    accountFailed,
    makeRecord,
    parsePublicUrl,
    resourcesToApi,
    sessionFailed,
)


@pytest.fixture
def store(tmp_path):
    instance = Store(str(tmp_path / "store" / "kf-boot"), session_ttl_seconds=60)
    yield instance
    instance.close()


def test_session_creation_lookup_and_payload_integrity(store):
    older = store.createSession(
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
    store.saveSession(older)

    newer = store.createSession(
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
    store.saveSession(newer)

    assert newer.session_id.startswith("sess_")
    assert store.getSession(newer.session_id).session_id == newer.session_id
    assert store.findActiveSessionForEphemeral("E1").session_id == newer.session_id
    assert store.findSessionForAccount("A1").session_id == newer.session_id

    payload = store.sessionPayload(newer)
    assert payload["session_id"] == newer.session_id
    assert payload["account_aid"] == "A1"
    assert payload["account_tier"] == "org"
    assert payload["witness_count"] == 4
    assert "witness_backend_ids" not in payload
    payload["witness_eids"].append("EXTRA")
    assert newer.witness_eids == ["W1", "W2", "W3", "W4"]

    account = store.buildAccount(
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
    store.saveAccount(account)

    accountPayload = store.accountPayload(account)
    assert accountPayload["status"] == ACCOUNT_STATE_ONBOARDED
    assert accountPayload["tier"] == "org"
    assert accountPayload["watcher_eid"] == "WA1"
    accountPayload["witness_eids"].append("EXTRA")
    assert account.witness_eids == ["W1", "W2", "W3", "W4"]


def test_expire_sessions_marks_only_non_terminal_records(store):
    open_session = store.createSession(
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
    store.saveSession(open_session)

    terminal = store.createSession(
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
    store.saveSession(terminal)

    expired = store.expireSessions(now="2024-01-01T00:00:01+00:00")

    assert store.getSession(open_session.session_id).state == SESSION_STATE_EXPIRED
    assert store.getSession(terminal.session_id).state == SESSION_STATE_COMPLETED
    assert [record.session_id for record in expired] == [open_session.session_id]


def test_cleanup_tasks_survive_store_reopen(tmp_path):
    """Test that clean up task are persistent"""
    path = str(tmp_path / "cleanup-store" / "kf-boot")
    first = Store(
        path,
        session_ttl_seconds=60,
        expired_account_retention_seconds=120,
    )
    try:
        session = first.createSession(
            ephemeral_aid="E-clean",
            account_aid="A-clean",
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
        # Set expiration date as now
        session.expires_at = "2024-01-01T00:00:00+00:00"

        # Save the session which should trigger clean up
        first.saveSession(session)

        # Build account from that session
        account = first.buildAccount(
            account_aid="A-clean",
            account_alias="alpha",
            witness_profile_code="1-of-1",
            witness_count=1,
            toad=1,
            watcher_required=True,
            region_id="test-region",
            region_name="Test Region",
            session_id=session.session_id,
            witness_eids=["W1"],
            watcher_eid="WA1",
            onboarded=True,
        )
        
        # Set the account as expired
        account.status = ACCOUNT_STATE_EXPIRED
        account.expired_at = "2024-01-01T00:00:00+00:00"

        # Save the account 
        first.saveAccount(account)
    finally:

        # Close the store to test persistence
        first.close()

    # Open the store with the same path
    second = Store(
        path,
        session_ttl_seconds=60,
        expired_account_retention_seconds=120,
    )
    try:
        # Assert for clean up tasks
        session_task = second.getCleanupTask(CLEANUP_TASK_SESSION_EXPIRE, session.session_id)
        account_task = second.getCleanupTask(CLEANUP_TASK_ACCOUNT_CLEANUP, account.account_aid)
        assert session_task is not None
        assert session_task.due_at == "2024-01-01T00:00:00+00:00"
        assert account_task is not None
        assert account_task.due_at == "2024-01-01T00:00:00+00:00"
    finally:
        second.close()


def test_cleanup_leases_are_persisted_and_exclusive(store):
    """Test that leases are exclusive to their owner and cannot be acquired unless realeased or ttl reached"""

    # owner A gets the lease with a TTL of 30 sec
    assert store.acquireLease(
        "cleanup",
        owner_id="owner-a",
        ttl_seconds=30,
        now="2024-01-01T00:00:00+00:00",
    )

    # owner B tries to acquire the lease after 10 sec, gets denied 
    assert not store.acquireLease(
        "cleanup",
        owner_id="owner-b",
        ttl_seconds=30,
        now="2024-01-01T00:00:10+00:00",
    )

    # Owner B tries again after 21 sec which is 1s after owner A's lease of 30 sec
    assert store.acquireLease(
        "cleanup",
        owner_id="owner-b",
        ttl_seconds=30,
        now="2024-01-01T00:00:31+00:00",
    )


def test_quota_records_are_saved_in_lmdb(tmp_path):
    """Test that the quotas records are saved and persistent"""
    path = str(tmp_path / "quota-store" / "kf-boot")
    first = Store(path, session_ttl_seconds=60)
    first.saveQuota(
        QuotaRecord(
            scope="account_request",
            subject="AID1",
            window_start="2026-01-01T00:00:00+00:00",
            count=2,
            blocked_until="",
        )
    )
    first.close()

    second = Store(path, session_ttl_seconds=60)
    try:
        record = second.getQuota("account_request", "AID1")
        assert record is not None
        assert record.window_start == "2026-01-01T00:00:00+00:00"
        assert record.count == 2
    finally:
        second.close()


def test_refreshSessionLease_extends_expiry_and_tracks_active_ip_sessions(store):
    first = store.createSession(
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
    second = store.createSession(
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
    third = store.createSession(
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
    store.saveSession(second)

    first.expires_at = "2099-01-01T00:00:00+00:00"
    store.saveSession(first)

    store.refreshSessionLease(first, now="2099-01-01T00:01:00+00:00")

    refreshed = store.getSession(first.session_id)
    assert refreshed.updated_at == "2099-01-01T00:01:00+00:00"
    assert refreshed.expires_at == "2099-01-01T00:02:00+00:00"

    active = store.listActiveSessionsForIp("127.0.0.1")
    assert [record.session_id for record in active] == [first.session_id]
    assert store.listActiveSessionsForIp("127.0.0.2")[0].session_id == third.session_id


def test_past_due_sessions_are_not_treated_as_active(store):
    """Test that expired session are not considered when running through the workflow"""
    # Create a stale session
    session = store.createSession(
        ephemeral_aid="E-stale",
        account_aid="A-stale",
        account_alias="stale",
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
    store.saveSession(session)

    # Assert that workflow does not consider it active/valid
    assert store.findActiveSessionForEphemeral("E-stale") is None
    assert store.listActiveSessionsForIp("127.0.0.1") == []
    assert store.listActiveSessionsForAlias("stale") == []


def test_refreshAccountLease_extends_future_expiry_but_not_past_due_accounts(tmp_path):
    """Test refresh lease for valid accounts but an expired account's lease is not refreshed"""
    lease_store = Store(
        str(tmp_path / "account-lease" / "kf-boot"),
        session_ttl_seconds=60,
        account_ttl_seconds=120,
    )
    try:
        account = lease_store.buildAccount(
            account_aid="A-lease",
            account_alias="lease",
            witness_profile_code="1-of-1",
            witness_count=1,
            toad=1,
            watcher_required=True,
            region_id="test-region",
            region_name="Test Region",
            session_id="SESSION1",
            witness_eids=[],
            watcher_eid="",
            tier="trial",
            onboarded=True,
        )

        # Set the account expiry date
        account.expires_at = "2024-01-01T00:05:00+00:00"
        lease_store.saveAccount(account)

        # Attempt to refresh the account lease earlier, before its due date
        lease_store.refreshAccountLease(account, now="2024-01-01T00:00:00+00:00")

        # Assert its lease was refreshed to a new date which is now + account TTL
        refreshed = lease_store.getAccount("A-lease")
        assert refreshed is not None
        assert refreshed.expires_at == "2024-01-01T00:02:00+00:00"

        # Simulate a past due account 
        refreshed.expires_at = "2024-01-01T00:00:00+00:00"
        lease_store.saveAccount(refreshed)

        # Attempt to refresh the account lease after the due date
        lease_store.refreshAccountLease(refreshed, now="2024-01-01T00:01:00+00:00")
        
        # Assert that the account lease is NOT refreshed 
        preserved = lease_store.getAccount("A-lease")
        assert preserved is not None
        assert preserved.expires_at == "2024-01-01T00:00:00+00:00"
    finally:
        lease_store.close()


def test_closed_sessions_transition_from_cleanup_to_delete_tasks(tmp_path):
    """Test that session transitions from cleanup state to delete tasks correctly"""
    lease_store = Store(
        str(tmp_path / "session-cleanup" / "kf-boot"),
        session_ttl_seconds=60,
        closed_session_retention_seconds=90,
    )
    try:
        session = lease_store.createSession(
            ephemeral_aid="E-close",
            account_aid="A-close",
            account_alias="close",
            chosen_profile_code="1-of-1",
            client_ip="127.0.0.1",
            region_id="test-region",
            region_name="Test Region",
            watcher_required=True,
            witness_count=1,
            toad=1,
            account_tier="trial",
        )
        # Set session state as failed and save it to trigger sync of tasks
        session.state = SESSION_STATE_FAILED
        session.updated_at = "2024-01-01T00:00:00+00:00"
        lease_store.saveSession(session)
        
        # Assert session clean up task scheduled for that session
        cleanup_task = lease_store.getCleanupTask(CLEANUP_TASK_SESSION_CLEANUP, session.session_id)
        assert cleanup_task is not None
        assert lease_store.getCleanupTask(CLEANUP_TASK_SESSION_EXPIRE, session.session_id) is None
        assert lease_store.getCleanupTask(CLEANUP_TASK_SESSION_DELETE, session.session_id) is None

        # Set the session as cleaned up of resources and save it to trigger sync of tasks
        session.resources_cleaned_at = "2024-01-01T00:01:00+00:00"
        session.updated_at = session.resources_cleaned_at
        lease_store.saveSession(session)

        # Assert session delete task scheduled for that session
        delete_task = lease_store.getCleanupTask(CLEANUP_TASK_SESSION_DELETE, session.session_id)
        assert delete_task is not None
        assert delete_task.due_at == "2024-01-01T00:02:30+00:00"
    finally:
        lease_store.close()


def test_cleanup_backlog_snapshot_reports_due_work(store):
    session = store.createSession(
        ephemeral_aid="E-backlog",
        account_aid="A-backlog",
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
    session.expires_at = "2024-01-01T00:00:00+00:00"
    store.saveSession(session)

    snapshot = store.cleanupBacklogSnapshot(now="2024-01-01T00:00:10+00:00")

    assert snapshot["pending_tasks"] == 1
    assert snapshot["due_tasks"] == 1
    assert snapshot["oldest_due_at"] == "2024-01-01T00:00:00+00:00"
    assert snapshot["oldest_due_age_seconds"] == 10.0


def test_failed_pending_account_defers_cleanup_to_linked_session(tmp_path):
    lease_store = Store(
        str(tmp_path / "failed-pending-account" / "kf-boot"),
        session_ttl_seconds=60,
        closed_session_retention_seconds=90,
    )
    try:
        session = lease_store.createSession(
            ephemeral_aid="E-failed",
            account_aid="A-failed",
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
        # Fail the session
        session.state = SESSION_STATE_FAILED
        session.updated_at = "2024-01-01T00:00:00+00:00"
        lease_store.saveSession(session)

        account = lease_store.buildAccount(
            account_aid="A-failed",
            account_alias="alpha",
            witness_profile_code="1-of-1",
            witness_count=1,
            toad=1,
            watcher_required=True,
            region_id="test-region",
            region_name="Test Region",
            session_id=session.session_id,
            witness_eids=[],
            watcher_eid="",
            tier="trial",
            onboarded=False,
        )
        # Fail the account
        account.status = ACCOUNT_STATE_FAILED
        lease_store.saveAccount(account)

        # The failed session should own teardown until it records cleanup, so the
        # linked failed account must not schedule a second account_cleanup task.
        assert lease_store.getCleanupTask(CLEANUP_TASK_SESSION_CLEANUP, session.session_id) is not None
        assert lease_store.getCleanupTask(CLEANUP_TASK_ACCOUNT_CLEANUP, account.account_aid) is None

        # Assert session work
        session.resources_cleaned_at = "2024-01-01T00:01:00+00:00"
        session.updated_at = session.resources_cleaned_at
        lease_store.saveSession(session)
        account.resources_cleaned_at = session.resources_cleaned_at
        account.session_id = ""
        lease_store.saveAccount(account)

        assert lease_store.getCleanupTask(CLEANUP_TASK_ACCOUNT_CLEANUP, account.account_aid) is None
        assert lease_store.getCleanupTask(CLEANUP_TASK_ACCOUNT_DELETE, account.account_aid) is not None
    finally:
        lease_store.close()


def test_resource_binding_listing_and_api_payloads(store):
    session = store.createSession(
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

    witness_older = makeRecord(
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
    witness_newer = makeRecord(
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
    watcher = makeRecord(
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

    store.addResource(witness_older)
    store.addResource(witness_newer)
    store.addResource(watcher)
    session.witness_eids = ["W1", "W2"]
    session.watcher_eid = "WA1"
    store.saveSession(session)

    store.bindResourcesToAccount(session=session, account_aid="A1")

    assert store.getResource("witness", "W1").principal == "A1"
    assert store.getResource("witness", "W1").cid == "A1"
    assert store.getResource("witness", "W2").cid == "A1"
    assert store.getResource("watcher", "WA1").principal == "A1"
    assert store.getResource("watcher", "WA1").cid == "A1"

    ordered = store.listResourcesForAccount(kind="witness", account_aid="A1")
    assert [record.eid for record in ordered] == ["W2", "W1"]
    session_rows = store.listResourcesForSession(kind="watcher", session_id=session.session_id)
    assert [record.eid for record in session_rows] == ["WA1"]

    api_rows = resourcesToApi([witness_newer, watcher])
    assert api_rows[0]["witness_url"] == "https://witness.example:5632"
    assert "backend_id" not in api_rows[0]
    assert "boot_url" not in api_rows[0]
    assert "principal" not in api_rows[0]
    assert "session_id" not in api_rows[0]
    assert api_rows[1]["watcher_url"] == "https://watcher.example"
    assert "boot_url" not in api_rows[1]
    assert api_rows[1]["status"] == "created"

    onboarding_rows = resourcesToApi([witness_newer, watcher], include_boot_url=True)
    assert onboarding_rows[0]["boot_url"] == "http://boot.local/witnesses"
    assert onboarding_rows[1]["boot_url"] == "http://boot.local/watchers"

    assert store.countResources("witness") == 2
    assert [record.eid for record in store.getResources("witness", ["W2", "missing", "W1"])] == ["W2", "W1"]
    store.deleteResource("witness", "W1")
    assert store.countResources("witness") == 1
    assert store.getResource("witness", "W1") is None


def test_helper_functions_cover_parse_urls_bindings_and_failure_transitions(store):
    assert parsePublicUrl("https://witness.example:5632") == ("witness.example", 5632)
    assert parsePublicUrl("https://watcher.example") == ("watcher.example", None)

    record = makeRecord(
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

    store.addBinding("principal-1", "cid-1")
    binding = store.baser.bindings.get(keys=("principal-1", "cid-1"))
    assert binding.principal == "principal-1"
    assert binding.cid == "cid-1"

    session = SessionRecord(session_id="sess_1")
    before = datetime.now(UTC)
    failed_session = sessionFailed(session, "boom")
    after = datetime.now(UTC)
    assert failed_session.state == "failed"
    assert failed_session.failure_reason == "boom"
    assert before <= datetime.fromisoformat(failed_session.updated_at) <= after + timedelta(seconds=1)

    account = store.buildAccount(
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
    assert accountFailed(None) is None
    assert accountFailed(account).status == ACCOUNT_STATE_FAILED
