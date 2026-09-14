"""Working out what a file actually is.

Extensions are a hint, not proof: assets extracted by FileDataID arrive as
``1234567.unknown``, and ``.wmo`` covers both roots and groups.  Detection
therefore always looks at the bytes.
"""

from __future__ import annotations

import os
import struct

from .chunks import ChunkReader

M2 = "m2"
SKIN = "skin"
ANIM = "anim"
SKEL = "skel"
BLP = "blp"
WMO_ROOT = "wmo"
WMO_GROUP = "wmo-group"
ADT = "adt"
WDT = "wdt"
UNKNOWN = "unknown"

#: Output extension for each kind.
EXTENSIONS = {
    M2: ".m2", SKIN: ".skin", ANIM: ".anim", SKEL: ".skel", BLP: ".blp",
    WMO_ROOT: ".wmo", WMO_GROUP: ".wmo", ADT: ".adt", WDT: ".wdt",
}

#: Extensions that name a Cataclysm-and-later split terrain file.
SPLIT_ADT_SUFFIXES = ("_obj0", "_obj1", "_tex0", "_tex1", "_lod")


def _chunk_names(data: bytes, limit: int = 6) -> list[str]:
    names: list[str] = []
    try:
        for chunk in ChunkReader(data, reverse=True):
            names.append(chunk.name)
            if len(names) >= limit:
                break
    except Exception:  # noqa: BLE001 - detection must never raise
        pass
    return names


def detect(data: bytes, path: str = "") -> str:
    """Classify a buffer. ``path`` only breaks ties detection cannot."""
    if len(data) < 8:
        return UNKNOWN

    head = data[:4]
    if head == b"MD20":
        return M2
    if head == b"MD21":
        return M2
    if head == b"SKIN":
        return SKIN
    if head == b"BLP2" or head == b"BLP1":
        return BLP
    if head in (b"AFM2", b"AFSA", b"AFSB"):
        return ANIM
    if head in (b"SKL1", b"SKB1", b"SKA1"):
        return SKEL

    names = _chunk_names(data)
    if names:
        if "MOHD" in names:
            return WMO_ROOT
        if "MOGP" in names:
            return WMO_GROUP
        if "MHDR" in names or "MCIN" in names or "MCNK" in names:
            return ADT
        if "MPHD" in names or "MAIN" in names:
            return WDT
        if names[0] == "MVER" and len(names) == 1:
            # MVER-only files are split ADT pieces whose payload chunks follow.
            return ADT

    ext = os.path.splitext(path)[1].lower()
    if ext == ".anim":
        return ANIM
    if ext == ".adt":
        return ADT
    if ext == ".skel":
        return SKEL
    return UNKNOWN


def is_split_adt(path: str) -> bool:
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    return stem.endswith(SPLIT_ADT_SUFFIXES)


def adt_base_name(path: str) -> str:
    """``Azeroth_32_48_obj0.adt`` -> ``Azeroth_32_48``."""
    stem = os.path.splitext(os.path.basename(path))[0]
    lowered = stem.lower()
    for suffix in SPLIT_ADT_SUFFIXES:
        if lowered.endswith(suffix):
            return stem[: -len(suffix)]
    return stem
