from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(f"KF_BOOT_{name}")
    if value is not None:
        return value

    if default is None:
        raise KeyError(f"Missing environment variable KF_BOOT_{name}")

    return default


def _split_str_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_bool(value: str, default: bool) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_url(value: str) -> str:
    return value.rstrip("/")


def _account_option(code: str) -> dict[str, int | str]:
    parts = code.lower().split("-of-")
    if len(parts) != 2:
        return {"code": code, "witness_count": 0, "toad": 0}

    try:
        toad = int(parts[0])
        witness_count = int(parts[1])
    except ValueError:
        return {"code": code, "witness_count": 0, "toad": 0}

    return {
        "code": code,
        "witness_count": witness_count,
        "toad": toad,
    }


FROZEN_ACCOUNT_OPTIONS = {
    "1-of-1": (1, 1),
    "3-of-4": (4, 3),
}


def _supported_account_options(codes: tuple[str, ...], *, witness_backend_count: int) -> tuple[str, ...]:
    supported: list[str] = []
    seen: set[str] = set()
    for code in codes:
        normalized = (code or "").strip().lower()
        if normalized not in FROZEN_ACCOUNT_OPTIONS:
            continue
        if normalized in seen:
            continue
        witness_count, _ = FROZEN_ACCOUNT_OPTIONS[normalized]
        if witness_count <= witness_backend_count:
            supported.append(normalized)
            seen.add(normalized)
    return tuple(supported)


@dataclass(frozen=True)
class WitnessBackend:
    id: str
    boot_url: str
    public_url: str


