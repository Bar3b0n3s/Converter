import pytest

from wotlkconv.chunks import ChunkReader, ChunkWriter, detect_reversal
from wotlkconv.errors import MalformedFileError


def test_detects_reversed_magics():
    cw = ChunkWriter(reverse=True)
    cw.add("MVER", b"\x12\0\0\0")
    assert cw.getvalue()[:4] == b"REVM"
    assert detect_reversal(cw.getvalue(), {"MVER", "MHDR"}) is True


def test_detects_forward_magics():
    cw = ChunkWriter(reverse=False)
    cw.add("MD21", b"\0" * 4)
    assert cw.getvalue()[:4] == b"MD21"
    assert detect_reversal(cw.getvalue(), {"MD21", "SFID"}) is False


def test_round_trip_preserves_payloads():
    cw = ChunkWriter(reverse=True)
    cw.add("MVER", b"\x11\0\0\0")
    cw.add("MOHD", b"x" * 64)
    chunks = {c.name: c.data for c in ChunkReader(cw.getvalue(), reverse=True)}
    assert chunks["MVER"] == b"\x11\0\0\0"
    assert chunks["MOHD"] == b"x" * 64


def test_oversized_final_chunk_is_clamped_not_rejected():
    # Blizzard occasionally ships a last chunk whose size runs a few bytes long.
    raw = b"REVM" + (100).to_bytes(4, "little") + b"\x12\0\0\0"
    chunks = list(ChunkReader(raw, reverse=True))
    assert len(chunks) == 1
    assert chunks[0].data == b"\x12\0\0\0"


def test_chunk_starting_past_eof_is_an_error():
    raw = b"REVM" + (100).to_bytes(4, "little")
    with pytest.raises(MalformedFileError):
        list(ChunkReader(raw, reverse=True))


def test_chunk_name_must_be_four_characters():
    with pytest.raises(ValueError):
        ChunkWriter().add("ABC", b"")
