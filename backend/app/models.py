"""SQLAlchemy ORM models: album groups, tracks, artwork candidates, jobs, audit."""
from __future__ import annotations

import datetime as dt
import enum
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class GroupState(str, enum.Enum):
    scanned = "scanned"              # freshly discovered, not yet processed
    has_art = "has_art"              # already has embedded/folder art -> left alone
    suspect_art = "suspect_art"      # has art, but the same image is on other unrelated albums
    needs_tagging = "needs_tagging"  # missing/incomplete album tags -> manual (Picard)
    multi_album_parent = "multi_album_parent"  # folder holds several albums -> excluded
    pending_review = "pending_review"          # has candidates awaiting a decision
    google_only = "google_only"      # trusted sources found nothing; Google opt-in offered
    applied = "applied"              # artwork written
    skipped = "skipped"              # user chose to skip
    error = "error"


class Tier(str, enum.Enum):
    exact = "exact"      # MBID / barcode match -> batch-appliable
    strong = "strong"    # full tag + track-count match -> apply w/ candidate shown
    fuzzy = "fuzzy"       # partial match -> explicit per-album approval only
    review = "review"     # Google image -> manual selection only


class JobKind(str, enum.Enum):
    scan = "scan"
    find_candidates = "find_candidates"
    google_search = "google_search"
    apply = "apply"
    ma_sync = "ma_sync"
    search_session = "search_session"
    apply_session = "apply_session"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class SearchStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    paused = "paused"
    review_ready = "review_ready"
    applying = "applying"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class QueryRole(str, enum.Enum):
    album = "album"
    track = "track"


class WorkStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    ready = "ready"
    applied = "applied"
    failed = "failed"
    cancelled = "cancelled"


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    files_seen: Mapped[int] = mapped_column(Integer, default=0)
    groups_total: Mapped[int] = mapped_column(Integer, default=0)
    groups_missing_art: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")