def _parse_witness_backends(value: str) -> tuple[WitnessBackend, ...]:
    backends: list[WitnessBackend] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        parts = [part.strip() for part in item.split("|")]
        if len(parts) != 3 or not all(parts):
            raise ValueError(
                "KF_BOOT_WITNESS_BACKENDS entries must be formatted as '<id>|<boot_url>|<public_url>'"
            )
        backends.append(
            WitnessBackend(
                id=parts[0],
                boot_url=_normalize_url(parts[1]),
                public_url=_normalize_url(parts[2]),
            )
        )
    return tuple(backends)


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: str
    keri_dir: str | None
    keri_name: str
    boot_hab_name: str
    onboarding_path: str
    account_path: str
    onboarding_public_url: str
    account_public_url: str
    region_id: str
    region_name: str
    witness_limit: int
    watcher_limit: int
    wit_boot_url: str
    wit_public_url: str
    wat_boot_url: str
    wat_public_url: str
    bootstrap_account_options: tuple[str, ...]
    bootstrap_watcher_required: bool
    bootstrap_accounts_per_ip: int
    bootstrap_aids_per_ip: int
    session_ttl_seconds: int
    witness_backends: tuple[WitnessBackend, ...] = ()

    def __post_init__(self) -> None:
        backends = tuple(self.witness_backends)
        if not backends:
            if not self.wit_boot_url or not self.wit_public_url:
                raise ValueError("At least one witness backend must be configured.")
            backends = (
                WitnessBackend(
                    id="wit-1",
                    boot_url=_normalize_url(self.wit_boot_url),
                    public_url=_normalize_url(self.wit_public_url),
                ),
            )

        normalized: list[WitnessBackend] = []
        seen_ids: set[str] = set()
        seen_boot_urls: set[str] = set()
        for backend in backends:
            backend_id = backend.id.strip()
            boot_url = _normalize_url(backend.boot_url)
            public_url = _normalize_url(backend.public_url)
            if not backend_id or not boot_url or not public_url:
                raise ValueError("Witness backend id, boot_url, and public_url are required.")
            if backend_id in seen_ids:
                raise ValueError(f"Duplicate witness backend id '{backend_id}'.")
            if boot_url in seen_boot_urls:
                raise ValueError(f"Duplicate witness backend boot_url '{boot_url}'.")
            seen_ids.add(backend_id)
            seen_boot_urls.add(boot_url)
            normalized.append(
                WitnessBackend(
                    id=backend_id,
                    boot_url=boot_url,
                    public_url=public_url,
                )
            )

        supported_options = _supported_account_options(
            tuple(self.bootstrap_account_options),
            witness_backend_count=len(normalized),
        )
        if not supported_options:
            raise ValueError("No bootstrap account options are supported by the configured witness backends.")

        object.__setattr__(self, "witness_backends", tuple(normalized))
        object.__setattr__(self, "wit_boot_url", normalized[0].boot_url)
        object.__setattr__(self, "wit_public_url", normalized[0].public_url)
        object.__setattr__(self, "bootstrap_account_options", supported_options)

    def account_option(self, code: str) -> dict[str, int | str] | None:
        target = (code or "").strip().lower()
        for item in self.bootstrap_account_options:
            option = _account_option(item)
            if option["code"].lower() == target:
                return option
        return None

    @property
    def onboarding_surface(self) -> dict[str, str]:
        return {"path": self.onboarding_path, "url": self.onboarding_public_url}

    @property
    def account_surface(self) -> dict[str, str]:
        return {"path": self.account_path, "url": self.account_public_url}

    @classmethod
    def from_env(cls) -> "Config":
        host = _env("HOST", "127.0.0.1")
        port = int(_env("PORT", "9723"))
        onboarding_path = _env("ONBOARDING_PATH", "/onboarding").rstrip("/") or "/onboarding"
        account_path = _env("ACCOUNT_PATH", "/account").rstrip("/") or "/account"

        onboarding_public_url = _env("ONBOARDING_PUBLIC_URL", f"http://{host}:{port}{onboarding_path}")
        account_public_url = _env("ACCOUNT_PUBLIC_URL", f"http://{host}:{port}{account_path}")

        witness_backends_env = os.environ.get("KF_BOOT_WITNESS_BACKENDS")
        witness_backends = _parse_witness_backends(witness_backends_env or "")
        if witness_backends:
            wit_boot_url = witness_backends[0].boot_url
            wit_public_url = witness_backends[0].public_url
        else:
            wit_boot_url = _env("WIT_BOOT_URL")
            wit_public_url = _env("WIT_PUBLIC_URL")
        wat_boot_url = _env("WAT_BOOT_URL")
        wat_public_url = _env("WAT_PUBLIC_URL")

        return cls(
            host=host,
            port=port,
            db_path=_env("DB_PATH", "./var/kf-boot"),
            keri_dir=os.environ.get("KF_BOOT_KERI_DIR"),
            keri_name=_env("KERI_NAME", "kf-boot"),
            boot_hab_name=_env("BOOT_HAB_NAME", "boot-server"),
            onboarding_path=onboarding_path,
            account_path=account_path,
            onboarding_public_url=onboarding_public_url.rstrip("/"),
            account_public_url=account_public_url.rstrip("/"),
            region_id=_env("REGION_ID", "nyc"),
            region_name=_env("REGION_NAME", "New York"),
            witness_limit=int(_env("WITNESS_LIMIT", "200")),
            watcher_limit=int(_env("WATCHER_LIMIT", "200")),
            wit_boot_url=_normalize_url(wit_boot_url),
            wit_public_url=_normalize_url(wit_public_url),
            wat_boot_url=_normalize_url(wat_boot_url),
            wat_public_url=_normalize_url(wat_public_url),
            bootstrap_account_options=_split_str_csv(
                _env("BOOTSTRAP_ACCOUNT_OPTIONS", "1-of-1,3-of-4")
            ),
            bootstrap_watcher_required=_parse_bool(
                _env("BOOTSTRAP_WATCHER_REQUIRED", "true"),
                True,
            ),
            bootstrap_accounts_per_ip=int(_env("BOOTSTRAP_ACCOUNTS_PER_IP", "1")),
            bootstrap_aids_per_ip=int(_env("BOOTSTRAP_AIDS_PER_IP", "10")),
            session_ttl_seconds=int(_env("SESSION_TTL_SECONDS", "300")),
            witness_backends=witness_backends,
        )
