"""Embed a front-cover image into an album group's audio files, with backup + undo.

Rules from the handoff:
  * always back up original files before writing;
  * exactly one front-cover APIC frame (type 3) — remove any others;
  * optional cover.jpg in the album folder, embedded is the default;
  * every write recorded in the audit log, fully reversible.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import logging
import shutil
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image

from .config import get_settings
from .models import AlbumGroup, AuditEntry, Candidate, Tier, utcnow

log = logging.getLogger("artwork.writer")
_settings = get_settings()
MAX_EDGE = 1400


class WriteError(RuntimeError):
    pass


def _container_path(rel_path: str) -> Path:
    return _settings.music_root / rel_path.lstrip("/")


def normalize_jpeg(raw: bytes, max_edge: int = MAX_EDGE) -> bytes:
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    if max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _backup_dir(group_id: int) -> Path:
    import uuid
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = _settings.backups_dir / str(group_id) / f"{stamp}-{uuid.uuid4().hex[:8]}"
    if d.exists():  # astronomically unlikely; never reuse a backup dir
        raise WriteError(f"backup dir already exists: {d}")
    d.mkdir(parents=True)
    return d


def backup_files(group_id: int, rel_paths: list[str], sidecar_rel: str | None = None) -> Path:
    """Snapshot every audio file plus the album-folder cover.jpg (recording whether
    it existed), so an undo restores the *exact* prior state."""
    d = _backup_dir(group_id)
    manifest: dict = {}
    for rel in rel_paths:
        src = _container_path(rel)
        if not src.is_file():
            # pre-validation should have caught this; record so undo can delete it
            manifest[hashlib.sha1(rel.encode()).hexdigest()[:16]] = {"rel": rel, "existed": False}
            continue
        key = hashlib.sha1(rel.encode()).hexdigest()[:16]
        shutil.copy2(src, d / f"{key}{src.suffix}")
        manifest[key] = {"rel": rel, "existed": True}

    sidecar = {"rel": sidecar_rel, "existed": False}
    if sidecar_rel:
        sp = _container_path(sidecar_rel)
        if sp.is_file():
            shutil.copy2(sp, d / "__sidecar")
            sidecar["existed"] = True
    (d / "manifest.json").write_text(
        json.dumps({"files": manifest, "sidecar": sidecar}, indent=2), encoding="utf-8")
    return d


def restore_files(backup_dir: str) -> int:
    d = Path(backup_dir)
    data = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    files = data.get("files", data)  # tolerate the old flat format
    n = 0
    for key, info in files.items():
        rel = info["rel"] if isinstance(info, dict) else info
        existed = info.get("existed", True) if isinstance(info, dict) else True
        target = _container_path(rel)
        matches = list(d.glob(f"{key}.*"))
        if matches:
            shutil.copy2(matches[0], target)
            n += 1
        elif not existed:
            target.unlink(missing_ok=True)   # file did not exist before -> remove our creation

    sidecar = data.get("sidecar") if isinstance(data, dict) else None
    if sidecar and sidecar.get("rel"):
        sp = _container_path(sidecar["rel"])
        if (d / "__sidecar").is_file():
            shutil.copy2(d / "__sidecar", sp)
        elif not sidecar.get("existed"):
            sp.unlink(missing_ok=True)
    return n


def _embed_one(path: Path, jpeg: bytes) -> None:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        try:
            tags = ID3(path)
            orig_minor = tags.version[1]
        except ID3NoHeaderError:
            tags = ID3()
            orig_minor = 4
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg))
        # preserve the file's existing ID3 sub-version so v2.4-only frames survive
        tags.save(path, v2_version=4 if orig_minor >= 4 else 3)
    elif suffix == ".flac":
        audio = FLAC(path)
        audio.clear_pictures()
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = jpeg
        audio.add_picture(pic)
        audio.save()
    elif suffix in (".m4a", ".mp4", ".m4b"):
        audio = MP4(path)
        audio["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
    else:
        raise WriteError(f"unsupported extension for embedding: {suffix}")


def apply_artwork(
    db,
    group: AlbumGroup,
    candidate: Candidate,
    image_bytes: bytes,
    *,
    write_cover_jpg: bool = False,
    decided_tier: Tier | None = None,
) -> AuditEntry:
    rel_paths = [t.path for t in group.tracks]
    if not rel_paths:
        raise WriteError("group has no tracks")

    # atomic pre-validation: never write (and never recreate) a file that has
    # gone missing since the last scan.
    missing = [rel for rel in rel_paths if not _container_path(rel).is_file()]
    if missing:
        raise WriteError(f"{len(missing)} track file(s) missing since last scan: {missing[:3]}")

    jpeg = normalize_jpeg(image_bytes)
    sha = hashlib.sha256(jpeg).hexdigest()
    sidecar_rel = (
        (group.common_dir.rstrip("/") + "/cover.jpg") if (write_cover_jpg and group.common_dir) else None
    )
    backup = backup_files(group.id, rel_paths, sidecar_rel=sidecar_rel)

    written = 0
    errors: list[str] = []
    for rel in rel_paths:
        p = _container_path(rel)
        try:
            _embed_one(p, jpeg)
            written += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{rel}: {e}")

    wrote_cover = False
    if sidecar_rel:
        try:
            _container_path(sidecar_rel).write_bytes(jpeg)
            wrote_cover = True
        except OSError as e:
            errors.append(f"cover.jpg: {e}")

    entry = AuditEntry(
        group_id=group.id,
        action="apply",
        candidate_id=candidate.id,
        source=candidate.source,
        tier=decided_tier or candidate.tier,
        image_url=candidate.image_url,
        image_sha256=sha,
        tracks_written=written,
        backup_dir=str(backup),
        wrote_cover_jpg=wrote_cover,
        ok=not errors,
        error="; ".join(errors),
        detail=json.dumps({"rel_paths": rel_paths, "bytes": len(jpeg)}),
    )
    db.add(entry)
    db.flush()
    return entry


def undo(db, entry: AuditEntry) -> int:
    if entry.undone_at is not None:
        return 0
    if not entry.backup_dir:
        raise WriteError("audit entry has no backup dir")
    # restore_files handles both audio files and the cover.jpg sidecar, including
    # deleting a cover.jpg that did not exist before this apply.
    restored = restore_files(entry.backup_dir)
    entry.undone_at = utcnow()
    db.add(AuditEntry(
        group_id=entry.group_id, action="undo", source=entry.source,
        image_sha256=entry.image_sha256, tracks_written=restored,
        backup_dir=entry.backup_dir, ok=True,
        detail=json.dumps({"undoes_audit_id": entry.id}),
    ))
    db.flush()
    return restored
