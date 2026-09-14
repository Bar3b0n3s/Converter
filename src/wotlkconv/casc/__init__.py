"""Reading a local CASC install (Warlords of Draenor and later).

CASC replaced MPQ as World of Warcraft's storage format, and everything the
converter wants lives inside it.  This package implements enough of it to pull
files out by FileDataID: BLTE decoding, the local ``.idx`` indices, the
encoding table and the root table.

Only local installs are read; nothing is fetched from Blizzard's CDN.
"""

from .blte import EncryptedChunkError, decode as blte_decode
from .keys import KeyRing
from .storage import CascStorage, FileNotInstalledError, StorageStats

__all__ = [
    "CascStorage", "KeyRing", "StorageStats",
    "EncryptedChunkError", "FileNotInstalledError", "blte_decode",
]
