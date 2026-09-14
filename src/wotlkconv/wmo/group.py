"""WMO group files -- the actual geometry.

A group is ``MVER`` plus one ``MOGP`` whose payload is a 68-byte header
followed by sub-chunks.  The header size never changed, so the work is in the
sub-chunks:

* ``MOVX`` (32-bit indices) has to become ``MOVI`` (16-bit), which is only
  possible if the group stays under 65536 vertices.
* ``MPY2`` (16-bit material ids) has to become ``MOPY`` (8-bit).
* Extra ``MOTV``/``MOCV`` layers beyond the two the old renderer binds are
  dropped, and the header flags that advertise them are corrected to match.
* Shadowlands reused ``MOBA``'s bounding-box bytes for a wide material id, so
  when that layout is detected the boxes are recomputed from the geometry
  rather than left as garbage the client would cull against.
"""

from __future__ import annotations

import dataclasses
import struct

from ..chunks import Chunk, ChunkReader, ChunkWriter
from ..errors import MalformedFileError, UnsupportedFormatError
from ..limits import (
    MOBA_SIZE,
    MOGP_HEADER_SIZE,
    WMO_GROUP_CHUNKS_KNOWN,
    WMO_GROUP_FLAG_HAS_TWO_MOCV,
    WMO_GROUP_FLAG_HAS_TWO_MOTV,
    WMO_GROUP_FLAG_HAS_VERTEX_COLORS,
    WMO_GROUP_FLAG_MASK,
    WMO_MAX_COLOR_LAYERS,
    WMO_MAX_GROUP_VERTICES,
    WMO_MAX_UV_LAYERS,
    WMO_VERSION,
)
from ..options import Options
from ..report import FileResult, Status

#: Sub-chunks 3.3.5a reads, in emission order.
GROUP_SUBCHUNK_ORDER = (
    "MOPY", "MOVI", "MOVT", "MONR", "MOTV", "MOBA", "MOLR", "MODR",
    "MOBN", "MOBR", "MOCV", "MLIQ",
)

#: Post-Wrath group sub-chunks, with what each one carries.
MODERN_GROUP_CHUNKS = {
    "MOVX": "32-bit triangle indices",
    "MPY2": "16-bit material references",
    "MOC2": "extended vertex colours",
    "MOLS": "spot lights",
    "MOLP": "point lights",
    "MLSS": "light set spline",
    "MLSK": "light set skybox",
    "MLSO": "spot light animation",
    "MLSP": "light spline points",
    "MDAL": "doodad ambient light",
    "MOPL": "terrain cutting planes",
    "MOPB": "prepass batches",
    "MOBS": "shadow batches",
    "MOTA": "tangent arrays",
    "MOQG": "query face flags",
    "MOLM": "lightmap texels",
    "MOLD": "lightmap definitions",
    "MOAM": "ambient colours",
    "MDAI": "doodad ambient",
    "MPBV": "prepass bounds",
    "MPBP": "prepass portals",
    "MPBI": "prepass indices",
    "MPBG": "prepass groups",
}

ALL_KNOWN = set(WMO_GROUP_CHUNKS_KNOWN) | set(MODERN_GROUP_CHUNKS)


@dataclasses.dataclass
class WmoGroup:
    version: int = WMO_VERSION
    header: bytearray = dataclasses.field(default_factory=lambda: bytearray(MOGP_HEADER_SIZE))
    subchunks: list[Chunk] = dataclasses.field(default_factory=list)
    reverse_magic: bool = True

    @property
    def flags(self) -> int:
        return struct.unpack_from("<I", self.header, 8)[0]

    @flags.setter
    def flags(self, value: int) -> None:
        struct.pack_into("<I", self.header, 8, value & 0xFFFFFFFF)

    @property
    def flags2(self) -> int:
        return struct.unpack_from("<I", self.header, 0x3C)[0]

    def by_name(self, name: str) -> list[Chunk]:
        return [c for c in self.subchunks if c.name == name]


