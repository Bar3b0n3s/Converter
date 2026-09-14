"""Splitting meshes that outgrew 16-bit indices."""

import random
import struct

import pytest

import fixtures as F
from wotlkconv.chunks import ChunkReader
from wotlkconv.limits import MOGI_SIZE, MOGP_HEADER_SIZE, WMO_MAX_GROUP_VERTICES
from wotlkconv.listfile import Listfile
from wotlkconv.m2.convert import convert_m2
from wotlkconv.m2.model import parse_m2
from wotlkconv.m2.skin import parse_skin
from wotlkconv.m2.split import compact, referenced_vertices
from wotlkconv.options import Options
from wotlkconv.report import FileResult, Status
from wotlkconv.resolve import AssetSource
from wotlkconv.wmo.bsp import build_bsp, node_count, validate
from wotlkconv.wmo.convert import convert_wmo_root
from wotlkconv.wmo.group import convert_group_parts, parse_group
from wotlkconv.wmo.root import parse_root, split_string_table


# ---------------------------------------------------------------------------
# Collision tree
# ---------------------------------------------------------------------------
def random_mesh(triangles: int, seed: int = 7):
    rng = random.Random(seed)
    vertices = [(rng.uniform(-50, 50), rng.uniform(-50, 50), rng.uniform(0, 30))
                for _ in range(triangles * 2)]
    faces = [(rng.randrange(len(vertices)), rng.randrange(len(vertices)),
              rng.randrange(len(vertices))) for _ in range(triangles)]
    return vertices, faces


@pytest.mark.parametrize("count", [1, 5, 40, 500, 5000])
def test_bsp_covers_every_triangle_exactly_once(count):
    vertices, faces = random_mesh(count)
    mobn, mobr = build_bsp(vertices, faces)
    assert validate(mobn, mobr, len(faces)) == []
    assert len(mobr) // 2 == len(faces)


def test_bsp_of_an_empty_mesh_is_empty():
    assert build_bsp([], []) == (b"", b"")


def test_bsp_node_indices_stay_inside_int16():
    vertices, faces = random_mesh(60000)
    mobn, mobr = build_bsp(vertices, faces)
    assert node_count(mobn) < 0x7FFF
    assert validate(mobn, mobr, len(faces)) == []


def test_bsp_of_coincident_triangles_degenerates_to_a_leaf():
    vertices = [(0.0, 0.0, 0.0)] * 3
    faces = [(0, 1, 2)] * 100
    mobn, mobr = build_bsp(vertices, faces)
    assert node_count(mobn) == 1
    assert validate(mobn, mobr, len(faces)) == []


# ---------------------------------------------------------------------------
# WMO groups
# ---------------------------------------------------------------------------
def group_chunks(data: bytes) -> dict[str, bytes]:
    mogp = next(c for c in ChunkReader(data, reverse=True) if c.name == "MOGP")
    return {c.name: c.data for c in ChunkReader(mogp.data, reverse=True,
                                                start=MOGP_HEADER_SIZE)}


@pytest.fixture(scope="module")
def oversized_group():
    return F.build_oversized_wmo_group(vertices=70000, strips=4)


@pytest.fixture(scope="module")
def oversized_parts(oversized_group):
    return convert_group_parts(oversized_group, "big_000.wmo", Options())


def test_an_oversized_group_splits(oversized_parts):
    parts, res = oversized_parts
    assert len(parts) == 2
    assert res.status is Status.LOSSY
    assert any(n.code == "wmo.group.split" for n in res.notes)