class AlbumMerge(Base):
    __tablename__ = "album_merges"
    __table_args__ = (UniqueConstraint("merge_key", name="uq_album_merge_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merge_key: Mapped[str] = mapped_column(String(512), index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    album_artist: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    groups: Mapped[list["AlbumGroup"]] = relationship(back_populates="merged_into")


class AlbumGroup(Base):
    __tablename__ = "album_groups"
    __table_args__ = (UniqueConstraint("group_key", name="uq_group_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_key: Mapped[str] = mapped_column(String(512), index=True)
    album_artist: Mapped[str] = mapped_column(String(512), default="")
    album: Mapped[str] = mapped_column(String(512), default="")
    disc: Mapped[int] = mapped_column(Integer, default=1)
    year: Mapped[Optional[int]] = mapped_column(Integer)
    is_compilation: Mapped[bool] = mapped_column(Boolean, default=False)
    musicbrainz_albumid: Mapped[Optional[str]] = mapped_column(String(64))
    musicbrainz_releasegroupid: Mapped[Optional[str]] = mapped_column(String(64))
    identified_album: Mapped[Optional[str]] = mapped_column(String(512))
    identified_artist: Mapped[Optional[str]] = mapped_column(String(512))
    identified_mbid: Mapped[Optional[str]] = mapped_column(String(64))
    identified_release_group_id: Mapped[Optional[str]] = mapped_column(String(64))

    common_dir: Mapped[str] = mapped_column(Text, default="")
    track_count: Mapped[int] = mapped_column(Integer, default=0)
    has_embedded_art: Mapped[bool] = mapped_column(Boolean, default=False)
    has_folder_art: Mapped[bool] = mapped_column(Boolean, default=False)
    art_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True)   # sha1 of current embedded cover
    art_dupe_albums: Mapped[int] = mapped_column(Integer, default=0)          # # other distinct albums sharing it

    merged_into_id: Mapped[Optional[int]] = mapped_column(ForeignKey("album_merges.id"))
    merge_dismissed: Mapped[bool] = mapped_column(Boolean, default=False)

    state: Mapped[GroupState] = mapped_column(Enum(GroupState), default=GroupState.scanned, index=True)
    google_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_scan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("scan_runs.id"))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    tracks: Mapped[list["Track"]] = relationship(back_populates="group", cascade="all, delete-orphan")
    candidates: Mapped[list["Candidate"]] = relationship(back_populates="group", cascade="all, delete-orphan")
    merged_into: Mapped[Optional[AlbumMerge]] = relationship(back_populates="groups")


class Track(Base):
    __tablename__ = "tracks"
    __table_args__ = (UniqueConstraint("path", name="uq_track_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("album_groups.id"), index=True)
    path: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(512), default="")
    artist: Mapped[str] = mapped_column(String(512), default="")
    album: Mapped[str] = mapped_column(String(512), default="")
    album_artist: Mapped[str] = mapped_column(String(512), default="")
    disc: Mapped[int] = mapped_column(Integer, default=1)
    year: Mapped[Optional[int]] = mapped_column(Integer)
    file_format: Mapped[str] = mapped_column(String(16), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    mtime_ns: Mapped[int] = mapped_column(Integer, default=0)
    track_no: Mapped[Optional[int]] = mapped_column(Integer)
    duration_s: Mapped[Optional[float]] = mapped_column(Float)
    has_embedded_art: Mapped[bool] = mapped_column(Boolean, default=False)
    musicbrainz_trackid: Mapped[Optional[str]] = mapped_column(String(64))

    group: Mapped[AlbumGroup] = relationship(back_populates="tracks")


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("album_groups.id"), index=True)
    source: Mapped[str] = mapped_column(String(64))          # musicbrainz|itunes|deezer|google
    tier: Mapped[Tier] = mapped_column(Enum(Tier))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(Text)
    provenance_url: Mapped[str] = mapped_column(Text, default="")
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    phash: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    cluster: Mapped[Optional[int]] = mapped_column(Integer)   # google consensus cluster id
    cluster_size: Mapped[int] = mapped_column(Integer, default=1)
    cache_path: Mapped[Optional[str]] = mapped_column(Text)   # downloaded copy in image_cache
    sha256: Mapped[Optional[str]] = mapped_column(String(64))
    # Fingerprint of the local album and its exact track paths when this was found.
    # A queued apply must not survive a rescan/re-grouping of the same DB row.
    group_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    group: Mapped[AlbumGroup] = relationship(back_populates="candidates")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[JobKind] = mapped_column(Enum(JobKind), index=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.queued, index=True)
    group_id: Mapped[Optional[int]] = mapped_column(ForeignKey("album_groups.id"))
    payload: Mapped[str] = mapped_column(Text, default="{}")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))


class AuditEntry(Base):
    __tablename__ = "audit_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("album_groups.id"), index=True)
    action: Mapped[str] = mapped_column(String(32))           # apply|undo|skip
    candidate_id: Mapped[Optional[int]] = mapped_column(ForeignKey("candidates.id"))
    source: Mapped[str] = mapped_column(String(64), default="")
    tier: Mapped[Optional[Tier]] = mapped_column(Enum(Tier))
    image_url: Mapped[str] = mapped_column(Text, default="")
    image_sha256: Mapped[str] = mapped_column(String(64), default="")
    tracks_written: Mapped[int] = mapped_column(Integer, default=0)
    backup_dir: Mapped[str] = mapped_column(Text, default="")
    wrote_cover_jpg: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="{}")   # JSON: before/after summary
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    undone_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))


class SearchSession(Base):
    __tablename__ = "search_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[SearchStatus] = mapped_column(Enum(SearchStatus), default=SearchStatus.queued, index=True)
    selection_kind: Mapped[str] = mapped_column(String(16), default="tracks")
    folder_path: Mapped[str] = mapped_column(Text, default="")
    recursive: Mapped[bool] = mapped_column(Boolean, default=True)
    include_existing: Mapped[bool] = mapped_column(Boolean, default=True)
    confirmed_large: Mapped[bool] = mapped_column(Boolean, default=False)
    total_tracks: Mapped[int] = mapped_column(Integer, default=0)
    completed_tracks: Mapped[int] = mapped_column(Integer, default=0)
    failed_tracks: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))