def parse_group(data: bytes, name: str = "<wmo group>") -> WmoGroup:
    if len(data) < 8:
        raise MalformedFileError(f"{name}: file is only {len(data)} bytes")
    reader = ChunkReader.auto(data, {"MVER", "MOGP"}, name=name)
    group = WmoGroup(reverse_magic=reader.reverse)
    mogp: Chunk | None = None
    for chunk in reader:
        if chunk.name == "MVER" and len(chunk.data) >= 4:
            group.version = struct.unpack_from("<I", chunk.data, 0)[0]
        elif chunk.name == "MOGP":
            mogp = chunk
    if mogp is None:
        raise UnsupportedFormatError(f"{name}: not a WMO group (no MOGP chunk)")
    if len(mogp.data) < MOGP_HEADER_SIZE:
        raise MalformedFileError(
            f"{name}: MOGP is {len(mogp.data)} bytes, header alone needs "
            f"{MOGP_HEADER_SIZE}")

    group.header = bytearray(mogp.data[:MOGP_HEADER_SIZE])
    inner = ChunkReader(mogp.data, reverse=group.reverse_magic, name=name,
                        start=MOGP_HEADER_SIZE)
    group.subchunks = list(inner)
    return group


# ---------------------------------------------------------------------------
# Sub-chunk downgrades
# ---------------------------------------------------------------------------
def _convert_indices(payload: bytes, result: FileResult, name: str) -> bytes | None:
    """MOVX (uint32) -> MOVI (uint16)."""
    count = len(payload) // 4
    indices = struct.unpack_from("<" + "I" * count, payload, 0) if count else ()
    biggest = max(indices, default=0)
    if biggest > WMO_MAX_GROUP_VERTICES:
        result.fail("wmo.group.indices",
                    f"{name} uses 32-bit triangle indices reaching {biggest}; "
                    f"3.3.5a groups are limited to {WMO_MAX_GROUP_VERTICES} "
                    f"vertices and the group must be split first",
                    max_index=biggest)
        return None
    result.lossy("wmo.group.movx",
                 f"converted {count} 32-bit indices to 16-bit MOVI", indices=count)
    return struct.pack("<" + "H" * count, *indices) if count else b""


def _convert_poly(payload: bytes, triangle_count: int, result: FileResult,
                  name: str) -> bytes | None:
    """MPY2 (uint16 flags + uint16 material) -> MOPY (uint8 + uint8)."""
    if triangle_count <= 0:
        return b""
    stride = len(payload) // triangle_count
    if stride not in (2, 4):
        result.fail("wmo.group.mpy2",
                    f"{name}: MPY2 is {len(payload)} bytes for {triangle_count} "
                    f"triangles, which is neither 2 nor 4 bytes per triangle",
                    stride=stride)
        return None
    if stride == 2:
        return payload[: triangle_count * 2]

    out = bytearray(triangle_count * 2)
    over = 0
    for i in range(triangle_count):
        flags, material = struct.unpack_from("<HH", payload, i * 4)
        if material > 0xFF:
            over += 1
            material = 0xFF  # 0xFF is the client's "collision only" material
        out[i * 2] = flags & 0xFF
        out[i * 2 + 1] = material & 0xFF
    if over:
        result.lossy("wmo.group.material_id",
                     f"{over} triangle(s) referenced a material above 255; "
                     f"3.3.5a stores material ids in a byte, so they were marked "
                     f"collision-only", triangles=over)
    result.lossy("wmo.group.mpy2",
                 f"converted MPY2 to MOPY for {triangle_count} triangles")
    return bytes(out)


