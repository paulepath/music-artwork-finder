"""Pure helpers for turning a flat list of scanned tracks into album groups.

No DB or filesystem access here so it is trivially unit-testable.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from .matching import normalize


@dataclass
class ScannedTrack:
    path: str                       # POSIX path as seen inside the container
    album: str = ""
    album_artist: str = ""
    artist: str = ""
    title: str = ""
    disc: int = 1
    track_no: int | None = None
    year: int | None = None
    duration_s: float | None = None
    is_compilation: bool = False
    has_embedded_art: bool = False
    art_sha: str | None = None            # sha1 of this track's embedded front cover
    musicbrainz_albumid: str | None = None
    musicbrainz_releasegroupid: str | None = None
    musicbrainz_trackid: str | None = None

    @property
    def dir(self) -> str:
        return posixpath.dirname(self.path)

    @property
    def effective_album_artist(self) -> str:
        return self.album_artist or self.artist


def _album_base(title: str) -> str:
    """Album title minus disc markers, so 'Box Disc 1' == 'Box Disc 2'."""
    toks = normalize(title).split()
    out: list[str] = []
    skip = False
    for i, t in enumerate(toks):
        if skip:
            skip = False
            continue
        if t in ("disc", "disk", "cd") and i + 1 < len(toks) and toks[i + 1].isdigit():
            skip = True
            continue
        out.append(t)
    return " ".join(out)


def album_base(title: str) -> str:
    """Public disc-marker normalizer used by track-first album queries."""
    return _album_base(title)


def group_key(album_artist: str, album: str, disc: int, directory: str = "") -> str:
    """Stable key for one on-disk album directory.

    Album/artist tags are not unique: compilation titles and reissues frequently
    occur in more than one directory.  Directory identity is therefore part of
    the write scope; it prevents one candidate from being embedded into another
    release with the same tags.
    """
    aa = normalize(album_artist) or "?"
    al = normalize(album) or "?"
    directory = posixpath.normpath(directory) if directory else ""
    return f"{aa}|||{al}|||{disc}|||{directory}"


@dataclass
class Group:
    key: str
    album: str
    album_artist: str
    disc: int
    year: int | None = None
    is_compilation: bool = False
    musicbrainz_albumid: str | None = None
    musicbrainz_releasegroupid: str | None = None
    tracks: list[ScannedTrack] = field(default_factory=list)
    dirs: set[str] = field(default_factory=set)
    multi_album_parent: bool = False
    incomplete_tags: bool = False

    @property
    def album_base(self) -> str:
        return _album_base(self.album)

    @property
    def art_sha(self) -> str | None:
        for t in self.tracks:
            if t.art_sha:
                return t.art_sha
        return None

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    @property
    def has_embedded_art(self) -> bool:
        return bool(self.tracks) and all(t.has_embedded_art for t in self.tracks)

    @property
    def common_dir(self) -> str:
        if not self.dirs:
            return ""
        return posixpath.commonpath(sorted(self.dirs)) if len(self.dirs) > 1 else next(iter(self.dirs))


def _first(*vals):
    for v in vals:
        if v:
            return v
    return None


def build_groups(tracks: list[ScannedTrack]) -> list[Group]:
    groups: dict[str, Group] = {}
    for t in tracks:
        aa = t.effective_album_artist
        key = group_key(aa, t.album, t.disc, t.dir)
        g = groups.get(key)
        if g is None:
            g = groups[key] = Group(
                key=key, album=t.album, album_artist=aa, disc=t.disc,
                year=t.year, is_compilation=t.is_compilation,
                musicbrainz_albumid=t.musicbrainz_albumid,
                musicbrainz_releasegroupid=t.musicbrainz_releasegroupid,
            )
        g.tracks.append(t)
        g.dirs.add(t.dir)
        g.year = g.year or t.year
        g.is_compilation = g.is_compilation or t.is_compilation
        g.musicbrainz_albumid = _first(g.musicbrainz_albumid, t.musicbrainz_albumid)
        g.musicbrainz_releasegroupid = _first(g.musicbrainz_releasegroupid, t.musicbrainz_releasegroupid)

    for g in groups.values():
        g.incomplete_tags = (
            not normalize(g.album) or not normalize(g.album_artist) or g.album_artist == "?"
        )
    _flag_multi_album_parents(list(groups.values()))
    return list(groups.values())


def _flag_multi_album_parents(groups: list[Group]) -> None:
    """Flag only groups that genuinely cannot be treated as one album:

    * a directory that *directly* holds tracks from two or more distinct album
      groups (e.g. ``/music/Classical`` full of loose, differently-tagged files);
    * a directory that directly holds this group's tracks *and* is a strict
      ancestor of another group's directory that carries a *different* album
      title (a folder of loose MP3s sitting above real album sub-folders).

    Multi-disc box sets — each disc in its own sub-folder, sharing an album
    title — are NOT flagged: every disc is a normal, art-able album.
    """
    dir_to_keys: dict[str, set[str]] = {}
    dir_to_albums: dict[str, set[str]] = {}
    dir_incomplete: dict[str, bool] = {}
    for g in groups:
        for d in g.dirs:
            dir_to_keys.setdefault(d, set()).add(g.key)
            dir_to_albums.setdefault(d, set()).add(_album_base(g.album))
            dir_incomplete[d] = dir_incomplete.get(d, False) or g.incomplete_tags

    # a directly-holding dir is "mixed" only when it carries two or more distinct
    # *base* album titles (multi-disc sets sharing a title are fine)
    mixed_dirs = {d for d, albums in dir_to_albums.items() if len(albums) > 1}
    all_dirs = list(dir_to_keys)

    bad_dirs = set(mixed_dirs)
    for d in all_dirs:
        own_albums = dir_to_albums[d]
        foreign: set[str] = set()
        for other in all_dirs:
            if other == d or not (other + "/").startswith(d + "/"):
                continue
            foreign |= dir_to_albums[other] - own_albums
        # a dir with loose tracks sitting above real album folders is a dumping
        # ground when its own tags are junk, or it has several unrelated children.
        if foreign and (dir_incomplete.get(d) or len(foreign) >= 2):
            bad_dirs.add(d)

    for g in groups:
        if g.dirs & bad_dirs:
            g.multi_album_parent = True