class ArtworkQuery(Base):
    __tablename__ = "artwork_queries"
    __table_args__ = (UniqueConstraint("session_id", "role", "query_key", name="uq_session_query"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("search_sessions.id"), index=True)
    role: Mapped[QueryRole] = mapped_column(Enum(QueryRole), index=True)
    query_key: Mapped[str] = mapped_column(String(1024))
    artist: Mapped[str] = mapped_column(String(512), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    album: Mapped[str] = mapped_column(String(512), default="")
    album_artist: Mapped[str] = mapped_column(String(512), default="")
    year: Mapped[Optional[int]] = mapped_column(Integer)
    musicbrainz_id: Mapped[Optional[str]] = mapped_column(String(64))
    release_group_id: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[WorkStatus] = mapped_column(Enum(WorkStatus), default=WorkStatus.queued, index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    google_used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))


class SearchTarget(Base):
    __tablename__ = "search_targets"
    __table_args__ = (UniqueConstraint("session_id", "track_id", name="uq_session_track"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("search_sessions.id"), index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id"), index=True)
    track_query_id: Mapped[int] = mapped_column(ForeignKey("artwork_queries.id"))
    album_query_id: Mapped[int] = mapped_column(ForeignKey("artwork_queries.id"))
    artwork_role: Mapped[QueryRole] = mapped_column(Enum(QueryRole), default=QueryRole.album)
    status: Mapped[WorkStatus] = mapped_column(Enum(WorkStatus), default=WorkStatus.queued, index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    search_title: Mapped[str] = mapped_column(String(512), default="")
    search_artist: Mapped[str] = mapped_column(String(512), default="")
    search_album: Mapped[str] = mapped_column(String(512), default="")
    search_album_artist: Mapped[str] = mapped_column(String(512), default="")
    selected_album_cluster_id: Mapped[Optional[int]] = mapped_column(ForeignKey("artwork_clusters.id"))
    selected_track_cluster_id: Mapped[Optional[int]] = mapped_column(ForeignKey("artwork_clusters.id"))
    album_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    track_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")


class ArtworkAsset(Base):
    __tablename__ = "artwork_assets"
    __table_args__ = (UniqueConstraint("sha256", name="uq_artwork_asset_sha"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    phash: Mapped[str] = mapped_column(String(32), index=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(64), default="image/jpeg")
    cache_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArtworkCluster(Base):
    __tablename__ = "artwork_clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    query_id: Mapped[int] = mapped_column(ForeignKey("artwork_queries.id"), index=True)
    representative_asset_id: Mapped[int] = mapped_column(ForeignKey("artwork_assets.id"))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_label: Mapped[str] = mapped_column(String(16), default="low")
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)


class CandidateObservation(Base):
    __tablename__ = "candidate_observations"
    __table_args__ = (UniqueConstraint("query_id", "source", "image_url", name="uq_query_source_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    query_id: Mapped[int] = mapped_column(ForeignKey("artwork_queries.id"), index=True)
    cluster_id: Mapped[Optional[int]] = mapped_column(ForeignKey("artwork_clusters.id"), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("artwork_assets.id"))
    source: Mapped[str] = mapped_column(String(64))
    image_url: Mapped[str] = mapped_column(Text)
    provenance_url: Mapped[str] = mapped_column(Text, default="")
    source_host: Mapped[str] = mapped_column(String(255), default="")
    metadata_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_kind: Mapped[str] = mapped_column(String(32), default="search")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrackWriteAudit(Base):
    __tablename__ = "track_write_audits"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[Optional[int]] = mapped_column(ForeignKey("search_sessions.id"), nullable=True, index=True)
    target_id: Mapped[Optional[int]] = mapped_column(ForeignKey("search_targets.id"), nullable=True, index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id"), index=True)
    action: Mapped[str] = mapped_column(String(32), default="apply")
    roles: Mapped[str] = mapped_column(String(64), default="")
    backup_dir: Mapped[str] = mapped_column(Text, default="")
    image_sha256: Mapped[str] = mapped_column(Text, default="")
    wrote_cover_jpg: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    undone_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
