"""Runtime configuration, loaded once from environment + secret files.

Secrets are read from files (never echoed to logs or API responses).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _split_exts(raw: str) -> tuple[str, ...]:
    return tuple(
        e if e.startswith(".") else f".{e}"
        for e in (p.strip().lower() for p in raw.split(","))
        if e
    )


@dataclass(frozen=True)
class Settings:
    music_root: Path
    data_dir: Path
    ma_ws_url: str
    ma_token_file: Path
    plex_url: str
    plex_token_file: Path
    plex_path_maps: tuple[tuple[str, str], ...]
    audio_extensions: tuple[str, ...]
    http_user_agent: str
    # per-domain minimum seconds between requests
    rate_limits: dict[str, float] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "artwork_recovery.sqlite3"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def image_cache_dir(self) -> Path:
        return self.data_dir / "image_cache"

    @property
    def static_dir(self) -> Path:
        # built frontend, bundled into the image at ./app/static
        return Path(__file__).resolve().parent / "static"

    def ma_token(self) -> str | None:
        env = os.environ.get("MA_TOKEN")
        if env:
            return env.strip()
        try:
            return self.ma_token_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def plex_token(self) -> str | None:
        """Return the Plex token without ever exposing it through the API/logs."""
        env = os.environ.get("PLEX_TOKEN")
        if env:
            return env.strip()
        try:
            return self.plex_token_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.backups_dir, self.image_cache_dir,
                  self.ma_token_file.parent):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("ARTWORK_DATA_DIR", "/config")).resolve()
    raw_maps = os.environ.get("PLEX_PATH_MAPS", "")
    path_maps: list[tuple[str, str]] = []
    for item in raw_maps.split(","):
        if "=" not in item:
            continue
        local, plex = (part.strip().rstrip("/") or "/" for part in item.split("=", 1))
        path_maps.append((local, plex))
    return Settings(
        music_root=Path(os.environ.get("MUSIC_ROOT", "/music")).resolve(),
        data_dir=data_dir,
        ma_ws_url=os.environ.get("MA_WS_URL", "ws://192.168.0.120:8095/ws"),
        ma_token_file=Path(
            os.environ.get("MA_TOKEN_FILE", str(data_dir / "secrets" / "ma_token"))
        ),
        plex_url=os.environ.get("PLEX_URL", "").strip().rstrip("/"),
        plex_token_file=Path(
            os.environ.get("PLEX_TOKEN_FILE", str(data_dir / "secrets" / "plex_token"))
        ),
        plex_path_maps=tuple(path_maps),
        audio_extensions=_split_exts(
            os.environ.get("AUDIO_EXTENSIONS", ".mp3,.m4a,.flac,.ogg,.opus")
        ),
        http_user_agent=os.environ.get(
            "HTTP_USER_AGENT", "artwork-recovery/0.1 (+homelab; music-assistant art repair)"
        ),
        rate_limits={
            "musicbrainz.org": 1.1,          # MB asks for <=1 req/s
            "coverartarchive.org": 1.1,
            "itunes.apple.com": 0.5,
            "api.deezer.com": 0.5,
            "_default": 0.25,
        },
    )