def test_each_part_fits_inside_16_bit_indices(oversized_parts):
    parts, _res = oversized_parts
    for part in parts:
        chunks = group_chunks(part.data)
        vertices = len(chunks["MOVT"]) // 12
        indices = struct.unpack("<" + "H" * (len(chunks["MOVI"]) // 2),
                                chunks["MOVI"])
        assert vertices <= WMO_MAX_GROUP_VERTICES
        assert max(indices) < vertices


def test_per_vertex_arrays_stay_in_step(oversized_parts):
    parts, _res = oversized_parts
    for part in parts:
        chunks = group_chunks(part.data)
        vertices = len(chunks["MOVT"]) // 12
        assert len(chunks["MONR"]) // 12 == vertices
        assert len(chunks["MOTV"]) // 8 == vertices
        assert len(chunks["MOCV"]) // 4 == vertices
        assert len(chunks["MOPY"]) // 2 == len(chunks["MOVI"]) // 6


def test_no_triangle_is_lost_or_duplicated(oversized_group, oversized_parts):
    parts, _res = oversized_parts
    total = sum(len(group_chunks(p.data)["MOVI"]) // 6 for p in parts)
    source = group_chunks(oversized_group)
    assert total == len(source["MOVX"]) // 12


def test_each_part_gets_its_own_collision_tree(oversized_parts):
    parts, _res = oversized_parts
    for part in parts:
        chunks = group_chunks(part.data)
        faces = len(chunks["MOVI"]) // 6
        assert validate(chunks["MOBN"], chunks["MOBR"], faces) == []


def test_batches_stay_inside_their_part(oversized_parts):
    parts, _res = oversized_parts
    for part in parts:
        chunks = group_chunks(part.data)
        indices = len(chunks["MOVI"]) // 2
        vertices = len(chunks["MOVT"]) // 12
        for i in range(len(chunks["MOBA"]) // 24):
            start, count, lowest, highest = struct.unpack_from(
                "<IHHH", chunks["MOBA"], i * 24 + 12)
            assert start + count <= indices
            assert highest < vertices and lowest <= highest
            box = struct.unpack_from("<6h", chunks["MOBA"], i * 24)
            assert box != (0, 0, 0, 0, 0, 0)


def test_liquid_stays_with_the_first_part_only(opts):
    raw = bytearray(F.build_oversized_wmo_group(vertices=70000, strips=4))
    # Splice an MLIQ chunk into the source group.
    mogp = next(c for c in ChunkReader(bytes(raw), reverse=True)
                if c.name == "MOGP")
    body = bytearray(mogp.data)
    body += b"QILM" + struct.pack("<I", 16) + bytes(16)
    from wotlkconv.chunks import ChunkWriter
    outer = ChunkWriter(reverse=True)
    outer.add("MVER", struct.pack("<I", 17))
    outer.add("MOGP", bytes(body))
    parts, _res = convert_group_parts(outer.getvalue(), "g.wmo", opts)
    assert len(parts) > 1
    assert "MLIQ" in group_chunks(parts[0].data)
    assert "MLIQ" not in group_chunks(parts[1].data)


def test_splitting_can_be_refused(oversized_group):
    parts, res = convert_group_parts(oversized_group, "g.wmo",
                                     Options(split_oversized_groups=False))
    assert parts == [] and res.status is Status.FAILED
    assert any(n.code == "wmo.group.indices" for n in res.notes)


def test_a_normal_group_still_produces_one_part(opts):
    parts, _res = convert_group_parts(F.build_modern_wmo_group(), "g.wmo", opts)
    assert len(parts) == 1


# ---------------------------------------------------------------------------
# WMO root bookkeeping
# ---------------------------------------------------------------------------
@pytest.fixture
def root_with_oversized_group(listfile, asset_dir):
    (asset_dir / "830001.wmo").write_bytes(F.build_modern_wmo_group())
    (asset_dir / "830002.wmo").write_bytes(
        F.build_oversized_wmo_group(vertices=70000, strips=4))
    return F.build_modern_wmo_root(groups=2)


def test_the_root_gains_the_groups_a_split_created(root_with_oversized_group,
                                                   listfile, source, opts):
    out, res, companions = convert_wmo_root(root_with_oversized_group,
                                            "House.wmo", opts, listfile, source)
    assert [c.filename for c in companions] == [
        "House_000.wmo", "House_001.wmo", "House_002.wmo"]
    back = parse_root(out, "o.wmo")
    assert back.n_groups == 3
    assert len(back.payload("MOGI")) // MOGI_SIZE == 3
    assert res.extra["groups"] == 3 and res.extra["groups_before_split"] == 2
    assert any(n.code == "wmo.group.added" for n in res.notes)


def test_the_new_group_entry_has_a_name_and_real_bounds(
        root_with_oversized_group, listfile, source, opts):
    out, _res, _companions = convert_wmo_root(root_with_oversized_group,
                                              "House.wmo", opts, listfile, source)
    back = parse_root(out, "o.wmo")
    names = split_string_table(back.payload("MOGN"))
    flags, *box, name_offset = struct.unpack_from("<I6fi", back.payload("MOGI"),
                                                  2 * MOGI_SIZE)
    assert names[name_offset] == "House_002"
    assert box != [0.0] * 6
    assert flags == struct.unpack_from("<I", back.payload("MOGI"),
                                       MOGI_SIZE)[0]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
def build_model_with_skins(model_vertices: int, submesh_lists, asset_dir,
                           profiles: int = 2):
    model = F.build_modern_model(vertices=6)
    model.vertices = F.build_model_vertices(model_vertices)
    model.vertex_count = model_vertices
    model.num_skin_profiles = profiles
    raw = F.serialise_modern_m2(model, skin_ids=(910000, 910001), anim_ids=(),
                                skeleton_id=0)
    skin = F.build_skin_for(submesh_lists, legion=True)
    for file_id in (910000, 910001)[:profiles]:
        (asset_dir / f"{file_id}.skin").write_bytes(skin)
    return raw


def test_unused_geometry_is_dropped_rather_than_failing(listfile, source,
                                                        asset_dir):
    raw = build_model_with_skins(
        70000, [list(range(0, 15000)), list(range(15000, 30000))], asset_dir)
    out, res, companions = convert_m2(raw, "big.m2", Options(), listfile,
                                      source, output_stem="big")
    assert res.ok
    assert parse_m2(out, "o.m2").vertex_count == 30000
    assert any(n.code == "m2.vertices.compacted" for n in res.notes)
    skin = parse_skin(next(c.data for c in companions
                           if c.filename == "big00.skin"), "s")
    assert max(skin.vertices) < 30000


def test_compaction_is_lossless():
    result = FileResult(source="t")
    model = F.build_modern_model(vertices=6)
    model.vertices = F.build_model_vertices(100)
    model.vertex_count = 100
    skin = parse_skin(F.build_skin_for([[10, 11, 12, 90, 91, 92]]), "s")
    before = [model.vertices[v * 48:(v + 1) * 48] for v in skin.vertices]
    compact(model, {0: skin}, result)
    after = [model.vertices[v * 48:(v + 1) * 48] for v in skin.vertices]
    assert before == after          # the drawn vertices are byte-identical
    assert model.vertex_count == 6
    assert referenced_vertices({0: skin}) == set(range(6))


CHUNKS = [list(range(i * 16384, (i + 1) * 16384)) for i in range(4)]


def test_a_model_that_still_does_not_fit_fails_by_default(listfile, source,
                                                          asset_dir):
    raw = build_model_with_skins(70000, CHUNKS, asset_dir)
    out, res, _companions = convert_m2(raw, "big.m2", Options(), listfile,
                                       source, output_stem="big")
    assert res.status is Status.FAILED and out == b""
    assert "--split-models" in next(n.message for n in res.notes
                                    if n.level == "error")


def test_splitting_a_model_shares_the_rig_and_splits_the_geometry(
        listfile, source, asset_dir):
    raw = build_model_with_skins(70000, CHUNKS, asset_dir)
    out, res, companions = convert_m2(
        raw, "big.m2", Options(split_oversized_models=True), listfile, source,
        output_stem="big")
    first = parse_m2(out, "o.m2")
    second = parse_m2(next(c.data for c in companions
                           if c.filename == "big_part1.m2"), "p1.m2")
    assert first.vertex_count + second.vertex_count == 65536
    # the rig, materials and textures ride along with every piece
    assert len(second.bones) == len(first.bones)
    assert len(second.sequences) == len(first.sequences)
    assert len(second.textures) == len(first.textures)
    assert second.bone_combos == first.bone_combos
    # model-wide effects and collision stay with the first piece
    assert first.particles and not second.particles
    assert first.collision_positions and not second.collision_positions
    assert first.attachments and not second.attachments
    # bounds are recomputed, not copied
    assert first.bounding_box != second.bounding_box
    assert any(n.code == "m2.split" for n in res.notes)


def test_every_part_gets_a_full_set_of_skins(listfile, source, asset_dir):
    raw = build_model_with_skins(70000, CHUNKS, asset_dir)
    _out, _res, companions = convert_m2(
        raw, "big.m2", Options(split_oversized_models=True), listfile, source,
        output_stem="big")
    names = sorted(c.filename for c in companions)
    assert names == ["big00.skin", "big01.skin", "big_part1.m2",
                     "big_part100.skin", "big_part101.skin"]


def test_split_skins_are_internally_consistent(listfile, source, asset_dir):
    raw = build_model_with_skins(70000, CHUNKS, asset_dir)
    _out, _res, companions = convert_m2(
        raw, "big.m2", Options(split_oversized_models=True), listfile, source,
        output_stem="big")
    for asset in companions:
        if not asset.filename.endswith(".skin"):
            continue
        skin = parse_skin(asset.data, asset.filename)
        assert max(skin.indices) < len(skin.vertices)
        assert len(skin.bones) == len(skin.vertices) * 4
        cursor_vertices = cursor_indices = 0
        for blob in skin.submeshes:
            level = struct.unpack_from("<H", blob, 2)[0]
            vertex_start, vertex_count, index_start, index_count = \
                struct.unpack_from("<4H", blob, 4)
            # Level carries the high bits of indexStart.
            assert vertex_start == cursor_vertices
            assert (level << 16) | index_start == cursor_indices
            cursor_vertices += vertex_count
            cursor_indices += index_count
        assert cursor_vertices == len(skin.vertices)
        assert cursor_indices == len(skin.indices)


def test_a_large_skin_round_trips_the_index_start_extension(listfile, source,
                                                            asset_dir):
    """A rebuilt skin past 65 535 indices must use Level, not overflow."""
    raw = build_model_with_skins(70000, CHUNKS, asset_dir)
    _out, _res, companions = convert_m2(
        raw, "big.m2", Options(split_oversized_models=True), listfile, source,
        output_stem="big")
    skin = parse_skin(next(c.data for c in companions
                           if c.filename == "big00.skin"), "s")
    per_submesh = (16384 - 2) * 3
    level = struct.unpack_from("<H", skin.submeshes[2], 2)[0]
    index_start = struct.unpack_from("<H", skin.submeshes[2], 8)[0]
    assert level > 0                       # would not fit in 16 bits alone
    assert (level << 16) | index_start == per_submesh * 2


def test_a_model_with_no_skins_still_reports_the_limit(listfile, asset_dir):
    model = F.build_modern_model(vertices=6)
    model.vertices = F.build_model_vertices(70000)
    model.vertex_count = 70000
    raw = F.serialise_modern_m2(model, skin_ids=(), anim_ids=(), skeleton_id=0)
    source = AssetSource(Listfile(), roots=[asset_dir])
    _out, res, _companions = convert_m2(raw, "big.m2", Options(), listfile,
                                        source)
    assert res.status is Status.FAILED
    assert any(n.code == "m2.limit.vertices" for n in res.notes)
