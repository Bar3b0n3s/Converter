import struct

import pytest

import fixtures as F
from wotlkconv.adt.convert import (AdtParts, MCIN_ENTRY_SIZE, MCNK_HEADER_SIZE,
                                   _fold_holes, convert_adt, inspect_adt)
from wotlkconv.chunks import ChunkReader
from wotlkconv.errors import MalformedFileError, UnsupportedFormatError
from wotlkconv.limits import ADT_VERSION
from wotlkconv.options import Options
from wotlkconv.report import Status

MHDR_FIELD_NAMES = ("MCIN", "MTEX", "MMDX", "MMID", "MWMO", "MWID", "MDDF",
                    "MODF", "MFBO", "MH2O", "MTXF")


@pytest.fixture
def split_tile():
    return F.build_split_adt(chunks=4)


def read_tile(data: bytes):
    named, mcnks = {}, []
    for c in ChunkReader(data, reverse=True):
        mcnks.append(c) if c.name == "MCNK" else named.setdefault(c.name, c)
    return named, mcnks


# ---------------------------------------------------------------------------
# Holes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("high,low", [
    (bytes([0b11, 0b11, 0, 0, 0, 0, 0, 0]), 0b1),          # one corner quadrant
    (bytes([0, 0, 0, 0, 0, 0, 0b11000000, 0b11000000]), 1 << 15),
    (bytes([0xFF] * 8), 0xFFFF),
    (bytes(8), 0),
])
def test_high_res_holes_fold_to_the_low_res_mask(high, low):
    assert _fold_holes(high) == low


def test_a_single_high_res_hole_still_punches_its_quadrant():
    assert _fold_holes(bytes([0b1] + [0] * 7)) == 0b1


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------
def test_split_pieces_are_recognised(split_tile):
    root, tex, obj = split_tile
    assert inspect_adt(root, "r.adt")["split"] is True
    assert set(inspect_adt(tex, "t.adt")["modern_chunks"]) >= {"MDID", "MTXP"}


def test_merge_produces_a_monolithic_tile(split_tile, listfile):
    root, tex, obj = split_tile
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    named, mcnks = read_tile(out)
    assert res.status is Status.LOSSY
    assert len(mcnks) == 4
    assert {"MVER", "MHDR", "MCIN", "MTEX", "MMDX", "MMID", "MWMO", "MWID",
            "MDDF", "MODF"} <= set(named)
    assert not ({"MTXP", "MDID", "MHID"} & set(named))
    assert struct.unpack_from("<I", named["MVER"].data, 0)[0] == ADT_VERSION


def test_terrain_texture_ids_become_mtex(split_tile, listfile):
    root, tex, obj = split_tile
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    named, _ = read_tile(out)
    paths = [p for p in named["MTEX"].data.split(b"\0") if p]
    assert paths == [b"tileset\\generic\\grass.blp", b"tileset\\generic\\rock.blp"]
    assert any(n.code == "adt.texture.resolved" for n in res.notes)


def test_mhdr_offsets_point_at_real_chunk_headers(split_tile, listfile):
    root, tex, obj = split_tile
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    named, _ = read_tile(out)
    mhdr = named["MHDR"]
    for name, offset in zip(MHDR_FIELD_NAMES,
                            struct.unpack_from("<11I", mhdr.data, 4)):
        if offset == 0:
            continue
        assert out[mhdr.offset + offset: mhdr.offset + offset + 4][::-1] \
            == name.encode()


def test_mcin_indexes_every_map_chunk(split_tile, listfile):
    root, tex, obj = split_tile
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    named, mcnks = read_tile(out)
    mcin = named["MCIN"].data
    for i, chunk in enumerate(mcnks):
        offset, size, flags, async_id = struct.unpack_from(
            "<4I", mcin, i * MCIN_ENTRY_SIZE)
        assert offset == chunk.offset - 8      # points at the MCNK magic
        assert size == chunk.size + 8          # covers magic + size + payload
        assert flags == 0 and async_id == 0


def test_map_chunk_pieces_are_reassembled(split_tile, listfile):
    root, tex, obj = split_tile
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    _named, mcnks = read_tile(out)
    chunk = mcnks[0]
    subs = {c.name: c for c in ChunkReader(chunk.data, reverse=True,
                                           start=MCNK_HEADER_SIZE)}
    assert set(subs) == {"MCVT", "MCCV", "MCNR", "MCLY", "MCRF", "MCSH",
                         "MCAL", "MCSE"}
    assert "MCLV" not in subs and "MCMT" not in subs
    # MCRD (3 doodad refs) then MCRW (2 object refs) become one MCRF.
    assert struct.unpack_from("<5I", subs["MCRF"].data, 0) == (0, 1, 2, 0, 1)


