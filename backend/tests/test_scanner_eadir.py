from __future__ import annotations

from pathlib import Path

from app.scanner import iter_audio_files


def test_eadir_entries_are_never_scanned(tmp_path: Path):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 - track.mp3").write_bytes(b"ID3fake")
    (album / "02 - track.mp3").write_bytes(b"ID3fake")

    # Synology sidecar: a directory literally named @eaDir holding *.mp3 entries
    eadir = album / "@eaDir" / "01 - track.mp3"
    eadir.mkdir(parents=True)
    (eadir / "SYNOPHOTO_THUMB_XL.jpg").write_bytes(b"x")
    (album / "@eaDir" / "02 - track.mp3").mkdir()

    found = {p.name for p in iter_audio_files(tmp_path, (".mp3",))}
    assert found == {"01 - track.mp3", "02 - track.mp3"}
    assert all("@eaDir" not in p.parts for p in iter_audio_files(tmp_path, (".mp3",)))


def test_non_audio_and_directories_ignored(tmp_path: Path):
    d = tmp_path / "x"
    d.mkdir()
    (d / "cover.jpg").write_bytes(b"x")
    (d / "notes.txt").write_bytes(b"x")
    (d / "song.mp3").write_bytes(b"x")
    assert [p.name for p in iter_audio_files(tmp_path, (".mp3",))] == ["song.mp3"]
