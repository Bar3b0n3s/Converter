"""A chunk nobody has seen before is named, not quietly discarded.

Each converter already lists the post-Wrath chunks it drops on purpose.  These
cover the other case: a chunk Blizzard adds after this tool was written, or one
nobody documented.  Dropping it is still the only option -- but saying which
chunk it was is the difference between a report and a shrug.
"""

import struct


import fixtures as F
from wotlkconv.adt.convert import AdtParts, convert_adt
from wotlkconv.adt.wdt import convert_wdt
from wotlkconv.listfile import Listfile
from wotlkconv.m2 import convert_m2
from wotlkconv.options import Options
from wotlkconv.wmo import convert_wmo_root


def with_chunk(data: bytes, name: str, payload: bytes = b"\xAA" * 64) -> bytes:
    """Append a chunk this tool has never heard of."""
    return data + name[::-1].encode() + struct.pack("<I", len(payload)) + payload


def codes(res) -> set[str]:
    return {n.code for n in res.notes}


def message(res, code: str) -> str:
    return next(n.message for n in res.notes if n.code == code)


def test_a_terrain_chunk_nobody_knows_is_named(listfile):
    root, tex, obj = F.build_split_adt(chunks=4)
    _out, res = convert_adt(AdtParts(with_chunk(root, "MXXX"), tex, obj),
                            "t.adt", Options(), listfile)
    assert "adt.chunks.unknown" in codes(res)
    assert "MXXX" in message(res, "adt.chunks.unknown")


def test_an_unknown_chunk_inside_a_map_chunk_is_named(listfile):
    """MCNK sub-chunks are dropped the same way, and counted the same way."""
    root, tex, obj = F.build_split_adt(chunks=4)
    _out, res = convert_adt(AdtParts(root, with_chunk(tex, "MCZZ"), obj),
                            "t.adt", Options(), listfile)
    assert "MCZZ" in message(res, "adt.chunks.unknown")


def test_a_map_index_chunk_nobody_knows_is_named():
    _out, res = convert_wdt(with_chunk(F.build_modern_wdt(), "MYYY"), "t.wdt",
                            Options())
    assert "MYYY" in message(res, "wdt.chunks.unknown")


def test_a_world_object_chunk_nobody_knows_is_named(listfile):
    raw = with_chunk(F.build_modern_wmo_root(groups=1), "MOZZ")
    _out, res, _extra = convert_wmo_root(raw, "t.wmo", Options(), listfile)
    assert "MOZZ" in message(res, "wmo.chunks.unknown")


def test_a_model_chunk_nobody_knows_is_named():
    model = F.build_modern_model()
    raw = F.serialise_modern_m2(model, skeleton_id=0,
                                extra_chunks=(("ZZZZ", b"\0" * 16),))
    _out, res, _extra = convert_m2(raw, "t.m2", Options(), Listfile())
    assert "ZZZZ" in message(res, "m2.chunks.unknown")
    # and it is distinguished from the ones dropped deliberately
    assert "unrecognised" in message(res, "m2.chunks.dropped")


def test_a_file_with_nothing_unexpected_says_nothing(listfile):
    root, tex, obj = F.build_split_adt(chunks=4)
    _out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(),
                            listfile)
    assert "adt.chunks.unknown" not in codes(res)


def test_the_chunk_names_are_carried_in_the_report_detail(listfile):
    """So a report reader can act on them without parsing prose."""
    root, tex, obj = F.build_split_adt(chunks=4)
    _out, res = convert_adt(AdtParts(with_chunk(root, "MXXX"), tex, obj),
                            "t.adt", Options(), listfile)
    note = next(n for n in res.notes if n.code == "adt.chunks.unknown")
    assert note.detail["chunks"] == ["MXXX"]


def test_an_unknown_chunk_makes_the_conversion_lossy(listfile):
    """Something was thrown away and nobody knows what: that is a loss."""
    from wotlkconv.report import Status

    root, tex, obj = F.build_split_adt(chunks=4)
    _out, res = convert_adt(AdtParts(with_chunk(root, "MXXX"), tex, obj),
                            "t.adt", Options(), listfile)
    assert res.status is Status.LOSSY
