from __future__ import annotations

from app.grouping import ScannedTrack, _album_base, build_groups, group_key


def _t(path, album, artist="VA", disc=1, art=False):
    return ScannedTrack(path=path, album=album, album_artist=artist, disc=disc,
                        has_embedded_art=art)


def test_parent_folder_with_child_albums_is_flagged():
    tracks = [
        _t("/music/Classical/track-a.mp3", "Classical"),
        _t("/music/Classical/track-b.mp3", "Classical"),
        _t("/music/Classical/Beethoven - Symphony 9/01.mp3", "Symphony No. 9", "Beethoven"),
        _t("/music/Classical/Beethoven - Symphony 9/02.mp3", "Symphony No. 9", "Beethoven"),
        _t("/music/Classical/Mozart - Requiem/01.mp3", "Requiem", "Mozart"),
    ]
    groups = {g.key: g for g in build_groups(tracks)}
    parent = groups[group_key("VA", "Classical", 1, "/music/Classical")]
    child = groups[group_key("Beethoven", "Symphony No. 9", 1, "/music/Classical/Beethoven - Symphony 9")]
    assert parent.multi_album_parent is True   # loose files above >=2 real child albums
    assert child.multi_album_parent is False


def test_album_with_single_bonus_subfolder_is_not_flagged():
    tracks = [
        _t("/music/Foo/Great Album/01.mp3", "Great Album", "Band"),
        _t("/music/Foo/Great Album/02.mp3", "Great Album", "Band"),
        _t("/music/Foo/Great Album/Bonus Live/01.mp3", "Great Album (Live Bonus)", "Band"),
    ]
    main = [g for g in build_groups(tracks) if g.album == "Great Album"][0]
    assert main.multi_album_parent is False


def test_mixed_folder_two_albums_same_dir_is_flagged():
    tracks = [
        _t("/music/Mix/a1.mp3", "Album A", "Artist A"),
        _t("/music/Mix/b1.mp3", "Album B", "Artist B"),
    ]
    groups = build_groups(tracks)
    assert all(g.multi_album_parent for g in groups)


def test_ordinary_album_is_not_flagged():
    tracks = [
        _t("/music/Queen/Greatest Hits II/01.mp3", "Greatest Hits II", "Queen"),
        _t("/music/Queen/Greatest Hits II/02.mp3", "Greatest Hits II", "Queen"),
    ]
    (g,) = build_groups(tracks)
    assert g.multi_album_parent is False
    assert g.track_count == 2


def test_album_base_strips_disc_markers():
    assert _album_base("The Box Set Disc 1") == _album_base("The Box Set Disc 2")
    assert _album_base("Café del Mar Vol. 3") != _album_base("Café del Mar Vol. 7")


def test_multidisc_boxset_in_separate_folders_not_flagged():
    tracks = [
        _t("/music/BigBox/Disc 1/01.mp3", "Big Box Disc 1", "VA"),
        _t("/music/BigBox/Disc 2/01.mp3", "Big Box Disc 2", "VA"),
    ]
    assert not any(g.multi_album_parent for g in build_groups(tracks))


def test_incomplete_tags_detected():
    (g,) = build_groups([ScannedTrack(path="/music/loose/x.mp3", album="", album_artist="")])
    assert g.incomplete_tags is True


def test_same_tags_in_different_directories_are_never_one_write_group():
    groups = build_groups([
        _t("/music/VA/Cafe Del Mar Vol 3/01.mp3", "Café Del Mar Vol. 3", "Various Artists"),
        _t("/music/Imports/Cafe Del Mar Vol 3/01.mp3", "Café Del Mar Vol. 3", "Various Artists"),
    ])
    assert len(groups) == 2
    assert all(g.track_count == 1 for g in groups)
