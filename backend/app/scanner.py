"""Filesystem walk + tag parsing + persistence of album groups.

Read-only with respect to music files. Synology ``@eaDir`` sidecar directories
contain entries named like real audio files and MUST be skipped everywhere.
"""
from __future__ import annotations

import logging
import posixpath
from pathlib import Path, PurePosixPath

from mutagen import File as MutagenFile

from .config import get_settings
from .db import session_scope
from .grouping import Group, ScannedTrack, build_groups
from .matching import normalize
from .models import AlbumGroup, GroupState, ScanRun, Track

log = logging.getLogger("artwork.scanner")

EADIR = "@eaDir"


def _to_int(v) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    for sep in ("/", "-", " "):
        if sep in s:
            s = s.split(sep)[0]
    try:
        return int(s)
    except ValueError:
        return None


def _year(v) -> int | None:
    if v is None:
        return None
    s = str(v)
    for i in range(len(s) - 3):
        chunk = s[i:i + 4]
        if chunk.isdigit() and 1900 <= int(chunk) <= 2100:
            return int(chunk)
    return None


def _has_embedded_front_art(path: Path) -> bool:
    """True only when the file carries something usable as a *front* cover.

    A back cover / booklet / artist image (APIC type != 3) does not count, so a
    file that has only those still routes into artwork recovery. A lone picture
    of unspecified type is treated as the front (very common in the wild).
    """
    try:
        audio = MutagenFile(str(path))
    except Exception:  # noqa: BLE001 - corrupt file
        return False
    if audio is None:
        return False

    pics = getattr(audio, "pictures", None)          # FLAC / OggVorbis
    if pics:
        return any(p.type == 3 for p in pics) or len(pics) == 1

    tags = getattr(audio, "tags", None)
    if tags is None:
        return False

    getall = getattr(tags, "getall", None)
    if callable(getall):
        apics = getall("APIC")
        if apics:
            return any(getattr(a, "type", 0) == 3 for a in apics) or len(apics) == 1

    try:
        keys = set(tags.keys())
    except Exception:  # noqa: BLE001
        return False
    return "covr" in keys or "metadata_block_picture" in keys


def _embedded_cover_sha(path: Path) -> str | None:
    """sha1 of the embedded front-cover image bytes (front APIC / lone picture / covr)."""
    import hashlib
    try:
        audio = MutagenFile(str(path))
    except Exception:  # noqa: BLE001
        return None
    if audio is None:
        return None

    data: bytes | None = None
    pics = getattr(audio, "pictures", None)
    if pics:
        front = [p for p in pics if p.type == 3] or (pics if len(pics) == 1 else [])
        if front:
            data = bytes(front[0].data)
    if data is None:
        tags = getattr(audio, "tags", None)
        getall = getattr(tags, "getall", None) if tags is not None else None
        if callable(getall):
            apics = getall("APIC")
            front = [a for a in apics if getattr(a, "type", 0) == 3] or (apics if len(apics) == 1 else [])
            if front:
                data = bytes(front[0].data)
        if data is None and tags is not None:
            try:
                covr = tags.get("covr")
            except Exception:  # noqa: BLE001
                covr = None
            if covr:
                data = bytes(covr[0])
    return hashlib.sha1(data).hexdigest() if data else None


def _easy(path: Path) -> dict:
    try:
        audio = MutagenFile(str(path), easy=True)
    except Exception:  # noqa: BLE001
        return {}
    if audio is None or audio.tags is None:
        return {}

    def g(key: str) -> str | None:
        val = audio.tags.get(key)
        if not val:
            return None
        return str(val[0]) if isinstance(val, list) else str(val)

    length = getattr(getattr(audio, "info", None), "length", None)
    comp = g("compilation")
    return {
        "album": g("album") or "",
        "albumartist": g("albumartist") or "",
        "artist": g("artist") or "",
        "title": g("title") or "",
        "disc": _to_int(g("discnumber")) or 1,
        "track_no": _to_int(g("tracknumber")),
        "year": _year(g("date") or g("originaldate") or g("year")),
        "duration_s": float(length) if length else None,
        "is_compilation": comp in ("1", "true", "True"),
        "musicbrainz_albumid": g("musicbrainz_albumid"),
        "musicbrainz_releasegroupid": g("musicbrainz_releasegroupid"),
        "musicbrainz_trackid": g("musicbrainz_trackid") or g("musicbrainz_releasetrackid"),
    }


def iter_audio_files(root: Path, exts: tuple[str, ...]):
    """Yield real audio files under ``root``, skipping any ``@eaDir`` component."""
    for p in root.rglob("*"):
        parts = p.parts
        if EADIR in parts:
            continue
        if p.suffix.lower() not in exts:
            continue
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        yield p


def read_track(path: Path, root: Path) -> ScannedTrack:
    meta = _easy(path)
    rel = "/" + PurePosixPath(path.relative_to(root).as_posix()).as_posix()
    return ScannedTrack(
        path=rel,
        album=meta.get("album", ""),
        album_artist=meta.get("albumartist", ""),
        artist=meta.get("artist", ""),
        title=meta.get("title", ""),
        disc=meta.get("disc", 1),
        track_no=meta.get("track_no"),
        year=meta.get("year"),
        duration_s=meta.get("duration_s"),
        is_compilation=meta.get("is_compilation", False),
        art_sha=(_art_sha := _embedded_cover_sha(path)),
        has_embedded_art=_art_sha is not None or _has_embedded_front_art(path),
        musicbrainz_albumid=meta.get("musicbrainz_albumid"),
        musicbrainz_releasegroupid=meta.get("musicbrainz_releasegroupid"),
        musicbrainz_trackid=meta.get("musicbrainz_trackid"),
    )


