"""WMO downgrade: root file plus every group that belongs to it."""

from __future__ import annotations

import dataclasses
import os
import struct
import time

from ..chunks import ChunkWriter
from ..limits import (
    MODD_SIZE,
    MOHD_SIZE,
    MOMT_SIZE,
    WMO_HEADER_FLAG_MASK,
    WMO_MATERIAL_FLAG_MASK,
    WMO_MAX_BLEND_MODE,
    WMO_MAX_GROUPS,
    WMO_MAX_SHADER,
    WMO_VERSION,
)
from ..listfile import Listfile, normalise
from ..options import Options, UnresolvedPolicy
from ..report import FileResult, Status
from ..resolve import AssetSource
from .group import convert_group
from .root import MODERN_ROOT_CHUNKS, StringTable, WmoRoot, parse_root, split_string_table


@dataclasses.dataclass(slots=True)
class ConvertedAsset:
    filename: str
    data: bytes
    result: FileResult


def _resolve(file_id: int, kind: str, opts: Options, listfile: Listfile,
             result: FileResult) -> str | None:
    """FileDataID -> in-game path under the configured unresolved policy."""
    path = listfile.path_for(file_id)
    if path is None:
        if opts.unresolved is UnresolvedPolicy.FAIL:
            result.fail("wmo.reference.unresolved",
                        f"no listfile entry for {kind} FileDataID {file_id}",
                        file_id=file_id, kind=kind)
            return None
        if opts.unresolved is UnresolvedPolicy.STRIP:
            result.lossy("wmo.reference.stripped",
                         f"dropped unresolvable {kind} FileDataID {file_id}",
                         file_id=file_id, kind=kind)
            return ""
        path = normalise(f"unresolved/{kind}/{file_id}.{kind}")
        result.lossy("wmo.reference.placeholder",
                     f"{kind} FileDataID {file_id} is not in the listfile; "
                     f"pointed it at {path}", file_id=file_id, path=path)
    if opts.path_prefix:
        path = normalise(opts.path_prefix.rstrip("\\/") + "\\" + path)
    return path


def _rebuild_materials(root: WmoRoot, opts: Options, listfile: Listfile,
                       result: FileResult) -> tuple[bytes, bytes]:
    """Return (MOTX blob, MOMT blob) with texture references repointed.

    Handles both source shapes: a MOTX string table addressed by byte offset,
    and BfA-era materials that carry FileDataIDs where the offsets used to be.
    """
    momt = root.payload("MOMT")
    count = len(momt) // MOMT_SIZE
    motx_blob = root.payload("MOTX")
    strings = split_string_table(motx_blob) if motx_blob else {}
    uses_file_ids = not motx_blob

    table = StringTable()
    out = bytearray(momt)
    clamped_shader = 0
    clamped_blend = 0
    clamped_flags = 0
    resolved_ids = 0

    # texture_1, texture_2, texture_3 live at these offsets in SMOMaterial.
    texture_fields = (12, 24, 32)

    for i in range(count):
        base = i * MOMT_SIZE
        flags, shader, blend = struct.unpack_from("<3I", out, base)

        masked = flags & WMO_MATERIAL_FLAG_MASK
        if masked != flags:
            clamped_flags += 1
        if shader > WMO_MAX_SHADER:
            clamped_shader += 1
            shader = 0  # plain diffuse always renders
        if blend > WMO_MAX_BLEND_MODE:
            clamped_blend += 1
            blend = 2  # alpha
        struct.pack_into("<3I", out, base, masked, shader, blend)

        for field in texture_fields:
            value = struct.unpack_from("<I", out, base + field)[0]
            if value == 0:
                continue
            if uses_file_ids:
                path = _resolve(value, "blp", opts, listfile, result)
                if path is None:
                    return b"", b""
                resolved_ids += 1
            else:
                path = strings.get(value)
                if path is None:
                    # Offsets that do not start a string happen in hand-edited
                    # WMOs; point them at the first texture rather than crash.
                    path = next(iter(strings.values()), "")
                if path and opts.path_prefix:
                    path = normalise(opts.path_prefix.rstrip("\\/") + "\\" + path)
            struct.pack_into("<I", out, base + field,
                             table.add(path) if path else 0)

    if clamped_shader:
        result.lossy("wmo.material.shader",
                     f"{clamped_shader} material(s) used a shader newer than "
                     f"3.3.5a; reset to diffuse", materials=clamped_shader)
    if clamped_blend:
        result.lossy("wmo.material.blend",
                     f"{clamped_blend} material(s) used a blend mode newer than "
                     f"3.3.5a; reset to alpha", materials=clamped_blend)
    if clamped_flags:
        result.info("wmo.material.flags",
                    f"cleared post-Wrath render flags on {clamped_flags} material(s)")
    if resolved_ids:
        result.info("wmo.texture.resolved",
                    f"resolved {resolved_ids} texture FileDataID(s) into a MOTX "
                    f"string table", count=resolved_ids)
    return table.getvalue(), bytes(out)


