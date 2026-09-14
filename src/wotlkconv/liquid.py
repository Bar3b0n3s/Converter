"""Liquid volumes -- ``.wlw`` and ``.wlm`` (and ``.wlq``, which the
community listfile records none of, but which reads the same way).

These sit beside a map and describe the lakes, rivers and lava the terrain's
own liquid grid cannot: volumes with real shape, the ones the client tests
against when deciding whether you are swimming.  3.3.5a reads all three, so
unlike the LOD and lighting sidecars they belong in a patch.

What this module does is narrower than a conversion, deliberately.  The header
is well understood -- magic, version, liquid type, block count -- but the block
layout that follows is not documented well enough to rewrite safely, and a
liquid volume rewritten wrongly puts swimmable water where there is none.  So
the version is checked and the body is passed through untouched:

* a version 3.3.5a reads is already the file the old client wants, and is
  copied byte for byte;
* a newer one is refused by name, because a client that cannot parse the body
  is worse off with the file than without it.

If a build turns out to ship a version this refuses, that is the moment to
work out the block layout -- and the report will say so explicitly rather than
leaving a misparsed file to be discovered in-game.
"""

from __future__ import annotations

import struct
import time

from .errors import MalformedFileError, UnsupportedFormatError
from .options import Options
from .report import FileResult, Status

#: The signature, in both byte orders.  Liquid files are not chunked, so the
#: magic is written plainly -- but tools disagree about which way round it
#: reads, and accepting both costs nothing and misidentifies nothing else.
LIQUID_MAGICS = (b"LIQ*", b"*QIL")

#: Versions the 3.3.5a client parses.
WOTLK_LIQUID_VERSIONS = (0, 1)

HEADER_SIZE = 12

#: Smallest a block could conceivably be, used only to catch a count that
#: cannot possibly fit in the file.
MIN_BLOCK_SIZE = 8


def parse_header(data: bytes, name: str = "<liquid>") -> tuple[int, int, int]:
    """``(version, liquid_type, block_count)`` from a liquid volume."""
    if len(data) < HEADER_SIZE:
        raise MalformedFileError(
            f"{name}: file is {len(data)} bytes, too short for a liquid header")
    if data[:4] not in LIQUID_MAGICS:
        raise UnsupportedFormatError(
            f"{name}: not a liquid volume -- expected {LIQUID_MAGICS[0]!r}, "
            f"found {data[:4]!r}")
    version, liquid_type, blocks = struct.unpack_from("<HHI", data, 4)
    if blocks and HEADER_SIZE + blocks * MIN_BLOCK_SIZE > len(data):
        raise MalformedFileError(
            f"{name}: header claims {blocks} liquid block(s), which cannot fit "
            f"in {len(data)} bytes")
    return version, liquid_type, blocks


def convert_liquid(data: bytes, source_name: str, opts: Options,
                   result: FileResult | None = None) -> tuple[bytes, FileResult]:
    """Check a liquid volume is one 3.3.5a can read, and pass it through."""
    started = time.time()
    res = result or FileResult(source=source_name, kind="liquid")
    res.kind = "liquid"
    res.bytes_in = len(data)

    version, liquid_type, blocks = parse_header(data, source_name)
    res.source_version = f"liquid v{version}"

    if version not in WOTLK_LIQUID_VERSIONS:
        res.fail("liquid.version",
                 f"liquid volume version {version}; 3.3.5a reads "
                 f"{' and '.join(str(v) for v in WOTLK_LIQUID_VERSIONS)}. The "
                 f"block layout is not documented well enough to rewrite "
                 f"safely, and a volume rewritten wrongly puts swimmable water "
                 f"where there is none, so this file is refused rather than "
                 f"copied")
        res.elapsed = time.time() - started
        return b"", res

    res.status = Status.PASSTHROUGH
    res.target_version = f"liquid v{version}"
    res.bytes_out = len(data)
    res.extra.update({"version": version, "liquid_type": liquid_type,
                      "blocks": blocks})
    res.info("liquid.compatible",
             f"{blocks} liquid block(s), type {liquid_type}; version {version} "
             f"is what 3.3.5a reads, so the file is used unchanged")
    res.elapsed = time.time() - started
    return data, res


def inspect_liquid(data: bytes, source_name: str) -> dict:
    try:
        version, liquid_type, blocks = parse_header(data, source_name)
    except (MalformedFileError, UnsupportedFormatError):
        return {"kind": "liquid", "readable": False, "bytes": len(data)}
    return {
        "kind": "liquid",
        "version": version,
        "liquid_type": liquid_type,
        "blocks": blocks,
        "reads_in_wotlk": version in WOTLK_LIQUID_VERSIONS,
        "bytes": len(data),
    }
