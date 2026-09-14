"""Terrain tiles.

Cataclysm split each ADT into four files -- ``Zone_32_48.adt`` for heights,
``_tex0`` for texture layers, ``_obj0`` for doodad and WMO placements, plus LOD
variants.  3.3.5a reads one monolithic tile with an ``MCIN`` index, which
Cataclysm dropped because it no longer needed it.

Merging them back means walking all 256 map chunks in lockstep across the three
files, reassembling each ``MCNK`` from pieces that now live in different
places, and recomputing every offset:

* ``MCNK`` sub-chunk offsets are relative to the start of the chunk *including*
  its 8-byte header, so they all move.
* ``MCRD`` (doodad refs) and ``MCRW`` (WMO refs) concatenate back into a single
  ``MCRF``, with the two counts written into the chunk header.
* ``MCIN`` has to be built from scratch.
* ``MHDR``'s offsets, which are relative to the start of its own payload, are
  rewritten to match the new layout.

Cataclysm's high-resolution 8x8 hole mask is folded down to the 4x4 mask Wrath
renders, and the chunks that only exist after Wrath are dropped.

One detail of ``MCIN`` has two readings.  Each entry gives the file offset of
an ``MCNK`` and a size, and the size is either the chunk's payload or that
payload plus the 8-byte chunk header.  The offset is unambiguous -- it points
at the header -- so a reader that seeks there and then trusts the chunk's own
size field, as the client does, cannot be misled either way; only a tool that
takes ``MCIN``'s size as the extent of the chunk can be.  For that reader the
header-inclusive value is the safe one: it spans the whole chunk, where the
payload-only value would stop 8 bytes short and cut off the end of the last
sub-chunk.  So that is the default.  It is not left as a guess, though: point
``--reference-adt`` at any genuine 3.3.5a tile and the convention is read off
it directly, by comparing each entry's size against the size the chunk itself
declares.
"""

from __future__ import annotations

import dataclasses
import pathlib
import struct
import time

from ..chunks import Chunk, ChunkReader, ChunkWriter
from ..errors import MalformedFileError, UnsupportedFormatError
from ..limits import ADT_MCNK_COUNT, ADT_VERSION
from ..listfile import Listfile, normalise
from ..options import Options, UnresolvedPolicy
from ..report import FileResult, Status

MCNK_HEADER_SIZE = 128
MCIN_ENTRY_SIZE = 16
MHDR_SIZE = 64

#: How much an MCIN entry's size field covers.  See the module docstring.
MCIN_SIZE_WITH_HEADER = "chunk"
MCIN_SIZE_PAYLOAD_ONLY = "payload"

#: MCNK.flags bit meaning "the 8 bytes at 0x40 are an 8x8 hole mask".
MCNK_FLAG_HIGH_RES_HOLES = 0x10000

#: MCNK sub-chunks 3.3.5a reads, in emission order.
SUBCHUNK_ORDER = ("MCVT", "MCCV", "MCNR", "MCLY", "MCRF", "MCSH", "MCAL",
                  "MCLQ", "MCSE")

#: Post-Wrath chunks, with what each one carries.
MODERN_CHUNKS = {
    "MTXP": "texture render parameters (height/parallax scale)",
    "MDID": "diffuse texture FileDataIDs",
    "MHID": "height texture FileDataIDs",
    "MAMP": "global texture amplifier",
    "MBMH": "blend mesh headers",
    "MBBB": "blend mesh bounding boxes",
    "MBNV": "blend mesh vertices",
    "MBMI": "blend mesh indices",
    "MLHD": "LOD header",
    "MLVH": "LOD heightmap",
    "MLVI": "LOD indices",
    "MLLL": "LOD levels",
    "MLND": "LOD quad tree",
    "MLSI": "LOD skirt indices",
    "MLLD": "LOD liquid data",
    "MLMD": "LOD map object definitions",
    "MLMX": "LOD map object extents",
    "MCLV": "per-chunk light values",
    "MCBB": "per-chunk blend batches",
    "MCMT": "per-chunk terrain material ids",
    "MCDD": "per-chunk detail doodad disable mask",
}

#: MHDR field order; the offsets are written back in this sequence.
MHDR_FIELDS = ("MCIN", "MTEX", "MMDX", "MMID", "MWMO", "MWID", "MDDF",
               "MODF", "MFBO", "MH2O", "MTXF")


@dataclasses.dataclass
class AdtParts:
    """The pieces of one tile, however the user extracted them."""

    root: bytes = b""
    tex0: bytes = b""
    obj0: bytes = b""

    def __bool__(self) -> bool:
        return bool(self.root)


