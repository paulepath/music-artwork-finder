"""Read and atomically write album/front plus track-specific artwork."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover

from .config import get_settings
from .models import Track
from .writer import WriteError, backup_files, normalize_jpeg, restore_files

TRACK_DESCRIPTION = "Track Artwork"


def track_path(track: Track) -> Path:
    return get_settings().music_root / track.path.lstrip("/")


def fingerprint(track: Track) -> str:
    path = track_path(track)
    stat = path.stat()
    body = "\x1f".join((
        track.path, track.artist, track.title, track.album_artist, track.album,
        str(track.disc), str(track.year or ""), str(stat.st_size), str(stat.st_mtime_ns),
    ))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _picture_bytes(pic: Picture) -> bytes:
    return bytes(pic.data)


def read_artwork(track: Track) -> dict[str, bytes | None]:
    """Return the managed album and track roles without guessing other picture types."""
    path = track_path(track)
    album: bytes | None = None
    track_art: bytes | None = None
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            return {"album": None, "track": None}
        for pic in tags.getall("APIC"):
            if pic.type == 3 and album is None:
                album = bytes(pic.data)
            if pic.type == 0 and pic.desc == TRACK_DESCRIPTION and track_art is None:
                track_art = bytes(pic.data)
    elif suffix == ".flac":
        for pic in FLAC(path).pictures:
            if pic.type == 3 and album is None:
                album = _picture_bytes(pic)
            if pic.type == 0 and pic.desc == TRACK_DESCRIPTION and track_art is None:
                track_art = _picture_bytes(pic)
    elif suffix in (".ogg", ".oga", ".opus"):
        audio = MutagenFile(path)
        values = (audio.tags or {}).get("metadata_block_picture", []) if audio else []
        for value in values:
            try:
                pic = Picture(base64.b64decode(value))
            except (ValueError, TypeError):
                continue
            if pic.type == 3 and album is None:
                album = _picture_bytes(pic)
            if pic.type == 0 and pic.desc == TRACK_DESCRIPTION and track_art is None:
                track_art = _picture_bytes(pic)
    elif suffix in (".m4a", ".mp4", ".m4b"):
        covers = list((MP4(path).tags or {}).get("covr", []))
        album = bytes(covers[0]) if covers else None
        track_art = bytes(covers[1]) if len(covers) > 1 else None
    return {"album": album, "track": track_art}


def _picture(raw: bytes, picture_type: int, description: str) -> Picture:
    pic = Picture()
    pic.type = picture_type
    pic.mime = "image/jpeg"
    pic.desc = description
    pic.data = raw
    return pic


def _write_roles(path: Path, album: bytes | None, track_art: bytes | None,
                 *, replace_album: bool, replace_track: bool) -> None:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        keep = [p for p in tags.getall("APIC")
                if not (replace_album and p.type == 3)
                and not (replace_track and p.type == 0 and p.desc == TRACK_DESCRIPTION)]
        tags.delall("APIC")
        for pic in keep:
            tags.add(pic)
        if replace_album and album:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Album Cover", data=album))
        if replace_track and track_art and (not album or track_art != album):
            tags.add(APIC(encoding=3, mime="image/jpeg", type=0, desc=TRACK_DESCRIPTION, data=track_art))
        # Always downgrade to ID3v2.3: classic Windows Media Player / Explorer's
        # shell thumbnail handler cannot parse ID3v2.4 frame headers and show no
        # art at all for v2.4 files, even though the tag is spec-valid. v2.3 is
        # universally readable (Windows, MA, everything else) and update_to_v23()
        # must be called before save(v2_version=3) or frame encodings/dates are
        # left in v2.4-only form.
        tags.update_to_v23()
        tags.save(path, v2_version=3)
        return

    if suffix == ".flac":
        audio = FLAC(path)
        keep = [p for p in audio.pictures
                if not (replace_album and p.type == 3)
                and not (replace_track and p.type == 0 and p.desc == TRACK_DESCRIPTION)]
        audio.clear_pictures()
        for pic in keep:
            audio.add_picture(pic)
        if replace_album and album:
            audio.add_picture(_picture(album, 3, "Album Cover"))
        if replace_track and track_art and (not album or track_art != album):
            audio.add_picture(_picture(track_art, 0, TRACK_DESCRIPTION))
        audio.save()
        return

    if suffix in (".ogg", ".oga", ".opus"):
        audio = MutagenFile(path)
        if audio is None:
            raise WriteError(f"cannot open {suffix} file")
        if audio.tags is None:
            audio.add_tags()
        keep: list[Picture] = []
        for value in (audio.tags or {}).get("metadata_block_picture", []):
            try:
                pic = Picture(base64.b64decode(value))
            except (ValueError, TypeError):
                continue
            if replace_album and pic.type == 3:
                continue
            if replace_track and pic.type == 0 and pic.desc == TRACK_DESCRIPTION:
                continue
            keep.append(pic)
        if replace_album and album:
            keep.append(_picture(album, 3, "Album Cover"))
        if replace_track and track_art and (not album or track_art != album):
            keep.append(_picture(track_art, 0, TRACK_DESCRIPTION))
        audio.tags["metadata_block_picture"] = [
            base64.b64encode(pic.write()).decode("ascii") for pic in keep
        ]
        audio.save()
        return

    if suffix in (".m4a", ".mp4", ".m4b"):
        audio = MP4(path)
        existing = list((audio.tags or {}).get("covr", []))
        album_value = album if replace_album else (bytes(existing[0]) if existing else None)
        track_value = track_art if replace_track else (bytes(existing[1]) if len(existing) > 1 else None)
        covers = []
        if album_value:
            covers.append(MP4Cover(album_value, imageformat=MP4Cover.FORMAT_JPEG))
        if track_value and track_value != album_value:
            covers.append(MP4Cover(track_value, imageformat=MP4Cover.FORMAT_JPEG))
        audio["covr"] = covers
        audio.save()
        return
    raise WriteError(f"unsupported extension for embedding: {suffix}")


def apply_track_artwork(track: Track, *, album_raw: bytes | None, track_raw: bytes | None,
                        replace_album: bool, replace_track: bool,
                        write_cover_jpg: bool = False, backup_key: int = 0) -> tuple[str, str, bool]:
    """Apply both roles to one file; automatically restore it on any failure."""
    path = track_path(track)
    if not path.is_file():
        raise WriteError(f"track file is missing: {track.path}")
    album = normalize_jpeg(album_raw) if album_raw else None
    track_art = normalize_jpeg(track_raw) if track_raw else None
    sidecar_rel = str(Path(track.path).parent / "cover.jpg").replace("\\", "/") if write_cover_jpg else None
    backup = backup_files(backup_key or track.id, [track.path], sidecar_rel=sidecar_rel)
    try:
        _write_roles(path, album, track_art, replace_album=replace_album, replace_track=replace_track)
        wrote_cover = False
        if sidecar_rel and album:
            (get_settings().music_root / sidecar_rel.lstrip("/")).write_bytes(album)
            wrote_cover = True
    except Exception:
        restore_files(str(backup))
        raise
    hashes = ",".join(hashlib.sha256(value).hexdigest() for value in (album, track_art) if value)
    return str(backup), hashes, wrote_cover