def test_map_chunk_header_offsets_and_counts(split_tile, listfile):
    root, tex, obj = split_tile
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    _named, mcnks = read_tile(out)
    chunk = mcnks[0]
    hdr = chunk.data[:MCNK_HEADER_SIZE]
    subs = {c.name: c for c in ChunkReader(chunk.data, reverse=True,
                                           start=MCNK_HEADER_SIZE)}

    for field, name in ((0x14, "MCVT"), (0x18, "MCNR"), (0x1C, "MCLY"),
                        (0x20, "MCRF"), (0x24, "MCAL"), (0x2C, "MCSH"),
                        (0x58, "MCSE"), (0x74, "MCCV")):
        offset = struct.unpack_from("<I", hdr, field)[0]
        assert chunk.data[offset - 8: offset - 4][::-1] == name.encode()

    assert struct.unpack_from("<I", hdr, 0x0C)[0] == 2   # nLayers
    assert struct.unpack_from("<I", hdr, 0x10)[0] == 3   # nDoodadRefs
    assert struct.unpack_from("<I", hdr, 0x38)[0] == 2   # nMapObjRefs
    assert struct.unpack_from("<I", hdr, 0x5C)[0] == 2   # nSndEmitters
    # Sizes include the sub-chunk header; an absent MCLQ still declares 8.
    assert struct.unpack_from("<I", hdr, 0x28)[0] == len(subs["MCAL"].data) + 8
    assert struct.unpack_from("<I", hdr, 0x30)[0] == len(subs["MCSH"].data) + 8
    assert struct.unpack_from("<I", hdr, 0x64)[0] == 8
    assert struct.unpack_from("<II", hdr, 0x78) == (0, 0)


def test_high_res_holes_are_converted_in_place(split_tile, listfile):
    root, tex, obj = split_tile
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    _named, mcnks = read_tile(out)
    hdr = mcnks[0].data[:MCNK_HEADER_SIZE]
    assert struct.unpack_from("<I", hdr, 0)[0] & 0x10000 == 0
    assert struct.unpack_from("<H", hdr, 0x3C)[0] == 0b1
    assert hdr[0x40:0x50] == b"\0" * 16
    assert any(n.code == "adt.holes" for n in res.notes)


def test_a_monolithic_tile_is_passed_through(split_tile, listfile):
    root, tex, obj = split_tile
    merged, _ = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    again, res = convert_adt(AdtParts(merged), "t.adt", Options(), listfile)
    assert res.status is Status.PASSTHROUGH
    named, mcnks = read_tile(again)
    assert "MCIN" in named and len(mcnks) == 4


def test_merging_without_the_texture_file_still_works(split_tile, listfile):
    root, _tex, obj = split_tile
    out, res = convert_adt(AdtParts(root, b"", obj), "t.adt", Options(), listfile)
    _named, mcnks = read_tile(out)
    subs = {c.name for c in ChunkReader(mcnks[0].data, reverse=True,
                                        start=MCNK_HEADER_SIZE)}
    assert "MCLY" not in subs     # no texture layers were supplied
    assert "MCRF" in subs and res.ok


def test_a_missing_root_is_an_error(listfile):
    with pytest.raises(MalformedFileError):
        convert_adt(AdtParts(b"", b"x", b"y"), "t.adt", Options(), listfile)


def test_a_non_adt_is_rejected(listfile):
    from wotlkconv.chunks import ChunkWriter
    cw = ChunkWriter(reverse=True)
    cw.add("MVER", struct.pack("<I", 18))
    with pytest.raises(UnsupportedFormatError):
        convert_adt(AdtParts(cw.getvalue()), "t.adt", Options(), listfile)


def test_path_prefix_reaches_the_terrain_texture_list(split_tile, listfile):
    """--path-prefix has to rewrite MTEX too, or the tile references art that
    is not where the rest of the converted set went."""
    root, tex, obj = split_tile
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt",
                            Options(path_prefix="custom\\mypatch"), listfile)
    named, _ = read_tile(out)
    paths = [p for p in named["MTEX"].data.split(b"\0") if p]
    assert all(p.startswith(b"custom\\mypatch\\") for p in paths), paths


def test_path_prefix_applies_to_an_existing_mtex(split_tile, listfile):
    """A tile that already had MTEX (rather than MDID) gets prefixed too."""
    root, tex, obj = split_tile
    merged, _ = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    out, _res = convert_adt(AdtParts(merged), "t.adt",
                            Options(path_prefix="patch"), listfile)
    named, _ = read_tile(out)
    paths = [p for p in named["MTEX"].data.split(b"\0") if p]
    assert paths and all(p.startswith(b"patch\\") for p in paths), paths


def test_unresolved_terrain_textures_get_placeholders(split_tile):
    from wotlkconv.listfile import Listfile
    root, tex, obj = split_tile
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(),
                           Listfile())
    named, _ = read_tile(out)
    assert b"unresolved\\blp\\700001.blp" in named["MTEX"].data
    assert any(n.code == "adt.texture.placeholder" for n in res.notes)


def test_unresolved_terrain_textures_can_fail_the_tile(split_tile):
    from wotlkconv.listfile import Listfile
    from wotlkconv.options import UnresolvedPolicy
    root, tex, obj = split_tile
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt",
                           Options(unresolved=UnresolvedPolicy.FAIL), Listfile())
    assert res.status is Status.FAILED and out == b""
    assert any(n.code == "adt.texture.unresolved" for n in res.notes)