def _collect(data: bytes, name: str) -> tuple[dict[str, list[Chunk]], list[Chunk]]:
    """Split a file into (non-MCNK chunks by name, MCNK chunks in order)."""
    if not data:
        return {}, []
    reader = ChunkReader.auto(data, {"MVER", "MHDR", "MCNK", "MTEX", "MCIN"}, name=name)
    named: dict[str, list[Chunk]] = {}
    mcnks: list[Chunk] = []
    for chunk in reader:
        if chunk.name == "MCNK":
            mcnks.append(chunk)
        else:
            named.setdefault(chunk.name, []).append(chunk)
    return named, mcnks


def _payload(named: dict[str, list[Chunk]], key: str) -> bytes:
    entries = named.get(key)
    return entries[0].data if entries else b""


def mcin_size_convention(data: bytes, name: str = "<adt>") -> tuple[str, str]:
    """Read off a real tile whether MCIN sizes include the chunk header.

    Every entry names an offset and a size, and the chunk sitting at that
    offset declares its own size eight bytes in.  Comparing the two says which
    convention wrote the file, with no interpretation left over.  Returns the
    convention and a sentence about the evidence, or ``("", why not)``.
    """
    reader = ChunkReader.auto(data, {"MVER", "MHDR", "MCNK", "MTEX", "MCIN"},
                              name=name)
    mcin = None
    for chunk in reader:
        if chunk.name == "MCIN":
            mcin = chunk
            break
    if mcin is None:
        return "", (f"{name} has no MCIN, so it is not a 3.3.5a tile "
                    f"(Cataclysm and later dropped the chunk)")

    votes = {MCIN_SIZE_WITH_HEADER: 0, MCIN_SIZE_PAYLOAD_ONLY: 0}
    checked = 0
    for i in range(min(ADT_MCNK_COUNT, len(mcin.data) // MCIN_ENTRY_SIZE)):
        offset, size = struct.unpack_from("<II", mcin.data, i * MCIN_ENTRY_SIZE)
        if not offset or not size or offset + 8 > len(data):
            continue
        declared = struct.unpack_from("<I", data, offset + 4)[0]
        checked += 1
        if size == declared + 8:
            votes[MCIN_SIZE_WITH_HEADER] += 1
        elif size == declared:
            votes[MCIN_SIZE_PAYLOAD_ONLY] += 1

    if not checked:
        return "", f"{name}'s MCIN entries are empty, so they settle nothing"
    winner = max(votes, key=lambda k: votes[k])
    if votes[winner] != checked:
        return "", (f"{name} is inconsistent with itself: of {checked} MCIN "
                    f"entries, {votes[MCIN_SIZE_WITH_HEADER]} include the "
                    f"chunk header in their size and "
                    f"{votes[MCIN_SIZE_PAYLOAD_ONLY]} do not")
    covers = ("the chunk header as well as the payload"
              if winner == MCIN_SIZE_WITH_HEADER else "the payload alone")
    return winner, (f"all {checked} of {name}'s MCIN entries size {covers}")


def _subchunks(data: bytes, reverse: bool, start: int = 0) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for chunk in ChunkReader(data, reverse=reverse, start=start):
        out.setdefault(chunk.name, chunk.data)
    return out


def _fold_holes(high: bytes) -> int:
    """Collapse Cataclysm's 8x8 hole mask into Wrath's 4x4 one."""
    low = 0
    for y in range(4):
        for x in range(4):
            block = 0
            for dy in range(2):
                row = high[y * 2 + dy] if y * 2 + dy < len(high) else 0
                for dx in range(2):
                    block |= (row >> (x * 2 + dx)) & 1
            if block:
                low |= 1 << (y * 4 + x)
    return low


def _resolve_textures(named: dict[str, list[Chunk]], opts: Options,
                      listfile: Listfile, result: FileResult) -> bytes:
    """Return an MTEX blob, building one from MDID FileDataIDs if needed."""
    mtex = _payload(named, "MTEX")
    if mtex:
        if not opts.path_prefix:
            return mtex
        rebuilt = bytearray()
        for raw in mtex.split(b"\0"):
            if not raw:
                continue
            path = normalise(opts.path_prefix.rstrip("\\/") + "\\"
                             + raw.decode("latin-1"))
            rebuilt += path.encode("latin-1") + b"\0"
        return bytes(rebuilt)

    mdid = _payload(named, "MDID")
    if not mdid:
        return b""

    ids = struct.unpack_from("<" + "I" * (len(mdid) // 4), mdid, 0)
    blob = bytearray()
    for file_id in ids:
        path = listfile.path_for(file_id) if file_id else ""
        if path is None:
            if opts.unresolved is UnresolvedPolicy.FAIL:
                result.fail("adt.texture.unresolved",
                            f"no listfile entry for terrain texture FileDataID "
                            f"{file_id}", file_id=file_id)
                return b""
            path = normalise(f"unresolved/blp/{file_id}.blp")
            result.lossy("adt.texture.placeholder",
                         f"terrain texture FileDataID {file_id} is not in the "
                         f"listfile; pointed it at {path}", file_id=file_id)
        if path and opts.path_prefix:
            path = normalise(opts.path_prefix.rstrip("\\/") + "\\" + path)
        blob += path.encode("latin-1") + b"\0"
    result.info("adt.texture.resolved",
                f"rebuilt MTEX from {len(ids)} MDID FileDataID(s)", count=len(ids))
    return bytes(blob)


def _build_mcnk(header: bytes, pieces: dict[str, bytes], reverse: bool,
                result: FileResult, counters: dict[str, int]) -> bytes:
    """Reassemble one map chunk with 3.3.5a offsets."""
    hdr = bytearray(header[:MCNK_HEADER_SIZE])
    flags = struct.unpack_from("<I", hdr, 0)[0]

    if flags & MCNK_FLAG_HIGH_RES_HOLES:
        low = _fold_holes(bytes(hdr[0x40:0x48]))
        struct.pack_into("<H", hdr, 0x3C, low)
        # Wrath reads those bytes as the low-quality texture map instead.
        hdr[0x40:0x50] = b"\0" * 16
        flags &= ~MCNK_FLAG_HIGH_RES_HOLES
        counters["holes"] = counters.get("holes", 0) + 1
    struct.pack_into("<I", hdr, 0, flags)

    layers = len(pieces.get("MCLY", b"")) // 16
    doodad_refs = len(pieces.get("MCRD", b"")) // 4
    object_refs = len(pieces.get("MCRW", b"")) // 4
    mcrf = pieces.get("MCRF")
    if mcrf is None:
        mcrf = pieces.get("MCRD", b"") + pieces.get("MCRW", b"")
    else:
        # A monolithic source already merged them; trust the header's counts.
        doodad_refs = struct.unpack_from("<I", hdr, 0x10)[0]
        object_refs = struct.unpack_from("<I", hdr, 0x38)[0]

    struct.pack_into("<I", hdr, 0x0C, layers)
    struct.pack_into("<I", hdr, 0x10, doodad_refs)
    struct.pack_into("<I", hdr, 0x38, object_refs)

    body = ChunkWriter(reverse=reverse)
    offsets: dict[str, int] = {}
    sizes: dict[str, int] = {}
    emitted = {"MCRF": mcrf} | {k: v for k, v in pieces.items()
                                if k in SUBCHUNK_ORDER and k != "MCRF"}
    for name in SUBCHUNK_ORDER:
        payload = emitted.get(name)
        if payload is None or (name != "MCRF" and not payload):
            continue
        # Offsets count from the start of the MCNK chunk header, so allow for
        # the 8 bytes of magic+size plus the 128-byte chunk header.
        offsets[name] = 8 + MCNK_HEADER_SIZE + len(body)
        sizes[name] = len(payload) + 8
        body.add(name, payload)

    struct.pack_into("<I", hdr, 0x14, offsets.get("MCVT", 0))
    struct.pack_into("<I", hdr, 0x18, offsets.get("MCNR", 0))
    struct.pack_into("<I", hdr, 0x1C, offsets.get("MCLY", 0))
    struct.pack_into("<I", hdr, 0x20, offsets.get("MCRF", 0))
    struct.pack_into("<I", hdr, 0x24, offsets.get("MCAL", 0))
    struct.pack_into("<I", hdr, 0x28, sizes.get("MCAL", 0))
    struct.pack_into("<I", hdr, 0x2C, offsets.get("MCSH", 0))
    struct.pack_into("<I", hdr, 0x30, sizes.get("MCSH", 0))
    struct.pack_into("<I", hdr, 0x58, offsets.get("MCSE", 0))
    struct.pack_into("<I", hdr, 0x5C, len(pieces.get("MCSE", b"")) // 28)
    struct.pack_into("<I", hdr, 0x60, offsets.get("MCLQ", 0))
    # A chunk with no liquid still declares the empty chunk's 8 header bytes.
    struct.pack_into("<I", hdr, 0x64, sizes.get("MCLQ", 8))
    struct.pack_into("<I", hdr, 0x74, offsets.get("MCCV", 0))
    # Wrath has no MCLV and ignores the last word; Cataclysm used both.
    struct.pack_into("<II", hdr, 0x78, 0, 0)

    return bytes(hdr) + body.getvalue()


def _learn_mcin(path: str, res: FileResult) -> tuple[str, str]:
    """Read the MCIN convention off a reference tile, if it can be read."""
    try:
        data = pathlib.Path(path).read_bytes()
    except OSError as exc:
        return "", f"could not read the reference tile {path}: {exc}"
    try:
        return mcin_size_convention(data, pathlib.Path(path).name)
    except (MalformedFileError, UnsupportedFormatError, struct.error) as exc:
        return "", f"could not read {path} as an ADT: {exc}"


def convert_adt(parts: AdtParts, source_name: str, opts: Options,
                listfile: Listfile | None = None,
                result: FileResult | None = None) -> tuple[bytes, FileResult]:
    """Merge a tile's pieces into one 3.3.5a ADT."""
    started = time.time()
    res = result or FileResult(source=source_name, kind="adt")
    res.kind = "adt"
    res.bytes_in = len(parts.root) + len(parts.tex0) + len(parts.obj0)
    listfile = listfile or Listfile()

    if not parts.root:
        raise MalformedFileError(f"{source_name}: no terrain (root) ADT supplied")

    mcin_convention = MCIN_SIZE_WITH_HEADER
    if opts.adt_reference:
        learned, why = _learn_mcin(opts.adt_reference, res)
        if learned:
            mcin_convention = learned
            res.info("adt.mcin.learned",
                     f"MCIN entry sizes follow the reference tile: {why}")
        elif why:
            res.warn("adt.mcin.reference_unusable",
                     f"{why}; kept the header-inclusive size, which is safe "
                     f"for a reader that trusts MCIN over the chunk's own "
                     f"header")

    root_named, root_mcnks = _collect(parts.root, source_name)
    tex_named, tex_mcnks = _collect(parts.tex0, source_name + "_tex0")
    obj_named, obj_mcnks = _collect(parts.obj0, source_name + "_obj0")
    reverse = True  # ADT magics are stored byte-reversed

    if "MHDR" not in root_named:
        raise UnsupportedFormatError(f"{source_name}: not an ADT (no MHDR chunk)")
    if not root_mcnks:
        raise MalformedFileError(f"{source_name}: root ADT has no MCNK chunks")

    split_source = bool(parts.tex0 or parts.obj0) or "MTEX" not in root_named
    res.source_version = (f"ADT {'split' if split_source else 'monolithic'}, "
                          f"{len(root_mcnks)} map chunks")

    if len(root_mcnks) != ADT_MCNK_COUNT:
        res.warn("adt.mcnk_count",
                 f"tile has {len(root_mcnks)} map chunks, not {ADT_MCNK_COUNT}",
                 chunks=len(root_mcnks))

    modern = sorted({n for n in (*root_named, *tex_named, *obj_named)
                     if n in MODERN_CHUNKS})

    # -- referenced tables ----------------------------------------------
    def pick(name: str) -> bytes:
        for named in (obj_named, tex_named, root_named):
            payload = _payload(named, name)
            if payload:
                return payload
        return b""

    mtex = _resolve_textures(tex_named or root_named, opts, listfile, res)
    if not res.ok:
        res.elapsed = time.time() - started
        return b"", res

    tables = {
        "MTEX": mtex,
        "MMDX": pick("MMDX"),
        "MMID": pick("MMID"),
        "MWMO": pick("MWMO"),
        "MWID": pick("MWID"),
        "MDDF": pick("MDDF"),
        "MODF": pick("MODF"),
        "MH2O": pick("MH2O"),
        "MFBO": pick("MFBO"),
        "MTXF": pick("MTXF"),
    }

    # -- map chunks ------------------------------------------------------
    counters: dict[str, int] = {}
    merged_mcnks: list[bytes] = []
    for index, chunk in enumerate(root_mcnks):
        pieces = _subchunks(chunk.data, reverse, start=MCNK_HEADER_SIZE)
        if index < len(tex_mcnks):
            pieces |= _subchunks(tex_mcnks[index].data, reverse)
        if index < len(obj_mcnks):
            pieces |= _subchunks(obj_mcnks[index].data, reverse)
        merged_mcnks.append(
            _build_mcnk(chunk.data, pieces, reverse, res, counters))

    if counters.get("holes"):
        res.lossy("adt.holes",
                  f"folded the 8x8 hole mask down to 4x4 on "
                  f"{counters['holes']} map chunk(s); 3.3.5a cannot punch "
                  f"sub-quadrant holes", chunks=counters["holes"])

    # -- assemble --------------------------------------------------------
    out = bytearray()
    top = ChunkWriter(reverse=reverse)
    top.add("MVER", struct.pack("<I", ADT_VERSION))
    out += top.getvalue()

    mhdr_source = _payload(root_named, "MHDR")
    mhdr = bytearray(mhdr_source[:MHDR_SIZE].ljust(MHDR_SIZE, b"\0"))
    out += b"RDHM" if reverse else b"MHDR"
    out += struct.pack("<I", MHDR_SIZE)
    mhdr_data_pos = len(out)
    out += bytes(mhdr)

    offsets: dict[str, int] = {}

    def append(name: str, payload: bytes, always: bool = False) -> None:
        if not payload and not always:
            return
        offsets[name] = len(out) - mhdr_data_pos
        out.extend((name[::-1] if reverse else name).encode("latin-1"))
        out.extend(struct.pack("<I", len(payload)))
        out.extend(payload)

    # MCIN is written first but only filled in once the chunks are placed.
    mcin_payload = bytearray(ADT_MCNK_COUNT * MCIN_ENTRY_SIZE)
    append("MCIN", bytes(mcin_payload), always=True)
    mcin_data_pos = len(out) - len(mcin_payload)

    for name in ("MTEX", "MMDX", "MMID", "MWMO", "MWID", "MDDF", "MODF"):
        append(name, tables[name], always=name in ("MTEX", "MMDX", "MMID",
                                                   "MWMO", "MWID"))
    append("MH2O", tables["MH2O"])

    for index, payload in enumerate(merged_mcnks):
        chunk_pos = len(out)
        out.extend((b"KNCM" if reverse else b"MCNK"))
        out.extend(struct.pack("<I", len(payload)))
        out.extend(payload)
        if index < ADT_MCNK_COUNT:
            entry_size = len(payload) + (8 if mcin_convention ==
                                         MCIN_SIZE_WITH_HEADER else 0)
            struct.pack_into("<4I", out, mcin_data_pos + index * MCIN_ENTRY_SIZE,
                             chunk_pos, entry_size, 0, 0)

    append("MFBO", tables["MFBO"])
    append("MTXF", tables["MTXF"])

    flags = struct.unpack_from("<I", mhdr, 0)[0]
    if tables["MFBO"]:
        flags |= 0x1
    else:
        flags &= ~0x1
    struct.pack_into("<I", mhdr, 0, flags)
    for i, name in enumerate(MHDR_FIELDS):
        struct.pack_into("<I", mhdr, 4 + i * 4, offsets.get(name, 0))
    out[mhdr_data_pos : mhdr_data_pos + MHDR_SIZE] = mhdr

    if modern:
        res.lossy("adt.chunks.dropped",
                  "dropped chunks with no 3.3.5a equivalent: "
                  + ", ".join(f"{n} ({MODERN_CHUNKS[n]})" for n in modern
                              if n not in ("MDID",)),
                  chunks=modern)
    if split_source:
        res.warn("adt.big_alpha",
                 "this tile's MCAL alpha maps are the 8-bit Cataclysm form; the "
                 "map's .wdt must have the big-alpha flag (MPHD 0x4) set or "
                 "3.3.5a reads them as 4-bit and every terrain blend is wrong. "
                 "Converting the .wdt in the same run sets it for you")
        res.info("adt.merged",
                 f"merged {'root' if parts.root else ''}"
                 f"{'+tex0' if parts.tex0 else ''}"
                 f"{'+obj0' if parts.obj0 else ''} into one monolithic tile "
                 f"with a rebuilt MCIN index")

    res.bytes_out = len(out)
    res.target_version = f"ADT v{ADT_VERSION} monolithic, {len(merged_mcnks)} map chunks"
    res.extra.update({"map_chunks": len(merged_mcnks),
                      "textures": tables["MTEX"].count(b"\0"),
                      "doodad_placements": len(tables["MDDF"]) // 36,
                      "wmo_placements": len(tables["MODF"]) // 64})
    if not split_source and not modern and res.status is Status.OK:
        res.status = Status.PASSTHROUGH
        res.info("adt.passthrough", "already a monolithic 3.3.5a tile")
    res.elapsed = time.time() - started
    return bytes(out), res


def inspect_adt(data: bytes, source_name: str) -> dict:
    named, mcnks = _collect(data, source_name)
    return {
        "kind": "adt",
        "chunks": sorted(named),
        "map_chunks": len(mcnks),
        "split": "MHDR" in named and "MTEX" not in named,
        "modern_chunks": sorted(n for n in named if n in MODERN_CHUNKS) or None,
        "wotlk_compatible": "MCIN" in named and not any(n in MODERN_CHUNKS
                                                        for n in named),
    }
