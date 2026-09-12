from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.writer import _container_path, backup_files, restore_files


def test_backup_and_restore_round_trips_bytes():
    s = get_settings()
    album = s.music_root / "Artist" / "Album"
    album.mkdir(parents=True, exist_ok=True)
    originals = {}
    rel_paths = []
    for i in range(3):
        rel = f"/Artist/Album/0{i}.mp3"
        rel_paths.append(rel)
        data = b"ORIGINAL-%d-%s" % (i, b"x" * 50)
        (s.music_root / rel.lstrip("/")).write_bytes(data)
        originals[rel] = data

    backup_dir = backup_files(42, rel_paths)
    assert (Path(backup_dir) / "manifest.json").is_file()

    for rel in rel_paths:
        (s.music_root / rel.lstrip("/")).write_bytes(b"CORRUPTED")

    restored = restore_files(str(backup_dir))
    assert restored == 3
    for rel, data in originals.items():
        assert (s.music_root / rel.lstrip("/")).read_bytes() == data


def test_two_backups_same_second_do_not_collide():
    b1 = backup_files(7, ["/x/a.mp3"])
    b2 = backup_files(7, ["/x/a.mp3"])
    assert b1 != b2


def test_undo_removes_a_cover_jpg_that_did_not_exist_before():
    s = get_settings()
    d = s.music_root / "Band" / "Rec"
    d.mkdir(parents=True, exist_ok=True)
    (d / "01.mp3").write_bytes(b"orig")
    sidecar_rel = "/Band/Rec/cover.jpg"
    assert not _container_path(sidecar_rel).exists()

    backup_dir = backup_files(99, ["/Band/Rec/01.mp3"], sidecar_rel=sidecar_rel)
    _container_path(sidecar_rel).write_bytes(b"NEW COVER")   # simulate the apply

    restore_files(str(backup_dir))
    assert not _container_path(sidecar_rel).exists()          # undo removed our creation


def test_undo_restores_a_pre_existing_cover_jpg():
    s = get_settings()
    d = s.music_root / "Band2" / "Rec2"
    d.mkdir(parents=True, exist_ok=True)
    (d / "01.mp3").write_bytes(b"orig")
    sidecar_rel = "/Band2/Rec2/cover.jpg"
    _container_path(sidecar_rel).write_bytes(b"OLD COVER")

    backup_dir = backup_files(100, ["/Band2/Rec2/01.mp3"], sidecar_rel=sidecar_rel)
    _container_path(sidecar_rel).write_bytes(b"NEW COVER")

    restore_files(str(backup_dir))
    assert _container_path(sidecar_rel).read_bytes() == b"OLD COVER"
