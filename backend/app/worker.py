"""Single background worker that drains the ``jobs`` table.

Resumable: on startup any ``running`` job is reset to ``queued``. Jobs are processed
one at a time (politeness to the metadata APIs matters more than throughput here).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath

from sqlalchemy import select

from .consensus import cluster_phashes, fetch_and_hash
from .db import session_scope
from .http import new_client
from .matching import GroupMeta, classify
from .models import (
    AlbumGroup, Candidate, GroupState, Job, JobKind, JobStatus, Tier, utcnow,
)
from .scanner import run_scan
from .sources import DeezerSource, ITunesSource, MusicBrainzSource, PlexSource
from .sources.google_images import GoogleImagesSource
from .writer import apply_artwork

log = logging.getLogger("artwork.worker")
TRUSTED = (MusicBrainzSource(), ITunesSource(), DeezerSource())
PLEX = PlexSource()
_ELIGIBLE = (GroupState.scanned, GroupState.pending_review, GroupState.google_only,
             GroupState.has_art, GroupState.error)


def enqueue(kind: JobKind, *, group_id: int | None = None, payload: dict | None = None) -> int:
    with session_scope() as db:
        job = Job(kind=kind, group_id=group_id, payload=json.dumps(payload or {}))
        db.add(job)
        db.flush()
        return job.id


def _group_meta(g: AlbumGroup) -> GroupMeta:
    return GroupMeta(
        album=g.album, album_artist=g.album_artist, year=g.year,
        track_count=g.track_count, mbid=g.musicbrainz_albumid,
        release_group_id=g.musicbrainz_releasegroupid, is_compilation=g.is_compilation,
        track_paths=tuple(sorted(t.path for t in g.tracks)),
    )


def _group_fingerprint(g: AlbumGroup) -> str:
    """Fingerprint both the metadata and the exact set of files a write targets."""
    body = "\x1f".join((g.album_artist, g.album, str(g.disc), *sorted(t.path for t in g.tracks)))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _safe_write_scope(g: AlbumGroup) -> bool:
    """Reject legacy merged groups whose files are not all in their album directory."""
    return bool(g.tracks and g.common_dir and all(
        posixpath.dirname(t.path) == g.common_dir for t in g.tracks
    ))


class Worker:
    def __init__(self, poll_interval: float = 1.5):
        self.poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        with session_scope() as db:
            for job in db.scalars(select(Job).where(Job.status == JobStatus.running)):
                job.status = JobStatus.queued
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="artwork-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stop.is_set():
            job_id = self._claim()
            if job_id is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._dispatch(job_id)
            except Exception as e:  # noqa: BLE001
                log.exception("job %s failed", job_id)
                self._finish(job_id, JobStatus.failed, message=str(e))

    def _claim(self) -> int | None:
        with session_scope() as db:
            job = db.scalars(
                select(Job).where(Job.status == JobStatus.queued).order_by(Job.id).limit(1)
            ).first()
            if job is None:
                return None
            job.status = JobStatus.running
            job.started_at = utcnow()
            return job.id

    def _finish(self, job_id: int, status: JobStatus, message: str = "", progress: float = 1.0):
        with session_scope() as db:
            job = db.get(Job, job_id)
            if job:
                job.status = status
                job.message = message[:2000]
                job.progress = progress
                job.finished_at = utcnow()

    def _set_progress(self, job_id: int, progress: float, message: str = ""):
        with session_scope() as db:
            job = db.get(Job, job_id)
            if job:
                job.progress = progress
                if message:
                    job.message = message[:2000]

    async def _dispatch(self, job_id: int) -> None:
        with session_scope() as db:
            job = db.get(Job, job_id)
            kind, group_id, payload = job.kind, job.group_id, json.loads(job.payload or "{}")

        if kind == JobKind.scan:
            await self._do_scan(job_id)
        elif kind == JobKind.find_candidates:
            await self._do_find_candidates(job_id, group_id, payload)
        elif kind == JobKind.google_search:
            await self._do_google(job_id, group_id, payload)
        elif kind == JobKind.apply:
            await self._do_apply(job_id, payload)
        elif kind == JobKind.ma_sync:
            await self._do_ma_sync(job_id, payload)
        elif kind == JobKind.search_session:
            await self._do_search_session(job_id, payload)
        elif kind == JobKind.apply_session:
            await self._do_apply_session(job_id, payload)
        else:  # pragma: no cover
            self._finish(job_id, JobStatus.failed, f"unknown job kind {kind}")

    # ---- handlers ------------------------------------------------------

    async def _do_scan(self, job_id: int) -> None:
        loop = asyncio.get_running_loop()

        def cb(frac: float, msg: str):
            self._set_progress(job_id, max(0.0, min(frac, 0.99)), msg)

        scan_id = await loop.run_in_executor(None, run_scan, cb)
        self._finish(job_id, JobStatus.done, f"scan {scan_id} complete")

    async def _do_find_candidates(self, job_id: int, group_id: int | None, payload: dict) -> None:
        with session_scope() as db:
            if group_id:
                ids = [group_id]
            elif payload.get("suspect_only"):
                ids = list(db.scalars(
                    select(AlbumGroup.id).where(AlbumGroup.state == GroupState.suspect_art)
                ))
            else:
                states = [GroupState.scanned, GroupState.error, GroupState.suspect_art]
                if payload.get("include_has_art"):
                    states.append(GroupState.has_art)
                q = select(AlbumGroup.id).where(AlbumGroup.state.in_(tuple(states)))
                if not payload.get("include_has_art"):
                    q = q.where(
                        (AlbumGroup.has_embedded_art.is_(False))
                        | (AlbumGroup.state == GroupState.suspect_art)
                    )
                ids = list(db.scalars(q))

        total = len(ids) or 1
        async with new_client() as client:
            for i, gid in enumerate(ids):
                await self._find_for_group(client, gid)
                self._set_progress(job_id, (i + 1) / total, f"{i + 1}/{total} albums")
        self._finish(job_id, JobStatus.done, f"processed {len(ids)} albums")

    async def _find_for_group(self, client, group_id: int) -> None:
        with session_scope() as db:
            g = db.get(AlbumGroup, group_id)
            if g is None or g.state in (GroupState.multi_album_parent, GroupState.needs_tagging):
                return
            gm = _group_meta(g)
            db.query(Candidate).filter_by(group_id=group_id).filter(
                Candidate.source != "google"
            ).delete()

        found: list[dict] = []
        # Plex is deliberately last and review-only.  It is not a name search:
        # PlexSource returns only an album common to every local track path.
        sources = (*TRUSTED, PLEX) if PLEX.enabled else TRUSTED
        for src in sources:
            try:
                results = await src.find(client, gm)
            except Exception as e:  # noqa: BLE001
                log.warning("source %s failed for group %s: %s", src.name, group_id, e)
                continue
            for cd in results:
                tr = classify(gm, cd.release)
                if tr.rejected:
                    log.info("rejected %s cover for group %s: %s", src.name, group_id, tr.reason)
                    continue
                headers = PLEX._headers() if cd.source == PLEX.name else None
                fetched = await fetch_and_hash(client, cd.image_url, headers=headers)
                if not fetched.ok:
                    continue
                tier = Tier.review if cd.extra.get("force_review") else tr.tier
                reason = ("Plex: exact file-path match for every track — review before embedding"
                          if cd.extra.get("force_review") else tr.reason)
                found.append(dict(
                    source=cd.source, tier=tier, confidence=tr.confidence, reason=reason,
                    image_url=cd.image_url, provenance_url=cd.provenance_url,
                    width=fetched.width, height=fetched.height, phash=fetched.phash,
                    sha256=fetched.sha256, cache_path=fetched.cache_path,
                ))

        with session_scope() as db:
            g = db.get(AlbumGroup, group_id)
            fingerprint = _group_fingerprint(g)
            # One immutable image is enough per source/release; do not create
            # duplicate cards when Plex returns repeated media records.
            seen: set[tuple[str, str]] = set()
            for f in found:
                identity = (f["source"], f["sha256"])
                if identity in seen:
                    continue
                seen.add(identity)
                f["group_fingerprint"] = fingerprint
                db.add(Candidate(group_id=group_id, **f))
            if found:
                g.state = GroupState.pending_review
            elif g.state in (GroupState.scanned, GroupState.error, GroupState.suspect_art):
                g.state = GroupState.google_only

    async def _do_google(self, job_id: int, group_id: int | None, payload: dict) -> None:
        if not group_id:
            self._finish(job_id, JobStatus.failed, "google_search requires a group_id")
            return
        with session_scope() as db:
            g = db.get(AlbumGroup, group_id)
            if g is None:
                self._finish(job_id, JobStatus.failed, "group not found")
                return
            if not g.google_enabled:
                self._finish(job_id, JobStatus.failed, "google not enabled for this album")
                return
            gm = _group_meta(g)
            db.query(Candidate).filter_by(group_id=group_id, source="google").delete()

        source = GoogleImagesSource(max_results=payload.get("max_results", 18))
        async with new_client() as client:
            raw = await source.find(client, gm)
            self._set_progress(job_id, 0.4, f"{len(raw)} raw results, downloading")
            fetched = []
            for cd in raw:
                fi = await fetch_and_hash(client, cd.image_url)
                if fi.ok:
                    fetched.append((cd, fi))
            clusters = cluster_phashes([fi.phash for _, fi in fetched])
            sizes: dict[int, int] = {}
            for c in clusters:
                if c >= 0:
                    sizes[c] = sizes.get(c, 0) + 1

        with session_scope() as db:
            for (cd, fi), cl in zip(fetched, clusters):
                db.add(Candidate(
                    group_id=group_id, source="google", tier=Tier.review,
                    confidence=round(sizes.get(cl, 1) / max(len(fetched), 1), 3),
                    reason=f"google image; consensus cluster {cl} ({sizes.get(cl, 1)} of {len(fetched)})",
                    image_url=cd.image_url, provenance_url=cd.provenance_url,
                    width=fi.width, height=fi.height, phash=fi.phash, sha256=fi.sha256,
                    cache_path=fi.cache_path, cluster=cl, cluster_size=sizes.get(cl, 1),
                ))
            g = db.get(AlbumGroup, group_id)
            if g.state == GroupState.google_only and fetched:
                g.state = GroupState.pending_review
        self._finish(job_id, JobStatus.done, f"{len(fetched)} google candidates in {len(sizes)} clusters")

    async def _do_apply(self, job_id: int, payload: dict) -> None:
        candidate_id = payload["candidate_id"]
        write_cover = bool(payload.get("write_cover_jpg", False))
        with session_scope() as db:
            cand = db.get(Candidate, candidate_id)
            if cand is None:
                self._finish(job_id, JobStatus.failed, "candidate not found")
                return
            group = db.get(AlbumGroup, cand.group_id)
            if cand.tier in (Tier.fuzzy, Tier.review) and not payload.get("approved"):
                self._finish(job_id, JobStatus.failed,
                             f"{cand.tier.value} candidate needs explicit approval")
                return
            if group.state != GroupState.pending_review or not _safe_write_scope(group):
                self._finish(job_id, JobStatus.failed,
                             "album is no longer an eligible single-directory review group")
                return
            if not cand.group_fingerprint or cand.group_fingerprint != _group_fingerprint(group):
                self._finish(job_id, JobStatus.failed,
                             "album changed after this candidate was found; search again")
                return

            data = None
            if cand.cache_path:
                try:
                    from pathlib import Path
                    data = Path(cand.cache_path).read_bytes()
                except OSError:
                    data = None
            if data is None:
                self._finish(job_id, JobStatus.failed,
                             "candidate cache is missing; search again rather than re-downloading changed artwork")
                return
            if not cand.sha256 or hashlib.sha256(data).hexdigest() != cand.sha256:
                self._finish(job_id, JobStatus.failed,
                             "candidate cache checksum mismatch; search again")
                return

            entry = apply_artwork(db, group, cand, data, write_cover_jpg=write_cover)
            group.state = GroupState.applied if entry.ok else GroupState.error
            msg = f"applied {cand.source} cover to {entry.tracks_written} tracks"
            if entry.error:
                msg += f" (errors: {entry.error})"
        self._finish(job_id, JobStatus.done if entry.ok else JobStatus.failed, msg)

    async def _do_ma_sync(self, job_id: int, payload: dict) -> None:
        from .ma_sync import trigger_sync
        res = await trigger_sync(payload.get("media_types"))
        self._finish(job_id, JobStatus.done if res.ok else JobStatus.failed, res.detail)

    async def _do_search_session(self, job_id: int, payload: dict) -> None:
        from .search_service import run_search_session
        detail = await run_search_session(int(payload["session_id"]))
        status = JobStatus.cancelled if detail == "cancelled" else JobStatus.done
        self._finish(job_id, status, detail)

    async def _do_apply_session(self, job_id: int, payload: dict) -> None:
        from .search_service import apply_search_session
        loop = asyncio.get_running_loop()
        detail = await loop.run_in_executor(None, apply_search_session, int(payload["session_id"]))
        wrote_any = not detail.startswith("applied 0 ")
        # Music Assistant caches the library's embedded cover art. Queue its
        # supported music sync after successful writes so the result becomes
        # visible without requiring a separate dashboard action.
        if wrote_any:
            sync_job_id = enqueue(JobKind.ma_sync, payload={"origin": "artwork_apply"})
            detail = f"{detail}; Music Assistant sync queued (job {sync_job_id})"
        self._finish(job_id, JobStatus.done if detail.startswith("applied") and "; 0 failed" in detail else JobStatus.failed, detail)


worker = Worker()
