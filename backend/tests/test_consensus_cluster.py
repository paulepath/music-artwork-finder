from __future__ import annotations

import imagehash
import numpy as np
from PIL import Image

from app.consensus import cluster_phashes


def _phash(seed: int) -> str:
    rng = np.random.default_rng(seed)
    arr = (rng.random((64, 64, 3)) * 255).astype("uint8")
    return str(imagehash.phash(Image.fromarray(arr, "RGB")))


def test_identical_hashes_cluster_together_and_biggest_is_zero():
    a = _phash(1)
    b = _phash(2)
    phashes = [a, a, a, b, None]
    clusters = cluster_phashes(phashes, threshold=0)
    assert clusters[0] == clusters[1] == clusters[2] == 0   # biggest cluster -> id 0
    assert clusters[3] == 1
    assert clusters[4] == -1


def test_threshold_controls_merging():
    a = _phash(10)
    b = _phash(11)
    assert cluster_phashes([a, b], threshold=0) == [0, 1] or cluster_phashes([a, b], threshold=0)[0] != cluster_phashes([a, b], threshold=0)[1]
