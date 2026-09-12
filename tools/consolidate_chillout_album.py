"""Create a safely backed-up, single-album compilation from /music/Chillout."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TCMP, TPOS, TPE2, TRCK
from PIL import Image

ROOT = Path("/music/Chillout")
ART = Path("/tmp/night-drift-chillout-archive.png")
BACKUPS = Path("/config/backups")
ALBUM = "Night Drift: Chillout Archive"
ALBUM_ARTIST = "Various Artists"


def jpeg(raw: bytes) -> bytes:
    image = Image.open(BytesIO(raw)).convert("RGB")
    image.thumbnail((1400, 1400))
    result = BytesIO()
    image.save(result, format="JPEG", quality=92, optimize=True)
    return result.getvalue()


def main() -> None:
    # Synology's @eaDir sidecars can themselves end in .mp3; never treat them as audio.
    files = sorted((p for p in ROOT.rglob("*.mp3") if p.is_file()), key=lambda p: str(p).casefold())
    if not files:
        raise SystemExit(f"No MP3 files found under {ROOT}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    backup = BACKUPS / f"night-drift-chillout-archive-{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    manifest = {"album": ALBUM, "album_artist": ALBUM_ARTIST, "files": []}
    # Full byte-for-byte originals make this bulk metadata change recoverable.
    for source in files:
        relative = source.relative_to(ROOT)
        saved = backup / "originals" / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, saved)
        manifest["files"].append({"path": str(relative), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    cover = jpeg(ART.read_bytes())
    total = len(files)
    for number, source in enumerate(files, 1):
        try:
            tags = ID3(source)
        except ID3NoHeaderError:
            tags = ID3()
        for frame in ("TALB", "TPE2", "TPOS", "TRCK", "TCMP", "APIC"):
            tags.delall(frame)
        tags.add(TALB(encoding=3, text=ALBUM))
        tags.add(TPE2(encoding=3, text=ALBUM_ARTIST))
        tags.add(TPOS(encoding=3, text="1/1"))
        tags.add(TRCK(encoding=3, text=f"{number}/{total}"))
        tags.add(TCMP(encoding=3, text="1"))
        # ID3v2.3 + front-cover APIC is the broadly compatible Windows Media Player form.
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover (Front)", data=cover))
        tags.save(source, v2_version=3)

    for source in files:
        tags = ID3(source)
        apics = [frame for frame in tags.getall("APIC") if frame.type == 3 and frame.data]
        if str(tags.get("TALB", "")) != ALBUM or str(tags.get("TPE2", "")) != ALBUM_ARTIST or not apics:
            raise RuntimeError(f"Validation failed: {source}")
    print(json.dumps({"updated": total, "album": ALBUM, "backup": str(backup), "cover_bytes": len(cover)}))


if __name__ == "__main__":
    main()
