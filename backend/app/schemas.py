"""Pydantic response/request models for the API."""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TrackOut(ORM):
    id: int
    path: str
    title: str
    track_no: int | None
    duration_s: float | None
    has_embedded_art: bool


class LibraryTrackOut(TrackOut):
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    disc: int = 1
    year: int | None = None
    file_format: str = ""
    file_size: int = 0


class CandidateOut(ORM):
    id: int
    source: str
    tier: str
    confidence: float
    reason: str
    image_url: str
    provenance_url: str
    width: int | None
    height: int | None
    cluster: int | None
    cluster_size: int
    created_at: dt.datetime


class AuditOut(ORM):
    id: int
    group_id: int
    action: str
    source: str
    tier: str | None
    image_url: str
    image_sha256: str
    tracks_written: int
    wrote_cover_jpg: bool
    ok: bool
    error: str
    created_at: dt.datetime
    undone_at: dt.datetime | None


class AlbumSummary(ORM):
    id: int
    album_artist: str
    album: str
    disc: int
    year: int | None
    track_count: int
    has_embedded_art: bool
    has_folder_art: bool
    state: str
    google_enabled: bool
    is_compilation: bool
    common_dir: str
    art_dupe_albums: int = 0
    updated_at: dt.datetime
    candidate_count: int = 0
    best_tier: str | None = None


class AlbumDetail(AlbumSummary):
    musicbrainz_albumid: str | None
    tracks: list[TrackOut] = []
    candidates: list[CandidateOut] = []
    audit: list[AuditOut] = []


class JobOut(ORM):
    id: int
    kind: str
    status: str
    group_id: int | None
    progress: float
    message: str
    created_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None


class ApplyRequest(BaseModel):
    candidate_id: int
    approved: bool = False
    write_cover_jpg: bool = False


class StatsOut(BaseModel):
    groups_total: int
    by_state: dict[str, int]
    missing_art: int
    last_scan: dt.datetime | None
    active_jobs: int


class SearchMetadataOverride(BaseModel):
    track_id: int
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: int | None = None


class SearchSelectionRequest(BaseModel):
    track_ids: list[int] = Field(default_factory=list)
    folder_path: str | None = None
    recursive: bool = True
    include_existing: bool = True
    confirm_large: bool = False
    overrides: list[SearchMetadataOverride] = Field(default_factory=list)


class SelectionUpdate(BaseModel):
    album_cluster_id: int | None = None
    track_cluster_id: int | None = None
    album_approved: bool = False
    track_approved: bool = False


class ApproveRecommendedRequest(BaseModel):
    minimum_confidence: str = "medium"
