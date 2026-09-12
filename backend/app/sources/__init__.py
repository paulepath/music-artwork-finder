from .base import CandidateData, Source, TRUSTED_SOURCES
from .musicbrainz import MusicBrainzSource
from .itunes import ITunesSource
from .deezer import DeezerSource
from .plex import PlexSource

__all__ = [
    "CandidateData", "Source", "TRUSTED_SOURCES",
    "MusicBrainzSource", "ITunesSource", "DeezerSource", "PlexSource",
]