def _recompute_batch_bounds(moba: bytes, vertices: bytes, indices: bytes,
                            result: FileResult) -> bytes:
    """Rebuild MOBA bounding boxes from the geometry.

    Shadowlands reused the first twelve bytes of each batch for a wide material
    id, so a straight copy leaves the old client culling against nonsense.
    """
    count = len(moba) // MOBA_SIZE
    vertex_count = len(vertices) // 12
    index_count = len(indices) // 2
    out = bytearray(moba)
    fixed = 0
    for i in range(count):
        base = i * MOBA_SIZE
        start_index, tri_count = struct.unpack_from("<IH", out, base + 12)
        lo = [32767, 32767, 32767]
        hi = [-32768, -32768, -32768]
        seen = False
        for n in range(start_index, min(start_index + tri_count, index_count)):
            vi = struct.unpack_from("<H", indices, n * 2)[0]
            if vi >= vertex_count:
                continue
            x, y, z = struct.unpack_from("<3f", vertices, vi * 12)
            seen = True
            for axis, value in enumerate((x, y, z)):
                iv = int(value)
                lo[axis] = min(lo[axis], iv - 1)
                hi[axis] = max(hi[axis], iv + 1)
        if not seen:
            continue
        clamp = lambda v: max(-32768, min(32767, v))  # noqa: E731
        struct.pack_into("<6h", out, base,
                         clamp(lo[0]), clamp(lo[1]), clamp(lo[2]),
                         clamp(hi[0]), clamp(hi[1]), clamp(hi[2]))
        fixed += 1
    if fixed:
        result.lossy("wmo.group.moba_bounds",
                     f"recomputed bounding boxes for {fixed} render batch(es); "
                     f"the source stored a wide material id in those bytes",
                     batches=fixed)
    return bytes(out)


