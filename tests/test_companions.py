"""SKIN, ANIM and SKEL -- the files that travel with a model."""

import struct

import pytest

import fixtures as F
from wotlkconv.errors import MalformedFileError, UnsupportedFormatError
from wotlkconv.limits import SKIN_HEADER_SIZE_WOTLK
from wotlkconv.m2.anim import convert_anim, inspect_anim
from wotlkconv.m2.convert import convert_m2
from wotlkconv.m2.model import parse_m2
from wotlkconv.m2.skel import load_skeleton_chain, parse_skel
from wotlkconv.m2.skin import convert_skin, inspect_skin, parse_skin
from wotlkconv.options import Options
from wotlkconv.report import Status


# ---------------------------------------------------------------------------
# SKIN
# ---------------------------------------------------------------------------
def test_legion_and_wrath_headers_are_told_apart():
    assert parse_skin(F.build_skin(legion=True), "l").had_legion_header is True
    assert parse_skin(F.build_skin(legion=False), "w").had_legion_header is False


def test_shadow_batches_are_dropped(opts):
    out, res = convert_skin(F.build_skin(legion=True, shadow_batches=3), "t.skin", opts)
    assert res.status is Status.LOSSY
    assert any(n.code == "skin.shadow_batches" for n in res.notes)
    assert len(out) >= SKIN_HEADER_SIZE_WOTLK
    assert parse_skin(out, "o").had_legion_header is False


def test_geometry_survives_the_downgrade(opts):
    raw = F.build_skin(vertices=9, triangles=4, legion=True)
    out, _res = convert_skin(raw, "t.skin", opts)
    before, after = parse_skin(raw, "i"), parse_skin(out, "o")
    assert after.vertices == before.vertices
    assert after.indices == before.indices
    assert after.bones == before.bones
    assert after.submeshes == before.submeshes


def test_batch_texture_count_is_clamped(opts):
    out, res = convert_skin(F.build_skin(texture_count=4, shader_id=0), "t.skin", opts)
    batch = parse_skin(out, "o").batches[0]
    # textureCount sits at 0x0E; 0x10 is textureComboIndex and must be untouched.
    assert struct.unpack_from("<H", batch, 0x0E)[0] == 2
    assert struct.unpack_from("<H", batch, 0x10)[0] == 0
    assert any(n.code == "skin.batch.textures" for n in res.notes)


def test_combiner_shader_without_combiner_combos_is_reset(opts):
    out, res = convert_skin(F.build_skin(shader_id=0x8001), "t.skin", opts,
                            uses_combiner_combos=False)
    assert struct.unpack_from("<H", parse_skin(out, "o").batches[0], 0x02)[0] == 0
    assert any(n.code == "skin.batch.shader" for n in res.notes)


def test_combiner_shader_is_kept_when_the_model_carries_the_table(opts):
    out, res = convert_skin(F.build_skin(shader_id=0x8001), "t.skin", opts,
                            uses_combiner_combos=True)
    assert struct.unpack_from("<H", parse_skin(out, "o").batches[0], 0x02)[0] == 0x8001
    assert not any(n.code == "skin.batch.shader" for n in res.notes)


def test_a_wrath_skin_is_passed_through(opts):
    raw = F.build_skin(legion=False, texture_count=2, shader_id=0)
    _out, res = convert_skin(raw, "t.skin", opts)
    assert res.status is Status.PASSTHROUGH


def test_a_non_skin_is_rejected():
    with pytest.raises(UnsupportedFormatError):
        parse_skin(b"NOPE" + b"\0" * 100, "t.skin")


def test_inspect_skin_counts_everything():
    info = inspect_skin(F.build_skin(vertices=8, triangles=3, legion=True), "t")
    assert info["vertices"] == 8 and info["triangles"] == 3
    assert info["header"] == "legion" and info["shadow_batches"] == 2


# ---------------------------------------------------------------------------
# ANIM
# ---------------------------------------------------------------------------
def test_chunked_anim_is_unwrapped(opts):
    payload = b"\xAA\xBB" * 20
    out, res = convert_anim(F.build_anim(payload), "t.anim", opts)
    assert out == payload
    assert res.status is Status.LOSSY
    assert any(n.code == "anim.chunks.dropped" for n in res.notes)


def test_flat_anim_is_passed_through(opts):
    payload = b"\x01\x02\x03\x04" * 10
    out, res = convert_anim(F.build_anim(payload, chunked=False), "t.anim", opts)
    assert out == payload
    assert res.status is Status.PASSTHROUGH