_FOLDER_ART_NAMES = {"cover", "folder", "front", "album", "albumart", "albumartsmall"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _has_folder_art(container_root: Path, group: Group) -> bool:
    for d in group.dirs:
        real = container_root / d.lstrip("/")
        try:
            for f in real.iterdir():
                if f.suffix.lower() in _IMAGE_EXTS and f.stem.lower() in _FOLDER_ART_NAMES:
                    return True
        except OSError:
            continue
    return False


def _classify_state(group: Group) -> GroupState:
    if group.multi_album_parent:
        return GroupState.multi_album_parent
    if group.incomplete_tags:
        return GroupState.needs_tagging
    return GroupState.scanned


def run_scan(progress_cb=None) -> int:
    """Full rescan. Returns the ScanRun id. Idempotent: groups are upserted by key."""
    s = get_settings()
    root = s.music_root
    if not root.is_dir():
        raise FileNotFoundError(f"MUSIC_ROOT {root} is not a directory")

    files = list(iter_audio_files(root, s.audio_extensions))
    total = len(files)
    tracks: list[ScannedTrack] = []
    for i, f in enumerate(files):
        tracks.append(read_track(f, root))
        if progress_cb and total and i % 200 == 0:
            progress_cb(i / total * 0.7, f"parsed {i}/{total} files")

    groups = build_groups(tracks)
    if progress_cb:
        progress_cb(0.75, f"grouped into {len(groups)} albums")

    # duplicate embedded-artwork detection: the same cover bytes on two or more
    # *different* albums almost always means a bad bulk embed.
    from collections import defaultdict
    hash_to_albums: dict[str, set[str]] = defaultdict(set)
    for g in groups:
        if g.art_sha:
            # Directory-aware group keys deliberately distinguish duplicate
            # releases with the same title.  Using only the title hid exactly
            # the bad bulk-artwork case this detector is meant to surface.
            hash_to_albums[g.art_sha].add(g.key)

    missing = 0
    with session_scope() as db:
        scan = ScanRun(files_seen=total, groups_total=len(groups))
        db.add(scan)
        db.flush()

        for g in groups:
            has_folder = _has_folder_art(root, g)
            has_embedded = g.has_embedded_art
            dupe_albums = len(hash_to_albums.get(g.art_sha, ())) - 1 if g.art_sha else 0
            state = _classify_state(g)
            if state == GroupState.scanned and (has_embedded and has_folder):
                state = GroupState.has_art
            # an album whose embedded cover also sits on other unrelated albums
            # needs a fresh look even though it technically "has art"
            if dupe_albums >= 1 and state in (GroupState.scanned, GroupState.has_art):
                state = GroupState.suspect_art
            if not (has_embedded and has_folder) and state != GroupState.suspect_art:
                missing += 1

            row = db.query(AlbumGroup).filter_by(group_key=g.key).one_or_none()
            if row is None:
                row = AlbumGroup(group_key=g.key)
                db.add(row)
            elif row.state in (GroupState.applied, GroupState.skipped):
                # keep user decisions; just refresh bookkeeping
                row.last_seen_scan_id = scan.id
                row.track_count = g.track_count
                _replace_tracks(db, row, g)
                continue

            row.album_artist = g.album_artist
            row.album = g.album
            row.disc = g.disc
            row.year = g.year
            row.is_compilation = g.is_compilation
            row.musicbrainz_albumid = g.musicbrainz_albumid
            row.musicbrainz_releasegroupid = g.musicbrainz_releasegroupid
            row.common_dir = g.common_dir
            row.track_count = g.track_count
            row.has_embedded_art = has_embedded
            row.has_folder_art = has_folder
            row.art_hash = g.art_sha
            row.art_dupe_albums = dupe_albums
            row.state = state
            row.last_seen_scan_id = scan.id
            db.flush()
            _replace_tracks(db, row, g)

        scan.groups_missing_art = missing
        import datetime as _dt
        scan.finished_at = _dt.datetime.now(_dt.timezone.utc)
        db.flush()
        scan_id = scan.id

    if progress_cb:
        progress_cb(1.0, f"done: {len(groups)} albums, {missing} missing art")
    return scan_id


def _replace_tracks(db, row: AlbumGroup, g: Group) -> None:
    # Track ids are stable because v2 search/audit rows reference them.  Move and
    # update an existing path instead of the legacy delete-and-reinsert strategy.
    from .models import SearchTarget, TrackWriteAudit
    paths = [t.path for t in g.tracks]
    for t in g.tracks:
        real = get_settings().music_root / t.path.lstrip("/")
        try:
            stat = real.stat()
            file_size, mtime_ns = stat.st_size, stat.st_mtime_ns
        except OSError:
            file_size, mtime_ns = 0, 0
        track = db.query(Track).filter_by(path=t.path).one_or_none()
        if track is None:
            track = Track(path=t.path)
            db.add(track)
        track.group_id = row.id
        track.title, track.artist = t.title, t.artist
        track.album = t.album
        track.album_artist = t.album_artist or t.effective_album_artist
        track.disc, track.year = t.disc, t.year
        track.file_format = real.suffix.lower().lstrip(".")
        track.file_size, track.mtime_ns = file_size, mtime_ns
        track.track_no, track.duration_s = t.track_no, t.duration_s
        track.has_embedded_art = t.has_embedded_art
        track.musicbrainz_trackid = t.musicbrainz_trackid

    stale = db.query(Track).filter(Track.group_id == row.id)
    if paths:
        stale = stale.filter(~Track.path.in_(paths))
    for track in stale:
        referenced = (
            db.query(SearchTarget.id).filter_by(track_id=track.id).first()
            or db.query(TrackWriteAudit.id).filter_by(track_id=track.id).first()
        )
        if not referenced:
            db.delete(track)
