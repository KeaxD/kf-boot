from __future__ import annotations

from dataclasses import dataclass

from keri import help

try:
    import requests
except ImportError:  # pragma: no cover - exercised only in thin local test envs
    requests = None

logger = help.ogler.getLogger(__name__)

class BootError(RuntimeError):
    """Raised when a boot API call fails."""

    def __init__(self, description: str, status_code: int | None = None):
        super().__init__(description)
        self.status_code = status_code


@dataclass(frozen=True)
class BootClient:
    base_url: str
    timeout: float = 10.0

    def allocateWitness(self, account_aid: str) -> dict:
        """Allocate a hosted witness for the permanent account AID."""

        return self._json("POST", "/witnesses", json={"aid": account_aid})

    def createWitness(self, cid: str) -> dict:
        return self.allocateWitness(cid)

    def deleteWitness(self, eid: str) -> None:
        self._empty("DELETE", f"/witnesses/{eid}")

    def allocateWatcher(self, account_aid: str, oobi: str | None = None) -> dict:
        """Allocate a hosted watcher for the permanent account AID."""

        payload = {"aid": account_aid}
        if oobi:
            payload["oobi"] = oobi
        return self._json("POST", "/watchers", json=payload)

    def createWatcher(self, cid: str, oobi: str | None = None) -> dict:
        return self.allocateWatcher(cid, oobi=oobi)

    def deleteWatcher(self, eid: str) -> None:
        self._empty("DELETE", f"/watchers/{eid}")

    def watcherStatus(self, eid: str) -> dict:
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
    ):
        if requests is None:
            logger.error(
                "Boot API client cannot make request because requests library is not available",
            )
            raise BootError("requests is required to call the downstream boot API")
        url = f"{self.base_url}{path}"
        logger.debug(
            "Boot API request starting",
        )
        try:
            response = requests.request(
                method,
                url,
                json=json,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.warning(
                f"BOOT API request failed due to request exception error: `{exc}`",
            )
            raise BootError(f"Boot API request failed: {exc}") from exc

        if response.status_code >= 400:
            description = response.text.strip() or f"HTTP {response.status_code}"
            logger.warning(
                "BOOT API request failed: "
                f"method={method} url={url} status={response.status_code} "
                f"body={description!r}",
            )
            raise BootError(description, status_code=response.status_code)

        logger.info(
            f"BOOT API request succeeded: `{response}`",
        )
        return response
