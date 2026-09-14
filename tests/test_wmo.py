import struct

import pytest

import fixtures as F
from wotlkconv.chunks import ChunkReader
from wotlkconv.errors import UnsupportedFormatError
from wotlkconv.limits import MOMT_SIZE
from wotlkconv.options import Options
from wotlkconv.report import Status
from wotlkconv.wmo.convert import convert_wmo_root, inspect_wmo_root
from wotlkconv.wmo.group import convert_group, inspect_group, parse_group
from wotlkconv.wmo.root import StringTable, parse_root, split_string_table


@pytest.fixture
def wmo_root():
    return F.build_modern_wmo_root()


@pytest.fixture
def wmo_group():
    return F.build_modern_wmo_group()


@pytest.fixture
def wmo_source(source, asset_dir, wmo_group):
    (asset_dir / "830001.wmo").write_bytes(wmo_group)
    (asset_dir / "830002.wmo").write_bytes(wmo_group)
    return source


# ---------------------------------------------------------------------------
# String tables
# ---------------------------------------------------------------------------
def test_string_table_dedupes_and_aligns():
    table = StringTable()
    a = table.add("world\\a.blp")
    b = table.add("world\\bb.blp")
    assert table.add("WORLD\\A.BLP") == a       # lookup is case-insensitive
    assert b % 4 == 0                            # entries stay 4-byte aligned
    assert set(split_string_table(table.getvalue()).values()) == {
        "world\\a.blp", "world\\bb.blp"}


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------
def test_modern_root_is_recognised_as_incompatible(wmo_root):
    info = inspect_wmo_root(wmo_root, "t.wmo")
    assert info["wotlk_compatible"] is False
    assert set(info["modern_chunks"]) >= {"GFID", "MODI", "MOSI", "MOUV"}
    assert info["num_lod"] == 2


def test_legion_flags_and_numlod_word_is_split(wmo_root, listfile):
    out, res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)
    back = parse_root(out, "o.wmo")
    assert back.header_flags == 0x2 and back.num_lod == 0
    assert any(n.code == "wmo.lod" for n in res.notes)


def test_texture_file_ids_become_a_motx_table(wmo_root, listfile):
    out, _res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)
    back = parse_root(out, "o.wmo")
    motx = split_string_table(back.payload("MOTX"))
    assert set(motx.values()) == {"world\\wmo\\tex1.blp", "world\\wmo\\tex2.blp"}
    momt = back.payload("MOMT")
    for i in range(2):
        assert struct.unpack_from("<I", momt, i * MOMT_SIZE + 12)[0] in motx
    assert back.n_textures == 2


def test_doodad_file_ids_become_a_modn_table(wmo_root, listfile):
    out, _res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)
    back = parse_root(out, "o.wmo")
    assert "MODI" not in back.chunks
    modn = split_string_table(back.payload("MODN"))
    assert set(modn.values()) == {"world\\doodads\\tree.m2", "world\\doodads\\rock.m2"}
    modd = back.payload("MODD")
    for i in range(2):
        word = struct.unpack_from("<I", modd, i * 40)[0]
        assert (word & 0xFFFFFF) in modn      # nameIndex is now a byte offset
        assert (word >> 24) == 0x01           # the flags byte is preserved
    assert back.n_doodad_names == 2


def test_skybox_file_id_becomes_mosb(wmo_root, listfile):
    out, res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)
    back = parse_root(out, "o.wmo")
    assert back.payload("MOSB").startswith(b"environments\\stars\\sky.m2\0")
    assert any(n.code == "wmo.skybox.resolved" for n in res.notes)


def test_material_shader_and_blend_are_clamped(wmo_root, listfile):
    out, res = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)[:2]
    momt = parse_root(out, "o.wmo").payload("MOMT")
    for i in range(2):
        flags, shader, blend = struct.unpack_from("<3I", momt, i * MOMT_SIZE)
        assert shader <= 6 and blend <= 6
        assert flags == 0x0004                # the 0x8000 bit is post-Wrath
    codes = {n.code for n in res.notes}
    assert {"wmo.material.shader", "wmo.material.blend"} <= codes


def test_modern_chunks_are_dropped(wmo_root, listfile):
    out, _res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)
    names = set(parse_root(out, "o.wmo").chunks)
    assert not (names & {"GFID", "MODI", "MOSI", "MOUV", "MAVG"})
    assert {"MVER", "MOHD", "MOTX", "MOMT", "MOGI", "MODS", "MODN", "MODD",
            "MFOG"} <= names


def test_path_prefix_applies_to_both_tables(wmo_root, listfile):
    out, _res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(path_prefix="patch"),
                                    listfile)
    back = parse_root(out, "o.wmo")
    assert all(v.startswith("patch\\")
               for v in split_string_table(back.payload("MOTX")).values())
    assert all(v.startswith("patch\\")
               for v in split_string_table(back.payload("MODN")).values())


def test_groups_are_converted_and_renamed(wmo_root, listfile, wmo_source):
    _out, res, companions = convert_wmo_root(wmo_root, "House.wmo", Options(),
                                             listfile, wmo_source)
    assert [c.filename for c in companions] == ["House_000.wmo", "House_001.wmo"]
    assert all(c.data for c in companions)


def test_output_stem_renames_the_groups(wmo_root, listfile, wmo_source):
    _out, _res, companions = convert_wmo_root(wmo_root, "12345.wmo", Options(),
                                              listfile, wmo_source,
                                              output_stem="Stormwind")
    assert [c.filename for c in companions] == ["Stormwind_000.wmo",
                                                "Stormwind_001.wmo"]


