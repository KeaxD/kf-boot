from __future__ import annotations

import pytest

from kfboot.config import Config, WitnessBackend

from .support import make_config, make_witness_backends


def test_config_filters_profiles_to_available_witness_backends(tmp_path):
    config = make_config(tmp_path, witness_backends=make_witness_backends(1))

    assert config.bootstrap_account_options == ("1-of-1",)
    assert config.account_option("1-of-1") == {"code": "1-of-1", "witness_count": 1, "toad": 1}
    assert config.account_option("3-of-4") is None


def test_config_filters_out_profiles_outside_the_frozen_contract(tmp_path):
    config = make_config(
        tmp_path,
        witness_backends=make_witness_backends(4),
        bootstrap_account_options=("2-of-2", "1-of-1", "3-of-4", "1-of-4"),
    )

    assert config.bootstrap_account_options == ("1-of-1", "3-of-4")
    assert config.account_option("2-of-2") is None
    assert config.account_option("1-of-4") is None


def test_config_rejects_when_no_profile_matches_configured_backends(tmp_path):
    with pytest.raises(ValueError, match="No bootstrap account options"):
        make_config(
            tmp_path,
            witness_backends=make_witness_backends(1),
            bootstrap_account_options=("3-of-4",),
        )


def test_config_from_env_parses_witness_backend_pool(monkeypatch):
    monkeypatch.setenv(
        "KF_BOOT_WITNESS_BACKENDS",
        (
            "wit-1|http://127.0.0.1:5631|https://boot.example.com:5632,"
            "wit-2|http://127.0.0.1:5641|https://boot.example.com:5642,"
            "wit-3|http://127.0.0.1:5651|https://boot.example.com:5652,"
            "wit-4|http://127.0.0.1:5661|https://boot.example.com:5662"
        ),
    )
    monkeypatch.delenv("KF_BOOT_WIT_BOOT_URL", raising=False)
    monkeypatch.delenv("KF_BOOT_WIT_PUBLIC_URL", raising=False)
    monkeypatch.setenv("KF_BOOT_WAT_BOOT_URL", "http://boot.local/watchers")
    monkeypatch.setenv("KF_BOOT_WAT_PUBLIC_URL", "https://watcher.example")

    config = Config.from_env()

    assert [backend.id for backend in config.witness_backends] == ["wit-1", "wit-2", "wit-3", "wit-4"]
    assert config.keri_dir is None
    assert config.wit_boot_url == "http://127.0.0.1:5631"
    assert config.wit_public_url == "https://boot.example.com:5632"
    assert config.bootstrap_account_options == ("1-of-1", "3-of-4")


def test_config_from_env_rejects_malformed_witness_backend_entry(monkeypatch):
    monkeypatch.setenv("KF_BOOT_WITNESS_BACKENDS", "wit-1|http://127.0.0.1:5631")

    with pytest.raises(ValueError, match="formatted as"):
        Config.from_env()


@pytest.mark.parametrize(
    ("witness_backends", "message"),
    [
        (
            (
                WitnessBackend(
                    id="wit-1",
                    boot_url="http://127.0.0.1:5631",
                    public_url="https://boot.example.com:5632",
                ),
                WitnessBackend(
                    id="wit-1",
                    boot_url="http://127.0.0.1:5641",
                    public_url="https://boot.example.com:5642",
                ),
            ),
            "Duplicate witness backend id",
        ),
        (
            (
                WitnessBackend(
                    id="wit-1",
                    boot_url="http://127.0.0.1:5631",
                    public_url="https://boot.example.com:5632",
                ),
                WitnessBackend(
                    id="wit-2",
                    boot_url="http://127.0.0.1:5631",
                    public_url="https://boot.example.com:5642",
                ),
            ),
            "Duplicate witness backend boot_url",
        ),
    ],
)
def test_config_rejects_duplicate_witness_backend_identity(tmp_path, witness_backends, message):
    with pytest.raises(ValueError, match=message):
        make_config(tmp_path, witness_backends=witness_backends)
