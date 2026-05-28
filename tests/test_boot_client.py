from __future__ import annotations

from collections import deque

import pytest

import kfboot.boot_client as boot_client_module
from kfboot.boot_client import BootClient, BootError, HioBootClient
from kfboot.utils import bootErrorToHTTP


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        json_data=None,
        json_error: Exception | None = None,
    ):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._json_data


class RecordingRequests:
    class RequestException(Exception):
        pass

    def __init__(self, responses=None, *, error: Exception | None = None):
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, json=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


class FakeHioClient:
    def __init__(self, responses=None):
        self.responses = deque(responses or [])

    def respond(self):
        if self.responses:
            return self.responses.popleft()
        return None


class RecordingHioClienter:
    def __init__(self, clients=None, *, error: Exception | None = None):
        self.clients = list(clients or [])
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.removed: list[FakeHioClient] = []

    def request(self, method, url):
        self.calls.append({"method": method, "url": url})
        if self.error is not None:
            raise self.error
        return self.clients.pop(0)

    def remove(self, client):
        self.removed.append(client)


def drain(gen, *, tick=None, max_steps: int = 20):
    for _ in range(max_steps):
        try:
            next(gen)
        except StopIteration as ex:
            return ex.value
        if tick is not None:
            tick()
    raise AssertionError("generator did not finish")


def test_allocate_methods_send_expected_requests(monkeypatch):
    requests = RecordingRequests(
        responses=[
            FakeResponse(json_data={"eid": "W1"}),
            FakeResponse(json_data={"eid": "WA1"}),
        ]
    )
    monkeypatch.setattr(boot_client_module, "requests", requests)

    client = BootClient("http://boot.local", timeout=7)

    assert client.allocateWitness("AID1") == {"eid": "W1"}
    assert client.allocateWatcher("AID1", oobi="oobi://watcher") == {"eid": "WA1"}
    assert requests.calls == [
        {
            "method": "POST",
            "url": "http://boot.local/witnesses",
            "json": {"aid": "AID1"},
            "timeout": 7,
        },
        {
            "method": "POST",
            "url": "http://boot.local/watchers",
            "json": {"aid": "AID1", "oobi": "oobi://watcher"},
            "timeout": 7,
        },
    ]


def test_legacy_create_methods_delegate_to_session_allocation_helpers(monkeypatch):
    requests = RecordingRequests(
        responses=[
            FakeResponse(json_data={"eid": "W1"}),
            FakeResponse(json_data={"eid": "WA1"}),
        ]
    )
    monkeypatch.setattr(boot_client_module, "requests", requests)

    client = BootClient("http://boot.local")

    assert client.createWitness("AID1") == {"eid": "W1"}
    assert client.createWatcher("AID1") == {"eid": "WA1"}
    assert requests.calls == [
        {
            "method": "POST",
            "url": "http://boot.local/witnesses",
            "json": {"aid": "AID1"},
            "timeout": 10,
        },
        {
            "method": "POST",
            "url": "http://boot.local/watchers",
            "json": {"aid": "AID1"},
            "timeout": 10,
        },
    ]


def test_createWatcher_omits_oobi_when_not_provided(monkeypatch):
    requests = RecordingRequests(responses=[FakeResponse(json_data={"eid": "WA1"})])
    monkeypatch.setattr(boot_client_module, "requests", requests)

    client = BootClient("http://boot.local")
    client.createWatcher("AID1")

    assert requests.calls == [
        {
            "method": "POST",
            "url": "http://boot.local/watchers",
            "json": {"aid": "AID1"},
            "timeout": 10,
        }
    ]


def test_delete_and_status_methods_send_expected_requests(monkeypatch):
    requests = RecordingRequests(
        responses=[
            FakeResponse(status_code=204),
            FakeResponse(status_code=204),
            FakeResponse(
                json_data={
                    "watcher_id": "WA1",
                    "controller_id": "AID1",
                    "summary": {"total_witnesses": 1, "responsive_witnesses": 1},
                }
            ),
        ]
    )
    monkeypatch.setattr(boot_client_module, "requests", requests)

    client = BootClient("http://boot.local")

    assert client.deleteWitness("W1") is None
    assert client.deleteWatcher("WA1") is None
    assert client.watcherStatus("WA1") == {
        "watcher_id": "WA1",
        "controller_id": "AID1",
        "summary": {"total_witnesses": 1, "responsive_witnesses": 1},
    }
    assert requests.calls == [
        {
            "method": "DELETE",
            "url": "http://boot.local/witnesses/W1",
            "json": None,
            "timeout": 10,
        },
        {
            "method": "DELETE",
            "url": "http://boot.local/watchers/WA1",
            "json": None,
            "timeout": 10,
        },
        {
            "method": "GET",
            "url": "http://boot.local/watchers/WA1/status",
            "json": None,
            "timeout": 10,
        },
    ]


def test_http_error_preserves_status_and_body(monkeypatch):
    requests = RecordingRequests(responses=[FakeResponse(status_code=409, text="already exists")])
    monkeypatch.setattr(boot_client_module, "requests", requests)

    with pytest.raises(BootError) as excinfo:
        BootClient("http://boot.local").createWitness("AID1")

    assert str(excinfo.value) == "already exists"
    assert excinfo.value.status_code == 409