def test_missing_groups_are_reported(wmo_root, listfile, source):
    _out, res, companions = convert_wmo_root(wmo_root, "t.wmo", Options(),
                                             listfile, source)
    assert companions == []
    assert any(n.code == "wmo.group.missing" for n in res.notes)


def test_a_non_wmo_is_rejected():
    with pytest.raises(UnsupportedFormatError):
        parse_root(b"NOPE" + b"\0" * 100, "t.wmo")


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
def group_subchunks(data: bytes) -> list[str]:
    return [c.name for c in parse_group(data, "g").subchunks]


def test_wide_indices_and_polys_are_narrowed(wmo_group, opts):
    out, res = convert_group(wmo_group, "g.wmo", opts)
    names = group_subchunks(out)
    assert "MOVX" not in names and "MPY2" not in names
    assert "MOVI" in names and "MOPY" in names
    codes = {n.code for n in res.notes}
    assert {"wmo.group.movx", "wmo.group.mpy2"} <= codes


def test_material_ids_above_255_become_collision_only(opts):
    raw = F.build_modern_wmo_group(big_material=True)
    out, res = convert_group(raw, "g.wmo", opts)
    mopy = next(c for c in parse_group(out, "g").subchunks if c.name == "MOPY")
    assert mopy.data[1] == 0xFF
    assert any(n.code == "wmo.group.material_id" for n in res.notes)


def test_extra_uv_and_colour_layers_are_dropped(opts):
    raw = F.build_modern_wmo_group(uv_layers=4, colour_layers=3)
    out, res = convert_group(raw, "g.wmo", opts)
    names = group_subchunks(out)
    assert names.count("MOTV") == 2 and names.count("MOCV") == 2
    codes = {n.code for n in res.notes}
    assert {"wmo.group.uv_layers", "wmo.group.color_layers"} <= codes


def test_layer_flags_are_recomputed_to_match(opts):
    raw = F.build_modern_wmo_group(uv_layers=1, colour_layers=0)
    out, _res = convert_group(raw, "g.wmo", opts)
    flags = parse_group(out, "g").flags
    assert flags & 0x02000000 == 0   # only one MOTV: the two-UV bit must clear
    assert flags & 0x01000000 == 0
    assert flags & 0x00000004 == 0   # no MOCV: the vertex-colour bit must clear
    assert flags & 0x08000000 == 0   # post-Wrath bit cleared


def test_split_group_indices_are_cleared(wmo_group, opts):
    out, res = convert_group(wmo_group, "g.wmo", opts)
    group = parse_group(out, "g")
    assert group.flags2 == 0
    assert struct.unpack_from("<I", group.header, 0x40)[0] == 0
    assert any(n.code == "wmo.group.split_index" for n in res.notes)


def test_batch_bounds_are_recomputed_for_shadowlands_layout(wmo_group, opts):
    out, res = convert_group(wmo_group, "g.wmo", opts)
    moba = next(c for c in parse_group(out, "g").subchunks if c.name == "MOBA")
    assert struct.unpack_from("<6h", moba.data, 0) != (0, 0, 0, 0, 0, 0)
    assert any(n.code == "wmo.group.moba_bounds" for n in res.notes)


def test_modern_group_chunks_are_dropped(wmo_group, opts):
    out, _res = convert_group(wmo_group, "g.wmo", opts)
    names = group_subchunks(out)
    assert "MOBS" not in names and "MOLS" not in names


def test_a_wrath_group_is_passed_through(opts):
    raw = F.build_modern_wmo_group(wide_indices=False, wide_polys=False,
                                   uv_layers=1, colour_layers=1)
    # Strip the modern-only chunks so nothing needs converting.
    inner = [c for c in parse_group(raw, "g").subchunks
             if c.name not in ("MOBS", "MOLS")]
    from wotlkconv.chunks import ChunkWriter
    body = ChunkWriter(reverse=True)
    for c in inner:
        body.add(c.name, c.data)
    header = bytearray(parse_group(raw, "g").header)
    struct.pack_into("<I", header, 8, 0x0C)
    struct.pack_into("<II", header, 0x3C, 0, 0)
    outer = ChunkWriter(reverse=True)
    outer.add("MVER", struct.pack("<I", 17))
    outer.add("MOGP", bytes(header) + body.getvalue())
    _out, res = convert_group(outer.getvalue(), "g.wmo", opts)
    assert res.status is Status.PASSTHROUGH


def test_an_index_past_the_vertex_array_is_a_hard_failure(opts):
    raw = bytearray(F.build_modern_wmo_group(wide_indices=True))
    # Sub-chunk offsets are relative to the MOGP payload, so rebase onto the file.
    mogp = next(c for c in ChunkReader(bytes(raw), reverse=True) if c.name == "MOGP")
    movx = next(c for c in parse_group(bytes(raw), "g").subchunks
                if c.name == "MOVX")
    struct.pack_into("<I", raw, mogp.offset + movx.offset, 70000)
    out, res = convert_group(bytes(raw), "g.wmo", opts)
    assert res.status is Status.FAILED and out == b""
    assert any(n.code == "wmo.group.bad_index" for n in res.notes)


def test_inspect_group_lists_modern_subchunks(wmo_group):
    info = inspect_group(wmo_group, "g.wmo")
    assert set(info["modern_subchunks"]) >= {"MOVX", "MPY2", "MOBS", "MOLS"}
