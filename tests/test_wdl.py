"""Low-resolution heightmaps: keep the three chunks 3.3.5a draws."""

import struct

import pytest

import fixtures as F
from wotlkconv.adt.wdl import (MAOF_ENTRIES, MARE_SIZE, WDL_VERSION,
                               convert_wdl, inspect_wdl)
from wotlkconv.chunks import ChunkReader
from wotlkconv.errors import MalformedFileError, UnsupportedFormatError
from wotlkconv.options import Options
from wotlkconv.report import Status


def chunks_of(data: bytes) -> list[str]:
    return [c.name for c in ChunkReader(data, reverse=True)]


def tile_heights(data: bytes, x: int, y: int) -> bytes:
    """Follow MAOF to a tile's MARE payload the way the client would."""
    maof = next(c for c in ChunkReader(data, reverse=True) if c.name == "MAOF")
    offset = struct.unpack_from("<I", maof.data, (y * 64 + x) * 4)[0]
    assert offset, f"tile {x},{y} has no heightmap"
    assert data[offset:offset + 4] == b"ERAM"
    size = struct.unpack_from("<I", data, offset + 4)[0]
    return data[offset + 8:offset + 8 + size]


def test_the_legion_lod_mesh_is_dropped():
    out, res = convert_wdl(F.build_wdl(), "t.wdl", Options())
    assert res.status is Status.LOSSY
    assert not [n for n in chunks_of(out) if n.startswith("ML")]
    note = next(n for n in res.notes if n.code == "wdl.chunks.dropped")
    assert "MLHD" in note.message and "no renderer" in note.message


def test_the_heightmap_survives_intact():
    out, _res = convert_wdl(F.build_wdl(tiles=((0, 0), (32, 48))), "t.wdl",
                            Options())
    assert tile_heights(out, 0, 0) == struct.pack("<h", 100) * (MARE_SIZE // 2)
    assert tile_heights(out, 32, 48) == struct.pack("<h", 101) * (MARE_SIZE // 2)


def test_maof_offsets_are_rewritten_for_the_new_layout():
    """Dropping chunks moves every MARE, so every offset has to move too."""
    source = F.build_wdl(tiles=((5, 7),))
    out, _res = convert_wdl(source, "t.wdl", Options())
    maof_before = next(c for c in ChunkReader(source, reverse=True)
                       if c.name == "MAOF").data
    maof_after = next(c for c in ChunkReader(out, reverse=True)
                      if c.name == "MAOF").data
    at = (7 * 64 + 5) * 4
    assert struct.unpack_from("<I", maof_before, at)[0] != \
        struct.unpack_from("<I", maof_after, at)[0]
    # and the new one lands on a real MARE
    assert len(tile_heights(out, 5, 7)) == MARE_SIZE


def test_tiles_without_terrain_keep_a_zero_offset():
    out, res = convert_wdl(F.build_wdl(tiles=((1, 1),)), "t.wdl", Options())
    maof = next(c for c in ChunkReader(out, reverse=True)
                if c.name == "MAOF").data
    filled = [i for i in range(MAOF_ENTRIES)
              if struct.unpack_from("<I", maof, i * 4)[0]]
    assert filled == [1 * 64 + 1]
    assert res.extra["tiles"] == 1


def test_hole_masks_follow_their_heightmap():
    out, res = convert_wdl(F.build_wdl(tiles=((0, 0),), holes=True), "t.wdl",
                           Options())
    assert res.extra["holes"] == 1
    names = chunks_of(out)
    assert names[names.index("MARE") + 1] == "MAHO"


def test_a_file_without_holes_writes_none():
    out, res = convert_wdl(F.build_wdl(holes=False), "t.wdl", Options())
    assert res.extra["holes"] == 0
    assert "MAHO" not in chunks_of(out)


def test_the_wrath_wmo_tables_are_always_present():
    """Legion stopped writing them; the old client still looks for them."""
    out, _res = convert_wdl(F.build_wdl(wmo_tables=False), "t.wdl", Options())
    assert chunks_of(out)[:4] == ["MVER", "MWMO", "MWID", "MODF"]


def test_wmo_tables_that_exist_are_carried_over():
    out, _res = convert_wdl(F.build_wdl(wmo_tables=True, lod_mesh=False),
                            "t.wdl", Options())
    mwmo = next(c for c in ChunkReader(out, reverse=True) if c.name == "MWMO")
    assert mwmo.data == b"world/wmo/a.wmo\0"


def test_the_version_is_restated_as_wrath_writes_it():
    out, res = convert_wdl(F.build_wdl(version=22), "t.wdl", Options())
    mver = next(c for c in ChunkReader(out, reverse=True) if c.name == "MVER")
    assert struct.unpack_from("<I", mver.data, 0)[0] == WDL_VERSION
    assert "v22" in res.source_version


def test_a_tile_pointing_at_the_wrong_size_is_reported_not_trusted():
    out, res = convert_wdl(F.build_wdl(tiles=((0, 0),), mare_size=64),
                           "t.wdl", Options())
    assert res.extra["tiles"] == 0
    note = next(n for n in res.notes if n.code == "wdl.tiles.unreadable")
    assert str(MARE_SIZE) in note.message


def test_an_offset_running_past_the_end_is_reported_not_trusted():
    source = bytearray(F.build_wdl(tiles=((0, 0),), lod_mesh=False))
    maof = next(c for c in ChunkReader(bytes(source), reverse=True)
                if c.name == "MAOF")
    struct.pack_into("<I", source, maof.offset, 0xFFFF0000)
    _out, res = convert_wdl(bytes(source), "t.wdl", Options())
    assert res.extra["tiles"] == 0
    assert any(n.code == "wdl.tiles.unreadable" for n in res.notes)


def test_a_file_with_no_maof_is_not_a_wdl():
    with pytest.raises(UnsupportedFormatError, match="no MAOF"):
        convert_wdl(F.build_wdl()[:16], "t.wdl", Options())


def test_a_short_maof_is_malformed():
    source = bytearray(F.build_wdl(tiles=(), lod_mesh=False))
    maof = next(c for c in ChunkReader(bytes(source), reverse=True)
                if c.name == "MAOF")
    struct.pack_into("<I", source, maof.offset - 4, 64)
    with pytest.raises(MalformedFileError, match="MAOF is 64 bytes"):
        convert_wdl(bytes(source[:maof.offset + 64]), "t.wdl", Options())


def test_a_converted_file_converts_again_to_itself():
    """The output is a 3.3.5a .wdl, so feeding it back changes nothing."""
    once, _res = convert_wdl(F.build_wdl(), "t.wdl", Options())
    twice, res = convert_wdl(once, "t.wdl", Options())
    assert twice == once
    assert res.status is Status.OK


def test_inspect_reports_what_is_in_the_file():
    info = inspect_wdl(F.build_wdl(tiles=((0, 0), (1, 2))), "t.wdl")
    assert info["kind"] == "wdl"
    assert info["version"] == 18
    assert info["tiles_with_terrain"] == 2
    assert "MLHD" in info["lod_mesh"]
