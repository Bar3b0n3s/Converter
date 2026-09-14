import struct

import pytest

import fixtures as F
from wotlkconv.errors import UnsupportedFormatError
from wotlkconv.limits import M2_VERSION
from wotlkconv.m2 import schemas
from wotlkconv.m2.convert import convert_m2, inspect_m2
from wotlkconv.m2.downgrade import anim_filename
from wotlkconv.m2.model import parse_m2
from wotlkconv.m2.types import Schema
from wotlkconv.m2.write import write_md20
from wotlkconv.options import Options, UnresolvedPolicy
from wotlkconv.report import Status


@pytest.fixture
def modern_m2():
    model = F.build_modern_model()
    return model, F.serialise_modern_m2(model)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("schema,size", [
    (schemas.BONE, 88), (schemas.TEXTURE, 16), (schemas.MATERIAL, 4),
    (schemas.COLOR, 40), (schemas.TEXTURE_WEIGHT, 20),
    (schemas.TEXTURE_TRANSFORM, 60), (schemas.ATTACHMENT, 40),
    (schemas.EVENT, 36), (schemas.LIGHT, 156), (schemas.RIBBON, 176),
    (schemas.SEQUENCE_264, 64), (schemas.SEQUENCE_272, 64),
    (schemas.CAMERA_264, 100), (schemas.CAMERA_272, 116),
    (schemas.PARTICLE_264, 476), (schemas.PARTICLE_CATA, 492),
])
def test_struct_sizes_match_the_wire_format(schema: Schema, size: int):
    assert schema.size == size


def test_schema_defaults_are_writable():
    w = write_md20(parse_m2(F.serialise_modern_m2(
        F.build_modern_model(sequences=1, bones=1, particles=0, cameras=0,
                             ribbons=0, lights=0)), "t.m2"))
    assert w[:4] == b"MD20"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_parses_a_chunked_legion_model(modern_m2):
    _model, raw = modern_m2
    assert raw[:4] == b"MD21"
    m = parse_m2(raw, "t.m2")
    assert m.chunked and m.version == 272
    assert m.texture_file_ids == [900000, 900001]
    assert m.skin_file_ids == [910000, 910001, 910002, 910003]
    assert [(a.anim_id, a.sub_anim_id, a.file_id) for a in m.anim_file_ids] == [
        (0, 0, 920000), (1, 0, 920001)]
    assert m.phys_file_id == 930000
    assert set(m.extra_chunks) >= {"PABC", "LDV1"}


def test_picks_the_right_era_struct_layouts(modern_m2):
    _model, raw = modern_m2
    m = parse_m2(raw, "t.m2")
    assert m.particle_schema is schemas.PARTICLE_CATA
    assert m.camera_schema is schemas.CAMERA_272
    assert m.sequence_schema is schemas.SEQUENCE_272


def test_nested_tracks_survive_parsing(modern_m2):
    model, raw = modern_m2
    m = parse_m2(raw, "t.m2")
    assert m.bones[0]["translation"].values == model.bones[0]["translation"].values
    assert m.bones[0]["rotation"].timestamps == model.bones[0]["rotation"].timestamps
    assert m.particles[0]["color_track"].values == [(1.0, 1.0, 1.0), (1.0, 0.0, 0.0)]
    assert m.events[0]["identifier"] == b"$AH0"


def test_flat_md20_is_parsed_without_a_wrapper():
    model = F.build_modern_model(version=264)
    raw = F.serialise_modern_m2(model, chunked=False)
    m = parse_m2(raw, "t.m2")
    assert not m.chunked and m.version == 264


def test_pre_wrath_models_are_rejected():
    model = F.build_modern_model(version=264)
    raw = bytearray(F.serialise_modern_m2(model, chunked=False))
    struct.pack_into("<I", raw, 4, 260)
    with pytest.raises(UnsupportedFormatError, match="predates Wrath"):
        parse_m2(bytes(raw), "old.m2")


def test_a_non_model_is_rejected():
    with pytest.raises(UnsupportedFormatError):
        parse_m2(b"NOPE" + b"\0" * 400, "not.m2")


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------
def convert(raw, listfile, source=None, **kw):
    return convert_m2(raw, "TestModel.m2", Options(**kw), listfile, source)