def test_http_error_without_body_uses_status_fallback(monkeypatch):
    requests = RecordingRequests(responses=[FakeResponse(status_code=503, text="   ")])
    monkeypatch.setattr(boot_client_module, "requests", requests)

    with pytest.raises(BootError) as excinfo:
        BootClient("http://boot.local").deleteWatcher("WA1")

    assert str(excinfo.value) == "HTTP 503"
    assert excinfo.value.status_code == 503


def test_boot_error_to_http_preserves_downstream_service_unavailable():
    error = bootErrorToHTTP(BootError("capacity exhausted", status_code=503))

    assert error.status == "503 Service Unavailable"
    assert error.title == "Downstream service unavailable"
    assert error.description == "capacity exhausted"


def test_invalid_json_response_raisesbootError_with_status(monkeypatch):
    requests = RecordingRequests(
        responses=[FakeResponse(status_code=200, json_error=ValueError("bad json"))]
    )
    monkeypatch.setattr(boot_client_module, "requests", requests)

    with pytest.raises(BootError) as excinfo:
        BootClient("http://boot.local").watcherStatus("WA1")

    assert "Invalid JSON from boot API" in str(excinfo.value)
    assert excinfo.value.status_code == 200


def test_request_exception_raisesbootError(monkeypatch):
    requests = RecordingRequests(error=RecordingRequests.RequestException("boom"))
    monkeypatch.setattr(boot_client_module, "requests", requests)

    with pytest.raises(BootError) as excinfo:
        BootClient("http://boot.local").createWatcher("AID1")

    assert str(excinfo.value) == "Boot API request failed: boom"
    assert excinfo.value.status_code is None


def test_missing_requests_dependency_raisesbootError(monkeypatch):
    monkeypatch.setattr(boot_client_module, "requests", None)

    with pytest.raises(BootError) as excinfo:
        BootClient("http://boot.local").createWitness("AID1")

    assert str(excinfo.value) == "requests is required to call the downstream boot API"
    assert excinfo.value.status_code is None


def test_hio_delete_waits_for_response_and_removes_client():
    hio_client = FakeHioClient()
    clienter = RecordingHioClienter([hio_client])
    clock = {"tyme": 0.0}
    client = HioBootClient("http://boot.local", clienter=clienter, timeout=1.0)

    gen = client.deleteWitnessDo("W1", tymth=lambda: clock["tyme"], tock=0.0)

    assert next(gen) == 0.0
    assert clienter.calls == [{"method": "DELETE", "url": "http://boot.local/witnesses/W1"}]
    assert clienter.removed == []

    hio_client.responses.append({"status": 204, "body": b""})

    with pytest.raises(StopIteration) as done:
        next(gen)

    assert done.value.value is None
    assert clienter.removed == [hio_client]


def test_hio_delete_error_preserves_status_and_removes_client():
    hio_client = FakeHioClient([{"status": 503, "body": b"downstream unavailable"}])
    clienter = RecordingHioClienter([hio_client])
    client = HioBootClient("http://boot.local", clienter=clienter)

    with pytest.raises(BootError) as excinfo:
        drain(client.deleteWatcherDo("WA1", tymth=lambda: 0.0, tock=0.0))

    assert str(excinfo.value) == "downstream unavailable"
    assert excinfo.value.status_code == 503
    assert clienter.removed == [hio_client]


def test_hio_request_exception_raises_boot_error():
    clienter = RecordingHioClienter(error=RuntimeError("boom"))
    client = HioBootClient("http://boot.local", clienter=clienter)

    with pytest.raises(BootError) as excinfo:
        drain(client.deleteWatcherDo("WA1", tymth=lambda: 0.0, tock=0.0))

    assert str(excinfo.value) == "Boot API request failed: boom"
    assert excinfo.value.status_code is None
    assert clienter.calls == [{"method": "DELETE", "url": "http://boot.local/watchers/WA1"}]
    assert clienter.removed == []


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"body": b"missing status"}, "Boot API response missing status"),
        ({"status": "wat", "body": b"invalid status"}, "Boot API response has invalid status: 'wat'"),
        ({"status": 0, "body": b"zero status"}, "Boot API response has invalid status: 0"),
    ],
)
def test_hio_delete_malformed_status_raises_boot_error_and_removes_client(response, message):
    hio_client = FakeHioClient([response])
    clienter = RecordingHioClienter([hio_client])
    client = HioBootClient("http://boot.local", clienter=clienter)

    with pytest.raises(BootError) as excinfo:
        drain(client.deleteWatcherDo("WA1", tymth=lambda: 0.0, tock=0.0))

    assert str(excinfo.value) == message
    assert excinfo.value.status_code is None
    assert clienter.removed == [hio_client]


def test_hio_delete_timeout_removes_client():
    hio_client = FakeHioClient()
    clienter = RecordingHioClienter([hio_client])
    clock = {"tyme": 0.0}
    client = HioBootClient("http://boot.local", clienter=clienter, timeout=0.2)

    with pytest.raises(BootError) as excinfo:
        drain(
            client.deleteWitnessDo("W1", tymth=lambda: clock["tyme"], tock=0.0),
            tick=lambda: clock.__setitem__("tyme", clock["tyme"] + 0.1),
        )

    assert str(excinfo.value) == "Boot API request timed out"
    assert excinfo.value.status_code is None
    assert clienter.removed == [hio_client]
