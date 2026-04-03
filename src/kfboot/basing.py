# -*- encoding: utf-8 -*-
"""
kfboot.basing module

LMDB storage for KF platform resource metadata and controller bindings.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from keri.db import dbing, koming


@dataclass(frozen=True)
class ResourceRecord:
    kind: str = ""
    eid: str = ""
    cid: str = ""
    principal: str = ""
    name: str = ""
    identifier_alias: str = ""
    region_id: str = ""
    region_name: str = ""
    url: str = ""
    public_host: str = ""
    public_port: int | None = None
    oobis: list[str] | None = None
    created_at: str = ""


@dataclass(frozen=True)
class BindingRecord:
    principal: str = ""
    cid: str = ""


class PlatformBaser(dbing.LMDBer):
    """LMDB database for the KF platform service."""

    TailDirPath = ""
    AltTailDirPath = ".kf-boot"
    TempPrefix = "kf_platform_"

    def __init__(self, name="platform", headDirPath=None, reopen=True, **kwa):
        self.resources = None
        self.bindings = None

        super(PlatformBaser, self).__init__(
            name=name,
            headDirPath=headDirPath,
            reopen=reopen,
            **kwa,
        )

    def reopen(self, **kwa):
        super(PlatformBaser, self).reopen(**kwa)

        self.resources = koming.Komer(
            db=self,
            subkey="resc.",
            klas=ResourceRecord,
        )
        self.bindings = koming.Komer(
            db=self,
            subkey="bind.",
            klas=BindingRecord,
        )

        return self.env


def open_baser(db_path: str) -> PlatformBaser:
    path = Path(db_path).expanduser()
    name = path.stem if path.suffix else path.name
    head = path.parent if path.parent != Path("") else Path(".")

    return PlatformBaser(
        name=name or "platform",
        headDirPath=str(head),
        reopen=True,
    )
