"""Download candidate images, hash them, and cluster near-duplicates.

Used both to validate trusted-source images (real, square-ish, big enough) and to
find the *consensus* cover among many Google Images results: the biggest cluster of
perceptually-identical images is almost always the correct cover.
"""
from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass

import httpx
import imagehash
from PIL import Image, UnidentifiedImageError

from .config import get_settings
from .http import get_bytes

log = logging.getLogger("artwork.consensus")
_settings = get_settings()
PHASH_SIZE = 8
CLUSTER_THRESHOLD = 8       # max Hamming distance to be "the same image"
MIN_EDGE = 250


@dataclass
class FetchedImage:
    ok: bool
    sha256: str = ""
    phash: str = ""
    width: int = 0
    height: int = 0
    cache_path: str = ""
    error: str = ""


async def fetch_and_hash(
    client: httpx.AsyncClient, url: str, *, headers: dict[str, str] | None = None,
) -> FetchedImage:
    try:
        raw, _ctype = await get_bytes(client, url, headers=headers)
    except (httpx.HTTPError, OSError) as e:
        return FetchedImage(False, error=f"download failed: {e}")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError) as e:
        return FetchedImage(False, error=f"not an image: {e}")

    w, h = img.size
    if min(w, h) < MIN_EDGE:
        return FetchedImage(False, width=w, height=h, error=f"too small ({w}x{h})")
    if not 0.8 <= (w / h) <= 1.25:
        return FetchedImage(False, width=w, height=h, error=f"not square-ish ({w}x{h})")

    sha = hashlib.sha256(raw).hexdigest()
    ph = str(imagehash.phash(img.convert("RGB"), hash_size=PHASH_SIZE))

    _settings.image_cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = _settings.image_cache_dir / f"{sha}.img"
    if not cache_path.exists():
        cache_path.write_bytes(raw)
    return FetchedImage(True, sha256=sha, phash=ph, width=w, height=h,
                        cache_path=str(cache_path))


def _hamming(a: str, b: str) -> int:
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def cluster_phashes(phashes: list[str | None], threshold: int = CLUSTER_THRESHOLD) -> list[int]:
    """Union-find clustering. Returns a cluster id per input (``-1`` for missing hash)."""
    n = len(phashes)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    for i in range(n):
        if phashes[i] is None:
            continue
        for j in range(i + 1, n):
            if phashes[j] is None:
                continue
            if _hamming(phashes[i], phashes[j]) <= threshold:
                union(i, j)

    # normalise ids to 0..k, ordered by descending cluster size
    raw = [find(i) if phashes[i] is not None else -1 for i in range(n)]
    sizes: dict[int, int] = {}
    for r in raw:
        if r >= 0:
            sizes[r] = sizes.get(r, 0) + 1
    order = {root: idx for idx, (root, _) in
             enumerate(sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0])))}
    return [order.get(r, -1) for r in raw]
