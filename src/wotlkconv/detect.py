"""Working out what a file actually is.

Extensions are a hint, not proof: assets extracted by FileDataID arrive as
``1234567.unknown``, and ``.wmo`` covers both roots and groups.  Detection
therefore always looks at the bytes.
"""

from __future__ import annotations

import os

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

# What to do with a file, once its kind is known.
CONVERT = "convert"   # needs a structural downgrade
COPY = "copy"         # 3.3.5a reads it unchanged; copy it into the patch
SKIP = "skip"         # cannot be used by 3.3.5a, or is handled elsewhere

#: Kinds this tool converts.
CONVERTIBLE = {M2, SKIN, ANIM, BLP, WMO_ROOT, WMO_GROUP, ADT, WDT}

#: Extensions the 3.3.5a client reads as-is. A patch archive needs these just
#: as much as the converted files, so they are copied rather than dropped.
COPY_EXTENSIONS = {
    ".wav", ".mp3", ".ogg",                    # sound and music
    ".avi",                                    # cinematics
    ".lua", ".xml", ".toc", ".txt", ".html",   # interface
    ".ttf", ".otf",                            # fonts
    ".zmp",                                    # minimap block data
    ".sbt", ".wtf", ".ini",
}

#: Extensions that exist in modern builds but cannot be used by 3.3.5a, with
#: the reason, so the report says why rather than "unrecognised".
UNSUPPORTED_EXTENSIONS = {
    ".db2": "client databases; 3.3.5a reads .dbc, and converting between them "
            "needs per-table schemas this tool does not carry",
    ".tex": "Legion streamed high-resolution texture payloads; 3.3.5a loads "
            "whole BLPs instead",
    ".phys": "physics rigs; 3.3.5a has no model physics",
    ".bone": "Legion bone override files; no 3.3.5a equivalent",
    ".mp4": "3.3.5a plays .avi cinematics, not .mp4",
    ".wwise": "Wwise audio banks; 3.3.5a plays loose .wav/.mp3/.ogg",
    ".bnk": "Wwise audio banks; 3.3.5a plays loose .wav/.mp3/.ogg",
    ".wdl": "low-resolution terrain heightmaps; the 3.3.5a layout differs and "
            "this tool does not convert them yet",
    ".anim.skel": "unused",
}

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


def classify(kind: str, path: str = "") -> tuple[str, str]:
    """Decide what to do with a file, returning ``(action, reason)``.

    Kinds this tool understands are converted. Everything else is judged on its
    extension: formats 3.3.5a reads unchanged are copied through so a patch
    build is complete, and formats it cannot use are skipped with a reason
    that says which.
    """
    if kind == SKEL:
        return SKIP, ("skeletons are merged into the model that references "
                      "them, not converted on their own")
    if kind in CONVERTIBLE:
        return CONVERT, ""

    ext = os.path.splitext(path)[1].lower()
    if ext in UNSUPPORTED_EXTENSIONS:
        return SKIP, UNSUPPORTED_EXTENSIONS[ext]
    if ext in COPY_EXTENSIONS:
        return COPY, ""
    if kind == UNKNOWN and ext:
        # An unrecognised extension is more likely to be data 3.3.5a can read
        # than something harmful, so carry it through and say so.
        return COPY, f"unrecognised format {ext}; copied unchanged"
    return SKIP, "unrecognised format with no extension to judge it by"


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
