import pytest

import fixtures as F
from wotlkconv.blp import bcn
from wotlkconv.blp.blp import Blp, PreferredFormat
from wotlkconv.blp.convert import convert_blp, inspect_blp
from wotlkconv.blp.image import Image, is_pot, nearest_pot, next_pot, prev_pot
from wotlkconv.blp.quantize import PaletteMapper, build_palette
from wotlkconv.errors import UnsupportedFormatError
from wotlkconv.options import Options, TextureFormat
from wotlkconv.report import Status


def mean_abs_error(a: bytes, b: bytes, stride: int = 1, offset: int = 0) -> float:
    xa, xb = a[offset::stride], b[offset::stride]
    return sum(abs(p - q) for p, q in zip(xa, xb)) / max(1, len(xa))


# ---------------------------------------------------------------------------
# Block codecs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("encode,decode,block_bytes", [
    (lambda img: bcn.encode_bc1(img.data, img.width, img.height, None),
     bcn.decode_bc1, 8),
    (lambda img: bcn.encode_bc2(img.data, img.width, img.height), bcn.decode_bc2, 16),
    (lambda img: bcn.encode_bc3(img.data, img.width, img.height), bcn.decode_bc3, 16),
])
def test_block_codecs_round_trip_within_tolerance(encode, decode, block_bytes):
    img = F.build_gradient_image(32, 32)
    payload = encode(img)
    assert len(payload) == (32 // 4) * (32 // 4) * block_bytes
    back = Image(32, 32, decode(payload, 32, 32))
    assert mean_abs_error(img.data, back.data, 4, 0) < 6.0  # red channel


def test_bc3_preserves_an_alpha_gradient():
    img = F.build_gradient_image(32, 32, alpha="smooth")
    back = Image(32, 32, bcn.decode_bc3(
        bcn.encode_bc3(img.data, 32, 32), 32, 32))
    assert mean_abs_error(img.data, back.data, 4, 3) < 4.0


def test_bc1_punch_through_keeps_binary_alpha_binary():
    img = F.build_gradient_image(32, 32, alpha="binary")
    payload = bcn.encode_bc1(img.data, 32, 32, 128)
    back = Image(32, 32, bcn.decode_bc1(payload, 32, 32))
    assert set(back.data[3::4]) <= {0, 255}
    matches = sum(1 for a, b in zip(img.data[3::4], back.data[3::4]) if a == b)
    assert matches / (32 * 32) > 0.95


def test_flat_block_survives_encoding():
    img = Image.new(4, 4, (200, 100, 50, 255))
    back = Image(4, 4, bcn.decode_bc1(bcn.encode_bc1(img.data, 4, 4, None), 4, 4))
    assert back.data[0:3] == bytes((200, 100, 50))[0:3] or \
        mean_abs_error(img.data, back.data) < 4


def test_fully_transparent_block_stays_transparent():
    img = Image.new(4, 4, (10, 20, 30, 0))
    back = Image(4, 4, bcn.decode_bc1(bcn.encode_bc1(img.data, 4, 4, 128), 4, 4))
    assert set(back.data[3::4]) == {0}


def test_bc5_reconstructs_a_plausible_blue_channel():
    # A flat normal pointing straight up decodes to roughly (128, 128, 255).
    block = bytes((128, 128, 0, 0, 0, 0, 0, 0)) + bytes((128, 128, 0, 0, 0, 0, 0, 0))
    rgba = bcn.decode_bc5(block, 4, 4, reconstruct_z=True)
    assert rgba[0] == 128 and rgba[1] == 128
    assert rgba[2] > 250


def test_non_multiple_of_four_dimensions_are_padded():
    img = F.build_gradient_image(6, 5)
    payload = bcn.encode_bc3(img.data, 6, 5)
    assert len(payload) == 2 * 2 * 16
    back = Image(6, 5, bcn.decode_bc3(payload, 6, 5))
    assert len(back.data) == 6 * 5 * 4


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------
def test_power_of_two_helpers():
    assert (next_pot(100), prev_pot(100), nearest_pot(100)) == (128, 64, 128)
    assert nearest_pot(48) == 32  # exact tie prefers the smaller
    assert is_pot(256) and not is_pot(257)


def test_mip_chain_walks_down_to_one_pixel():
    chain = F.build_gradient_image(64, 16).mip_chain()
    assert [(m.width, m.height) for m in chain] == [
        (64, 16), (32, 8), (16, 4), (8, 2), (4, 1), (2, 1), (1, 1)]


def test_halving_a_uniform_image_is_lossless():
    img = Image.new(8, 8, (10, 20, 30, 40))
    assert set(img.halve().data) == {10, 20, 30, 40}


def test_bgra_round_trip_swaps_channels_back():
    img = F.build_gradient_image(8, 8)
    assert Image.from_bgra(8, 8, img.to_bgra()).data == img.data


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------
def test_blp1_is_rejected_with_a_useful_message():
    with pytest.raises(UnsupportedFormatError, match="BLP1"):
        Blp.parse(b"BLP1" + b"\0" * 100, "old.blp")


def test_header_is_exactly_1172_bytes():
    raw = F.build_blp(F.build_gradient_image(8, 8))
    assert Blp.parse(raw, "t").mips[0] == raw[1172:1172 + 16 * 4]


def test_compatible_texture_is_copied_through_bit_for_bit(opts):
    raw = F.build_blp(F.build_gradient_image(64, 64), PreferredFormat.DXT5)
    out, res = convert_blp(raw, "t.blp", opts)
    assert res.status is Status.PASSTHROUGH
    assert Blp.parse(out, "o").mips == Blp.parse(raw, "i").mips


def test_bc5_normal_map_is_transcoded(opts):
    raw = F.build_bc5_blp(32, 32)
    assert inspect_blp(raw, "n.blp")["wotlk_compatible"] is False
    out, res = convert_blp(raw, "n.blp", opts)
    assert res.status is Status.LOSSY
    assert {n.code for n in res.notes} >= {"blp.transcode", "blp.normalmap"}
    rt = Blp.parse(out, "o")
    assert rt.alpha_type == PreferredFormat.DXT1
    assert rt.mip_count == 6  # 32x32 down to 1x1


def test_non_power_of_two_is_resized(opts):
    raw = F.build_blp(F.build_gradient_image(48, 96, "opaque"), PreferredFormat.DXT1)
    out, res = convert_blp(raw, "npot.blp", opts)
    assert res.status is Status.LOSSY
    assert any(n.code == "blp.resize.npot" for n in res.notes)
    rt = Blp.parse(out, "o")
    assert is_pot(rt.width) and is_pot(rt.height)


def test_oversized_texture_is_capped():
    raw = F.build_blp(F.build_gradient_image(256, 256, "opaque"), PreferredFormat.DXT1)
    out, res = convert_blp(raw, "big.blp", Options(max_texture_size=64))
    rt = Blp.parse(out, "o")
    assert (rt.width, rt.height) == (64, 64)
    assert any(n.code == "blp.resize.cap" for n in res.notes)


def test_max_texture_size_zero_disables_the_cap():
    raw = F.build_blp(F.build_gradient_image(64, 64, "opaque"), PreferredFormat.DXT1)
    out, _ = convert_blp(raw, "t.blp", Options(max_texture_size=0))
    assert Blp.parse(out, "o").width == 64


def test_auto_format_picks_dxt1_for_opaque_and_dxt5_for_gradients(opts):
    opaque = F.build_blp(F.build_gradient_image(32, 32, "opaque"),
                         PreferredFormat.DXT3)
    out, _ = convert_blp(opaque, "o.blp", Options(texture_format=TextureFormat.AUTO,
                                                  force_power_of_two=False,
                                                  max_texture_size=16))
    assert Blp.parse(out, "o").alpha_type == PreferredFormat.DXT1

    smooth = F.build_blp(F.build_gradient_image(32, 32, "smooth"),
                         PreferredFormat.DXT3)
    out, _ = convert_blp(smooth, "s.blp", Options(max_texture_size=16))
    assert Blp.parse(out, "o").alpha_type == PreferredFormat.DXT5


def test_palettised_output_round_trips(opts):
    raw = F.build_blp(F.build_gradient_image(32, 32, "opaque"))
    out, res = convert_blp(raw, "ui.blp", Options(texture_format=TextureFormat.PAL))
    rt = Blp.parse(out, "o")
    assert rt.compression == 1 and rt.alpha_size == 0
    decoded = rt.decode_level(0)
    original = Blp.parse(raw, "i").decode_level(0)
    assert mean_abs_error(original.data, decoded.data, 4, 0) < 10


@pytest.mark.parametrize("alpha_bits", [1, 4, 8])
def test_palettised_alpha_planes_round_trip(alpha_bits):
    img = F.build_gradient_image(8, 8, "smooth")
    entries = build_palette(img.data, 256)
    mapper = PaletteMapper(entries)
    indices = mapper.map_image(img.data)
    if alpha_bits == 8:
        plane = img.data[3::4]
    elif alpha_bits == 4:
        plane = bytes(((img.data[3::4][i * 2 + 1] * 15 // 255) << 4)
                      | (img.data[3::4][i * 2] * 15 // 255) for i in range(32))
    else:
        plane = bytes(sum(((img.data[3::4][i * 8 + b] >= 128) << b) for b in range(8))
                      for i in range(8))
    blp = Blp(8, 8, 1, alpha_bits, 8, 0,
              [(r << 16) | (g << 8) | b for r, g, b in entries],
              [bytes(indices) + plane])
    out = blp.decode_level(0)
    assert len(out.data) == 8 * 8 * 4


def test_raw_bgra_textures_decode():
    img = F.build_gradient_image(8, 8)
    blp = Blp(8, 8, 3, 8, PreferredFormat.ARGB8888, 0, [0] * 256,
              [bytes(img.to_bgra())])
    assert blp.decode_level(0).data == img.data


def test_palette_stays_within_256_entries():
    img = F.build_gradient_image(64, 64)
    assert len(build_palette(img.data, 256)) == 256
