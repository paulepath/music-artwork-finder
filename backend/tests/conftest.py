from __future__ import annotations

import os
import tempfile

# Configure the app for an isolated, throwaway environment BEFORE app imports.
_tmp = tempfile.mkdtemp(prefix="artwork-test-")
os.environ.setdefault("MUSIC_ROOT", os.path.join(_tmp, "music"))
os.environ.setdefault("ARTWORK_DATA_DIR", os.path.join(_tmp, "data"))
os.makedirs(os.environ["MUSIC_ROOT"], exist_ok=True)
os.makedirs(os.environ["ARTWORK_DATA_DIR"], exist_ok=True)
