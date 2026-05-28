from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hio.base import tyming
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


@dataclass(frozen=True)
class HioBootClient:
    base_url: str
    clienter: Any
    timeout: float = 10.0

    def deleteWitnessDo(self, eid: str, *, tymth, tock: float = 0.0):
        yield from self._emptyDo("DELETE", f"/witnesses/{eid}", tymth=tymth, tock=tock)

    def deleteWatcherDo(self, eid: str, *, tymth, tock: float = 0.0):
        yield from self._emptyDo("DELETE", f"/watchers/{eid}", tymth=tymth, tock=tock)

    def _emptyDo(self, method: str, path: str, *, tymth, tock: float = 0.0):
        yield from self._requestDo(method, path, tymth=tymth, tock=tock)

    def _requestDo(self, method: str, path: str, *, tymth, tock: float = 0.0):
        url = f"{self.base_url}{path}"
        try:
            client = self.clienter.request(method, url)
        except Exception as exc:
            logger.warning(
                f"BOOT API request failed due to HIO client exception: `{exc}`",
            )
            raise BootError(f"Boot API request failed: {exc}") from exc
        if client is None:
            raise BootError("Boot API request failed to create HIO client")

        tymer = tyming.Tymer(tymth=tymth, duration=self.timeout)
        try:
            while not client.responses and not tymer.expired:
                yield tock

            if not client.responses:
                raise BootError("Boot API request timed out")

            response = client.respond() if hasattr(client, "respond") else client.responses.popleft()
            status = _responseStatus(response)
            if status >= 400:
                description = _responseBodyText(response) or f"HTTP {status}"
                logger.warning(
                    "BOOT API request failed: "
                    f"method={method} url={url} status={status} "
                    f"body={description!r}",
                )
                raise BootError(description, status_code=status)

            logger.info(
                f"BOOT API request succeeded: `{response}`",
            )
            return response
        finally:
            self.clienter.remove(client)


def _responseStatus(response: Any) -> int:
    status = response.get("status") if isinstance(response, dict) else getattr(response, "status", None)
    if status is None:
        raise BootError("Boot API response missing status")
    try:
        status = int(status)
    except (TypeError, ValueError) as exc:
        raise BootError(f"Boot API response has invalid status: {status!r}") from exc
    if status <= 0:
        raise BootError(f"Boot API response has invalid status: {status}")
    return status


def _responseBodyText(response: Any) -> str:
    body = response.get("body", b"") if isinstance(response, dict) else getattr(response, "body", b"")
    if isinstance(body, memoryview):
        body = bytes(body)
    if isinstance(body, bytearray):
        body = bytes(body)
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace").strip()
    return str(body or "").strip()