def test_downgrade_emits_version_264(modern_m2, listfile):
    _model, raw = modern_m2
    out, res, _ = convert(raw, listfile)
    assert res.status is Status.LOSSY
    back = parse_m2(out, "o.m2")
    assert back.version == M2_VERSION and not back.chunked
    assert out[:4] == b"MD20"


def test_texture_file_ids_become_inline_paths(modern_m2, listfile):
    _model, raw = modern_m2
    out, _res, _ = convert(raw, listfile)
    back = parse_m2(out, "o.m2")
    assert back.textures[0]["filename"] == "creature\\testbeast\\testbeast_skin.blp"
    assert back.textures[1]["filename"] == "creature\\testbeast\\testbeast_normal.blp"


def test_path_prefix_is_applied(modern_m2, listfile):
    _model, raw = modern_m2
    out, _res, _ = convert(raw, listfile, path_prefix="custom\\mypatch")
    back = parse_m2(out, "o.m2")
    assert back.textures[0]["filename"].startswith("custom\\mypatch\\creature\\")


def test_unresolved_reference_policies(modern_m2):
    from wotlkconv.listfile import Listfile
    _model, raw = modern_m2
    empty = Listfile()

    out, res, _ = convert(raw, empty, unresolved=UnresolvedPolicy.PLACEHOLDER)
    assert parse_m2(out, "o").textures[0]["filename"] == "unresolved\\blp\\900000.blp"
    assert any(n.code == "m2.reference.placeholder" for n in res.notes)

    out, res, _ = convert(raw, empty, unresolved=UnresolvedPolicy.STRIP)
    assert parse_m2(out, "o").textures[0]["filename"] == ""

    _out, res, _ = convert(raw, empty, unresolved=UnresolvedPolicy.FAIL)
    assert res.status is Status.FAILED


def test_sequence_blend_times_are_folded(modern_m2, listfile):
    _model, raw = modern_m2
    out, _res, _ = convert(raw, listfile)
    seq = parse_m2(out, "o.m2").sequences[0]
    assert "blend_time_in" not in seq
    assert seq["blend_time"] == 250  # max(in=150, out=250)


def test_camera_fov_track_becomes_a_scalar(modern_m2, listfile):
    _model, raw = modern_m2
    out, res, _ = convert(raw, listfile)
    cam = parse_m2(out, "o.m2").cameras[0]
    assert "fov_track" not in cam
    assert cam["fov"] == pytest.approx(0.8)
    assert any(n.code == "m2.camera.fov" and n.level == "lossy" for n in res.notes)


def test_particles_are_mapped_onto_the_wrath_layout(modern_m2, listfile):
    _model, raw = modern_m2
    out, res, _ = convert(raw, listfile)
    p = parse_m2(out, "o.m2").particles[0]
    assert "multi_texture_param0" not in p
    assert p["flags"] == 0x1                 # multi-texture bit cleared
    assert p["texture"] == 3                 # first of the three packed indices
    assert p["emitter_type"] == 1            # bone emitter -> plane
    assert p["particle_type"] == 0 and p["head_or_tail"] == 0
    codes = {n.code for n in res.notes}
    assert {"m2.particle.multitexture", "m2.particle.emitter_type",
            "m2.particle.flags"} <= codes


def test_post_wrath_flags_are_cleared(modern_m2, listfile):
    _model, raw = modern_m2
    back = parse_m2(convert(raw, listfile)[0], "o.m2")
    assert back.global_flags == 0x08         # 0x80 cleared, combiner bit kept
    assert back.bones[1]["flags"] == 0x200   # 0x400 kinematic bit cleared
    assert back.materials[0]["flags"] == 0x04
    assert back.materials[1]["blending_mode"] == 4   # BlendAdd -> Add


def test_ribbon_cataclysm_bytes_are_zeroed(modern_m2, listfile):
    _model, raw = modern_m2
    rib = parse_m2(convert(raw, listfile)[0], "o.m2").ribbons[0]
    assert rib["ribbon_color_index"] == 0
    assert rib["texture_transform_lookup_index"] == 0


def test_geometry_and_collision_survive_verbatim(modern_m2, listfile):
    model, raw = modern_m2
    back = parse_m2(convert(raw, listfile)[0], "o.m2")
    assert back.vertices == model.vertices
    assert back.collision_positions == model.collision_positions
    assert back.collision_indices == model.collision_indices
    assert back.texture_combiner_combos == [0, 1]
    assert back.bone_combos == model.bone_combos