def convert_group(data: bytes, source_name: str, opts: Options,
                  result: FileResult | None = None) -> tuple[bytes, FileResult]:
    """Downgrade one WMO group file."""
    res = result or FileResult(source=source_name, kind="wmo-group")
    res.kind = "wmo-group"
    res.bytes_in = len(data)

    group = parse_group(data, source_name)
    res.source_version = (f"WMO group v{group.version}, "
                          f"{len(group.subchunks)} sub-chunks")

    by_name: dict[str, list[bytes]] = {}
    for chunk in group.subchunks:
        by_name.setdefault(chunk.name, []).append(chunk.data)

    modern_seen = sorted(n for n in by_name if n in MODERN_GROUP_CHUNKS)
    had_wide_materials = "MPY2" in by_name

    # -- indices --------------------------------------------------------
    indices = b""
    if "MOVI" in by_name:
        indices = by_name["MOVI"][0]
    elif "MOVX" in by_name:
        converted = _convert_indices(by_name["MOVX"][0], res, source_name)
        if converted is None:
            return b"", res
        indices = converted

    # -- polygons -------------------------------------------------------
    triangle_count = len(indices) // 6
    polys = b""
    if "MOPY" in by_name:
        polys = by_name["MOPY"][0]
    elif "MPY2" in by_name:
        converted = _convert_poly(by_name["MPY2"][0], triangle_count, res, source_name)
        if converted is None:
            return b"", res
        polys = converted

    vertices = by_name.get("MOVT", [b""])[0]
    if len(vertices) // 12 > WMO_MAX_GROUP_VERTICES:
        res.fail("wmo.group.vertices",
                 f"{len(vertices) // 12} vertices exceeds the "
                 f"{WMO_MAX_GROUP_VERTICES} a 3.3.5a group can index",
                 vertices=len(vertices) // 12)
        return b"", res

    # -- batches --------------------------------------------------------
    batches = by_name.get("MOBA", [b""])[0]
    if batches and had_wide_materials and vertices and indices:
        batches = _recompute_batch_bounds(batches, vertices, indices, res)

    # -- layered chunks -------------------------------------------------
    uvs = by_name.get("MOTV", [])
    colors = by_name.get("MOCV", [])
    if len(uvs) > WMO_MAX_UV_LAYERS:
        res.lossy("wmo.group.uv_layers",
                  f"dropped {len(uvs) - WMO_MAX_UV_LAYERS} UV layer(s); 3.3.5a "
                  f"binds at most {WMO_MAX_UV_LAYERS}", layers=len(uvs))
        uvs = uvs[:WMO_MAX_UV_LAYERS]
    if len(colors) > WMO_MAX_COLOR_LAYERS:
        res.lossy("wmo.group.color_layers",
                  f"dropped {len(colors) - WMO_MAX_COLOR_LAYERS} vertex-colour "
                  f"layer(s)", layers=len(colors))
        colors = colors[:WMO_MAX_COLOR_LAYERS]

    # -- header ---------------------------------------------------------
    flags = group.flags
    original_flags = flags
    flags &= WMO_GROUP_FLAG_MASK
    if len(uvs) >= 2:
        flags |= WMO_GROUP_FLAG_HAS_TWO_MOTV
    else:
        flags &= ~WMO_GROUP_FLAG_HAS_TWO_MOTV
    if len(colors) >= 2:
        flags |= WMO_GROUP_FLAG_HAS_TWO_MOCV
    else:
        flags &= ~WMO_GROUP_FLAG_HAS_TWO_MOCV
    if colors:
        flags |= WMO_GROUP_FLAG_HAS_VERTEX_COLORS
    else:
        flags &= ~WMO_GROUP_FLAG_HAS_VERTEX_COLORS
    if flags != original_flags:
        res.info("wmo.group.flags",
                 f"group flags 0x{original_flags:08X} -> 0x{flags:08X}")
    group.flags = flags

    if group.flags2 or struct.unpack_from("<I", group.header, 0x40)[0]:
        # Wrath has no flags2 and ignores the last word; Legion's split-group
        # indices would read as a huge unknown value.
        struct.pack_into("<II", group.header, 0x3C, 0, 0)
        res.info("wmo.group.split",
                 "cleared Legion split-group indices from the MOGP header")

    if modern_seen:
        dropped = [n for n in modern_seen if n not in ("MOVX", "MPY2")]
        if dropped:
            res.lossy("wmo.group.chunks_dropped",
                      "dropped group chunks with no 3.3.5a equivalent: "
                      + ", ".join(f"{n} ({MODERN_GROUP_CHUNKS[n]})" for n in dropped),
                      chunks=dropped)

    # -- rebuild --------------------------------------------------------
    inner = ChunkWriter(reverse=group.reverse_magic)
    emit: list[tuple[str, bytes]] = []
    if polys:
        emit.append(("MOPY", polys))
    if indices:
        emit.append(("MOVI", indices))
    if vertices:
        emit.append(("MOVT", vertices))
    for name in ("MONR",):
        if name in by_name:
            emit.append((name, by_name[name][0]))
    for uv in uvs:
        emit.append(("MOTV", uv))
    if batches:
        emit.append(("MOBA", batches))
    for name in ("MOLR", "MODR", "MOBN", "MOBR"):
        if name in by_name:
            emit.append((name, by_name[name][0]))
    for colour in colors:
        emit.append(("MOCV", colour))
    if "MLIQ" in by_name:
        emit.append(("MLIQ", by_name["MLIQ"][0]))
    inner.extend(emit)

    outer = ChunkWriter(reverse=group.reverse_magic)
    outer.add("MVER", struct.pack("<I", WMO_VERSION))
    outer.add("MOGP", bytes(group.header) + inner.getvalue())
    out = outer.getvalue()

    res.bytes_out = len(out)
    res.target_version = f"WMO group v{WMO_VERSION}, {len(emit)} sub-chunks"
    if not modern_seen and res.status is Status.OK:
        res.status = Status.PASSTHROUGH
        res.info("wmo.group.passthrough", "already a 3.3.5a group layout")
    return out, res


def inspect_group(data: bytes, source_name: str) -> dict:
    group = parse_group(data, source_name)
    names: dict[str, int] = {}
    for c in group.subchunks:
        names[c.name] = names.get(c.name, 0) + 1
    return {
        "kind": "wmo-group",
        "version": group.version,
        "flags": f"0x{group.flags:08X}",
        "subchunks": names,
        "modern_subchunks": sorted(n for n in names if n in MODERN_GROUP_CHUNKS) or None,
    }