def _rebuild_doodads(root: WmoRoot, opts: Options, listfile: Listfile,
                     result: FileResult) -> tuple[bytes, bytes]:
    """Return (MODN blob, MODD blob) with doodad references repointed."""
    modd = root.payload("MODD")
    count = len(modd) // MODD_SIZE
    modn_blob = root.payload("MODN")
    modi = root.payload("MODI")

    table = StringTable()
    out = bytearray(modd)

    if modn_blob:
        strings = split_string_table(modn_blob)
        fallback = next(iter(strings.values()), "")
        for i in range(count):
            word = struct.unpack_from("<I", out, i * MODD_SIZE)[0]
            name_index, flags = word & 0xFFFFFF, word >> 24
            path = strings.get(name_index, fallback)
            if path and opts.path_prefix:
                path = normalise(opts.path_prefix.rstrip("\\/") + "\\" + path)
            new_index = table.add(path) if path else 0
            struct.pack_into("<I", out, i * MODD_SIZE,
                             (new_index & 0xFFFFFF) | (flags << 24))
        return table.getvalue(), bytes(out)

    if modi:
        # BfA replaced the name table with an array of FileDataIDs, and MODD's
        # nameIndex became an index into it rather than a byte offset.
        ids = list(struct.unpack_from("<" + "I" * (len(modi) // 4), modi, 0))
        paths: list[str] = []
        for file_id in ids:
            path = _resolve(file_id, "m2", opts, listfile, result) if file_id else ""
            if path is None:
                return b"", b""
            paths.append(path)
        for i in range(count):
            word = struct.unpack_from("<I", out, i * MODD_SIZE)[0]
            name_index, flags = word & 0xFFFFFF, word >> 24
            path = paths[name_index] if name_index < len(paths) else ""
            new_index = table.add(path) if path else 0
            struct.pack_into("<I", out, i * MODD_SIZE,
                             (new_index & 0xFFFFFF) | (flags << 24))
        result.info("wmo.doodad.resolved",
                    f"rebuilt MODN from {len(ids)} MODI FileDataID(s)",
                    count=len(ids))
        return table.getvalue(), bytes(out)

    return b"", bytes(out)


def _skybox(root: WmoRoot, opts: Options, listfile: Listfile,
            result: FileResult) -> bytes:
    mosb = root.payload("MOSB")
    if mosb:
        return mosb
    mosi = root.payload("MOSI")
    if len(mosi) >= 4:
        file_id = struct.unpack_from("<I", mosi, 0)[0]
        path = _resolve(file_id, "m2", opts, listfile, result) if file_id else ""
        if path:
            result.info("wmo.skybox.resolved", f"skybox MOSI {file_id} -> {path}")
            blob = path.encode("latin-1") + b"\0"
            while len(blob) % 4:
                blob += b"\0"
            return blob
    return b"\0" * 4


def convert_wmo_root(data: bytes, source_name: str, opts: Options,
                     listfile: Listfile | None = None,
                     source: AssetSource | None = None,
                     result: FileResult | None = None,
                     output_stem: str | None = None
                     ) -> tuple[bytes, FileResult, list[ConvertedAsset]]:
    """Convert a WMO root and, when available, its group files.

    ``output_stem`` is the basename the root will be written as; groups are
    renamed to ``<stem>_000.wmo`` so the client finds them.
    """
    started = time.time()
    res = result or FileResult(source=source_name, kind="wmo")
    res.kind = "wmo"
    res.bytes_in = len(data)
    listfile = listfile or Listfile()

    root = parse_root(data, source_name)
    modern = sorted(n for n in root.chunks if n in MODERN_ROOT_CHUNKS)
    res.source_version = (f"WMO root v{root.version}, {root.n_groups} groups, "
                          f"{len(root.chunks)} chunk kinds")

    if root.version != WMO_VERSION:
        res.warn("wmo.version",
                 f"root declares version {root.version}; 3.3.5a expects "
                 f"{WMO_VERSION}", version=root.version)
    if root.n_groups > WMO_MAX_GROUPS:
        res.warn("wmo.limit.groups",
                 f"{root.n_groups} groups is far beyond anything 3.3.5a ships; "
                 f"expect long load times", groups=root.n_groups)

    motx, momt = _rebuild_materials(root, opts, listfile, res)
    if not res.ok:
        res.elapsed = time.time() - started
        return b"", res, []
    modn, modd = _rebuild_doodads(root, opts, listfile, res)
    if not res.ok:
        res.elapsed = time.time() - started
        return b"", res, []
    mosb = _skybox(root, opts, listfile, res)

    # -- header ---------------------------------------------------------
    mohd = bytearray(root.payload("MOHD")[:MOHD_SIZE])
    struct.pack_into("<I", mohd, 0, len(split_string_table(motx)) if motx else 0)
    struct.pack_into("<I", mohd, 16, len(split_string_table(modn)) if modn else 0)
    flags = root.header_flags & WMO_HEADER_FLAG_MASK
    struct.pack_into("<I", mohd, 60, flags)
    if root.num_lod:
        res.lossy("wmo.lod",
                  f"dropped {root.num_lod} level(s) of detail; 3.3.5a renders "
                  f"the base geometry only", num_lod=root.num_lod)

    if modern:
        res.lossy("wmo.chunks.dropped",
                  "dropped root chunks with no 3.3.5a equivalent: "
                  + ", ".join(f"{n} ({MODERN_ROOT_CHUNKS[n]})" for n in modern
                              if n not in ("MODI", "MOSI", "GFID")),
                  chunks=modern)

    # -- rebuild --------------------------------------------------------
    cw = ChunkWriter(reverse=root.reverse_magic)
    cw.add("MVER", struct.pack("<I", WMO_VERSION))
    cw.add("MOHD", bytes(mohd))
    cw.add("MOTX", motx or b"\0" * 4)
    cw.add("MOMT", momt)
    cw.add("MOGN", root.payload("MOGN") or b"\0" * 4)
    cw.add("MOGI", root.payload("MOGI"))
    cw.add("MOSB", mosb)
    for name in ("MOPV", "MOPT", "MOPR", "MOVV", "MOVB", "MOLT", "MODS"):
        cw.add(name, root.payload(name))
    cw.add("MODN", modn or b"\0" * 4)
    cw.add("MODD", modd)
    cw.add("MFOG", root.payload("MFOG"))
    if "MCVP" in root.chunks:
        cw.add("MCVP", root.payload("MCVP"))
    out = cw.getvalue()

    res.bytes_out = len(out)
    res.target_version = f"WMO root v{WMO_VERSION}, {root.n_groups} groups"
    res.extra.update({"groups": root.n_groups, "materials": len(momt) // MOMT_SIZE,
                      "doodads": len(modd) // MODD_SIZE})

    companions = _convert_groups(root, source_name, opts, source, res,
                                 output_stem)

    if not modern and res.status is Status.OK:
        res.status = Status.PASSTHROUGH
        res.info("wmo.passthrough", "already a 3.3.5a root layout")
    res.elapsed = time.time() - started
    return out, res, companions


def _convert_groups(root: WmoRoot, source_name: str, opts: Options,
                    source: AssetSource | None, result: FileResult,
                    output_stem: str | None = None) -> list[ConvertedAsset]:
    """Convert the group files, renaming them into ``<root>_NNN.wmo``."""
    if source is None or not opts.convert_companions:
        return []

    src_stem = os.path.splitext(os.path.basename(source_name))[0]
    stem = output_stem or src_stem
    gfid = root.payload("GFID")
    group_ids = list(struct.unpack_from("<" + "I" * (len(gfid) // 4), gfid, 0)) \
        if gfid else []

    out: list[ConvertedAsset] = []
    found = 0
    for index in range(root.n_groups):
        raw = None
        if index < len(group_ids) and group_ids[index]:
            raw = source.by_file_id(group_ids[index], ".wmo")
        if raw is None:
            raw = source.by_path(f"{src_stem}_{index:03d}.wmo")
        if raw is None:
            continue
        name = f"{stem}_{index:03d}.wmo"
        sub = FileResult(source=name, kind="wmo-group")
        try:
            data, sub = convert_group(raw, name, opts, sub)
        except Exception as exc:  # noqa: BLE001 - reported per file
            sub.fail("wmo.group.error", f"{type(exc).__name__}: {exc}")
            out.append(ConvertedAsset(name, b"", sub))
            continue
        if sub.ok and data:
            found += 1
        out.append(ConvertedAsset(name, data, sub))

    if root.n_groups and found == 0:
        result.warn("wmo.group.missing",
                    f"none of the {root.n_groups} group file(s) were found next "
                    f"to the root; the WMO will not render without them",
                    expected=root.n_groups)
    elif found < root.n_groups:
        result.info("wmo.group.partial",
                    f"converted {found} of {root.n_groups} group file(s)",
                    found=found, expected=root.n_groups)
    return out


def inspect_wmo_root(data: bytes, source_name: str) -> dict:
    root = parse_root(data, source_name)
    return {
        "kind": "wmo",
        "version": root.version,
        "groups": root.n_groups,
        "textures": root.n_textures,
        "doodad_names": root.n_doodad_names,
        "doodad_defs": root.n_doodad_defs,
        "header_flags": f"0x{root.header_flags:04X}",
        "num_lod": root.num_lod,
        "chunks": sorted(root.chunks),
        "modern_chunks": sorted(n for n in root.chunks if n in MODERN_ROOT_CHUNKS) or None,
        "wotlk_compatible": not any(n in MODERN_ROOT_CHUNKS for n in root.chunks),
    }
