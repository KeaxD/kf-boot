from __future__ import annotations

import pytest

import kfboot.boot_client as boot_client_module
from kfboot.boot_client import BootClient, BootError


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


def test_allocate_methods_send_expected_requests(monkeypatch):
    requests = RecordingRequests(
        responses=[
            FakeResponse(json_data={"eid": "W1"}),
            FakeResponse(json_data={"eid": "WA1"}),
        ]
    )
    monkeypatch.setattr(boot_client_module, "requests", requests)

    client = BootClient("http://boot.local", timeout=7)

    assert client.allocate_witness("AID1") == {"eid": "W1"}
    assert client.allocate_watcher("AID1", oobi="oobi://watcher") == {"eid": "WA1"}
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

    assert client.create_witness("AID1") == {"eid": "W1"}
    assert client.create_watcher("AID1") == {"eid": "WA1"}
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


def test_create_watcher_omits_oobi_when_not_provided(monkeypatch):
    requests = RecordingRequests(responses=[FakeResponse(json_data={"eid": "WA1"})])
    monkeypatch.setattr(boot_client_module, "requests", requests)

    client = BootClient("http://boot.local")
    client.create_watcher("AID1")

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

    assert client.delete_witness("W1") is None
    assert client.delete_watcher("WA1") is None
    assert client.watcher_status("WA1") == {
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
        BootClient("http://boot.local").create_witness("AID1")

    assert str(excinfo.value) == "already exists"
    assert excinfo.value.status_code == 409


def test_http_error_without_body_uses_status_fallback(monkeypatch):
    requests = RecordingRequests(responses=[FakeResponse(status_code=503, text="   ")])
    monkeypatch.setattr(boot_client_module, "requests", requests)

    with pytest.raises(BootError) as excinfo:
        BootClient("http://boot.local").delete_watcher("WA1")

    assert str(excinfo.value) == "HTTP 503"
    assert excinfo.value.status_code == 503


def test_invalid_json_response_raises_boot_error_with_status(monkeypatch):
    requests = RecordingRequests(
        responses=[FakeResponse(status_code=200, json_error=ValueError("bad json"))]
    )
    monkeypatch.setattr(boot_client_module, "requests", requests)

    with pytest.raises(BootError) as excinfo:
        BootClient("http://boot.local").watcher_status("WA1")

    assert "Invalid JSON from boot API" in str(excinfo.value)
    assert excinfo.value.status_code == 200


def test_request_exception_raises_boot_error(monkeypatch):
    requests = RecordingRequests(error=RecordingRequests.RequestException("boom"))
    monkeypatch.setattr(boot_client_module, "requests", requests)

    with pytest.raises(BootError) as excinfo:
        BootClient("http://boot.local").create_watcher("AID1")

    assert str(excinfo.value) == "Boot API request failed: boom"
    assert excinfo.value.status_code is None


def test_missing_requests_dependency_raises_boot_error(monkeypatch):
    monkeypatch.setattr(boot_client_module, "requests", None)

    with pytest.raises(BootError) as excinfo:
        BootClient("http://boot.local").create_witness("AID1")

    assert str(excinfo.value) == "requests is required to call the downstream boot API"
    assert excinfo.value.status_code is None