def test_inspect_anim_detects_the_wrapper():
    assert inspect_anim(F.build_anim(), "t")["chunked"] is True
    assert inspect_anim(F.build_anim(chunked=False), "t")["chunked"] is False


def test_a_too_short_anim_is_an_error(opts):
    with pytest.raises(MalformedFileError):
        convert_anim(b"\x01\x02", "t.anim", opts)


# ---------------------------------------------------------------------------
# SKEL
# ---------------------------------------------------------------------------
def test_skeleton_sections_are_parsed():
    skel = parse_skel(F.build_skel(bones=4, sequences=3, attachments=2), "t.skel")
    assert skel.name == "TestSkeleton"
    assert len(skel.bones) == 4
    assert len(skel.sequences) == 3
    assert len(skel.attachments) == 2
    assert skel.global_loops == [1000]
    assert len(skel.bones[0]["translation"].values) == 3


def test_a_non_skeleton_is_rejected():
    with pytest.raises(UnsupportedFormatError):
        parse_skel(b"NOPE" + b"\0" * 100, "t.skel")


def build_skeleton_model():
    """A Legion character model: the rig lives entirely in the .skel."""
    m = F.build_modern_model(sequences=0, bones=0)
    m.bones = m.sequences = m.attachments = []
    m.key_bone_lookup = m.sequence_lookups = m.attachment_lookup = []
    return F.serialise_modern_m2(m, skeleton_id=940000, anim_ids=())


def test_external_skeleton_is_merged(listfile, source, asset_dir):
    (asset_dir / "940000.skel").write_bytes(F.build_skel(bones=4, sequences=2))
    raw = build_skeleton_model()
    assert parse_m2(raw, "c.m2").uses_external_skeleton
    out, res, _ = convert_m2(raw, "char.m2", Options(), listfile, source)
    back = parse_m2(out, "o.m2")
    assert len(back.bones) == 4 and len(back.sequences) == 2
    assert len(back.attachments) == 1
    assert back.sequences[0]["blend_time"] == 200
    assert any(n.code == "m2.skeleton.merged" for n in res.notes)


def test_missing_skeleton_fails_loudly(listfile, source):
    _out, res, _ = convert_m2(build_skeleton_model(), "char.m2", Options(),
                              listfile, source)
    assert res.status is Status.FAILED
    message = next(n.message for n in res.notes if n.level == "error")
    assert "--allow-missing-skeleton" in message


def test_missing_skeleton_can_be_overridden(listfile, source):
    out, res, _ = convert_m2(build_skeleton_model(), "char.m2",
                             Options(allow_missing_skeleton=True), listfile, source)
    assert res.status is Status.LOSSY
    assert parse_m2(out, "o.m2").bones == []


def test_parent_skeleton_chain_is_followed(source, asset_dir):
    (asset_dir / "940000.skel").write_bytes(F.build_skel(bones=4, sequences=3))
    (asset_dir / "941000.skel").write_bytes(
        F.build_skel(bones=0, sequences=0, attachments=0, parent_id=940000))
    merged = load_skeleton_chain(source.loader_for(".skel"), 941000, "child")
    assert len(merged.bones) == 4 and len(merged.sequences) == 3


def test_companions_are_found_and_renamed(listfile, source, asset_dir):
    for fid in (910000, 910001, 910002, 910003):
        (asset_dir / f"{fid}.skin").write_bytes(F.build_skin(legion=True))
    for fid in (920000, 920001):
        (asset_dir / f"{fid}.anim").write_bytes(F.build_anim())
    raw = F.serialise_modern_m2(F.build_modern_model())
    _out, _res, companions = convert_m2(raw, "123456.m2", Options(), listfile,
                                        source, output_stem="testbeast")
    names = sorted(c.filename for c in companions)
    assert names == ["testbeast00.skin", "testbeast0000-00.anim",
                     "testbeast0001-00.anim", "testbeast01.skin",
                     "testbeast02.skin", "testbeast03.skin"]
    assert all(c.data for c in companions)


def test_missing_skins_are_reported(listfile, source):
    raw = F.serialise_modern_m2(F.build_modern_model())
    _out, res, companions = convert_m2(raw, "t.m2", Options(), listfile, source)
    assert companions == []
    assert any(n.code == "m2.skin.missing" for n in res.notes)
