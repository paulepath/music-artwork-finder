"""Source interface + the shared candidate DTO."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from ..matching import GroupMeta, ReleaseMeta


@dataclass
class CandidateData:
    source: str
    image_url: str
    release: ReleaseMeta
    provenance_url: str = ""
    width: int | None = None
    height: int | None = None
    extra: dict = field(default_factory=dict)


class Source(Protocol):
    name: str

    async def find(self, client: httpx.AsyncClient, group: GroupMeta) -> list[CandidateData]:
        ...


# Order matters: earlier = more trusted. Google is deliberately NOT here; it is
# only ever run on explicit per-album opt-in via google_images.GoogleImagesSource.
TRUSTED_SOURCES = ("musicbrainz", "itunes", "deezer")
