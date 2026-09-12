from __future__ import annotations

from app.sources.plex import PlexSource, _PlexAlbum


def test_plex_requires_one_common_album_for_all_local_paths(monkeypatch):
    source = PlexSource()
    a = _PlexAlbum("1", "Album A", "Artist", "/library/metadata/1/thumb")
    b = _PlexAlbum("2", "Album B", "Artist", "/library/metadata/2/thumb")
    source._index = {
        "/volume4/music/A/01.mp3": {a},
        "/volume4/music/A/02.mp3": {a, b},
        "/volume4/music/A/03.mp3": {b},
    }
    monkeypatch.setattr(source, "_local_to_plex_paths", lambda p: {p})
    common = None
    for path in ("/volume4/music/A/01.mp3", "/volume4/music/A/02.mp3"):
        albums = set().union(*(source._index.get(x, set()) for x in source._local_to_plex_paths(path)))
        common = albums if common is None else common & albums
    assert common == {a}

    common = None
    for path in ("/volume4/music/A/01.mp3", "/volume4/music/A/03.mp3"):
        albums = set().union(*(source._index.get(x, set()) for x in source._local_to_plex_paths(path)))
        common = albums if common is None else common & albums
    assert common == set()
