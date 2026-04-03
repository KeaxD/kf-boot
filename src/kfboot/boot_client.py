from __future__ import annotations

from dataclasses import dataclass

import requests


class BootError(RuntimeError):
    """Raised when a boot API call fails."""

    def __init__(self, description: str, status_code: int | None = None):
        super().__init__(description)
        self.status_code = status_code


@dataclass(frozen=True)
class BootClient:
    base_url: str
    timeout: int = 10

    def create_witness(self, cid: str) -> dict:
        return self._json("POST", "/witnesses", json={"aid": cid})

    def delete_witness(self, eid: str) -> None:
        self._empty("DELETE", f"/witnesses/{eid}")

    def create_watcher(self, cid: str, oobi: str | None = None) -> dict:
        payload = {"aid": cid}
        if oobi:
            payload["oobi"] = oobi
        return self._json("POST", "/watchers", json=payload)

    def delete_watcher(self, eid: str) -> None:
        self._empty("DELETE", f"/watchers/{eid}")

    def watcher_status(self, eid: str) -> dict:
        return self._json("GET", f"/watchers/{eid}/status")

    def _json(self, method: str, path: str, json: dict | None = None) -> dict:
        response = self._request(method, path, json=json)
        try:
            return response.json()
        except ValueError as exc:
            raise BootError(
                f"Invalid JSON from boot API: {exc}",
                status_code=response.status_code,
            ) from exc

    def _empty(self, method: str, path: str) -> None:
        self._request(method, path)

    def _request(
        self, method: str, path: str, json: dict | None = None
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method,
                url,
                json=json,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise BootError(f"Boot API request failed: {exc}") from exc

        if response.status_code >= 400:
            description = response.text.strip() or f"HTTP {response.status_code}"
            raise BootError(description, status_code=response.status_code)

        return response
