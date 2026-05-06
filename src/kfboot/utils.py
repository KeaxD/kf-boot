# utils.py
import falcon

def _payload(serder) -> dict[str, Any]:
    payload = serder.ked.get("a", {})
    return payload if isinstance(payload, dict) else {}


def _optional_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = _optional_str(payload, key)
    if value:
        return value
    raise falcon.HTTPBadRequest(
        title="Invalid request payload",
        description=f"{key} is required.",
    )

def _boot_error(exc: BootError) -> falcon.HTTPError:
    if exc.status_code == 400:
        return falcon.HTTPBadRequest(
            title="Boot API rejected request",
            description=str(exc),
        )
    if exc.status_code == 404:
        return falcon.HTTPNotFound(
            title="Upstream resource not found",
            description=str(exc),
        )
    if exc.status_code == 409:
        return falcon.HTTPConflict(
            title="Boot API conflict",
            description=str(exc),
        )
    return falcon.HTTPBadGateway(
        title="Boot API call failed",
        description=str(exc),
    )