def test_stripping_options(modern_m2, listfile):
    _model, raw = modern_m2
    out, res, _ = convert(raw, listfile, strip_particles=True, strip_ribbons=True,
                          strip_cameras=True, strip_lights=True)
    back = parse_m2(out, "o.m2")
    assert not back.particles and not back.ribbons
    assert not back.cameras and not back.lights
    codes = {n.code for n in res.notes}
    assert {"m2.particle.stripped", "m2.ribbon.stripped",
            "m2.camera.stripped", "m2.light.stripped"} <= codes


def test_dropped_chunks_are_reported(modern_m2, listfile):
    _model, raw = modern_m2
    _out, res, _ = convert(raw, listfile)
    note = next(n for n in res.notes if n.code == "m2.chunks.dropped")
    assert "PFID" in note.message and "LDV1" in note.message


def test_an_existing_wrath_model_is_passed_through(listfile):
    model = F.build_modern_model(version=264)
    model.global_flags = 0x08
    for m in model.materials:
        m["flags"] = 0x04
        m["blending_mode"] = min(m["blending_mode"], 6)
    for b in model.bones:
        b["flags"] &= 0x3FF
    for r in model.ribbons:
        r["ribbon_color_index"] = 0
        r["texture_transform_lookup_index"] = 0
    for p in model.particles:
        p["flags"] = 0x1
        p["emitter_type"] = 1
    for t, path in zip(model.textures, ("a.blp", "b.blp")):
        t["filename"] = path
    raw = F.serialise_modern_m2(model, chunked=False)
    _out, res, _ = convert_m2(raw, "old.m2", Options(), listfile, None)
    assert res.status is Status.PASSTHROUGH


def test_inspect_reports_compatibility(modern_m2):
    _model, raw = modern_m2
    info = inspect_m2(raw, "t.m2")
    assert info["version"] == 272 and info["chunked"] is True
    assert info["wotlk_compatible"] is False
    assert info["texture_file_ids"] == [900000, 900001]


def test_anim_filename_matches_the_client_convention():
    assert anim_filename("Bear.m2", 0, 0) == "Bear0000-00.anim"
    assert anim_filename("path/to/Bear.m2", 42, 3) == "Bear0042-03.anim"


# ---------------------------------------------------------------------------
# Sequences whose keyframes live in a sibling .anim
# ---------------------------------------------------------------------------
def test_a_sequence_flagged_external_is_not_read_out_of_the_model():
    """Its offsets address the .anim, so whatever is at them here is not it."""
    model = F.build_modern_model(sequences=2, external_sequence=1)
    raw = F.serialise_modern_m2(model)
    parsed = parse_m2(raw, "t.m2")
    translation = parsed.bones[0]["translation"]
    assert translation.external == {1}
    assert translation.values[1] == []
    # the embedded sequence is untouched
    assert translation.values[0] == model.bones[0]["translation"].values[0]


def test_a_flat_model_is_never_guessed_at():
    """Marking an embedded sequence external would throw its keyframes away."""
    model = F.build_modern_model(sequences=2, external_sequence=1, version=264)
    parsed = parse_m2(F.serialise_modern_m2(model, chunked=False), "t.m2")
    assert parsed.bones[0]["translation"].external == set()
    assert parsed.bones[0]["translation"].values[1]


def test_an_external_sub_array_keeps_the_offset_it_came_with():
    """Re-pointing it at the converted model would break the .anim link."""
    from wotlkconv.m2.types import DeferredWriter, M2TRACK_SIZE

    track = F.external_track("vec3", sequences=2, index=1, offset=4096)
    w = DeferredWriter()
    w.reserve(M2TRACK_SIZE)
    w.write_track(0, track)
    w.flush()
    out = w.getvalue()

    count, offset = struct.unpack_from("<II", out, 4)       # timestamps
    subs = [struct.unpack_from("<II", out, offset + i * 8) for i in range(count)]
    assert subs[1] == (2, 4096)          # verbatim, addressing the .anim
    assert subs[0] != (2, 4096)          # the embedded one was rewritten


def test_a_span_that_runs_off_the_end_is_treated_as_external_not_fatal():
    from wotlkconv.m2.types import StructReader, Track

    reader = StructReader(b"\0" * 64, "t.m2")
    track = Track(kind="vec3")
    assert reader._sub_array(track, 0, "vec3", (100, 900000)) == []
    assert track.external == {0}
