"""CASC reading: BLTE, indices, encoding, root, and the whole chain."""

import struct

import pytest

import casc_fixtures as CF
from wotlkconv.casc import CascStorage, KeyRing, blte
from wotlkconv.casc.blte import EncryptedChunkError
from wotlkconv.casc.config import parse_build_info, parse_config
from wotlkconv.casc.encoding import EncodingTable
from wotlkconv.casc.index import bucket_for, parse_index
from wotlkconv.casc.root import RootTable
from wotlkconv.casc.salsa20 import arc4, salsa20
from wotlkconv.casc.storage import FileNotInstalledError
from wotlkconv.errors import MalformedFileError, MissingDependencyError


# ---------------------------------------------------------------------------
# Ciphers (published test vectors)
# ---------------------------------------------------------------------------
def test_salsa20_matches_the_reference_vector():
    # ECRYPT set 1, 256-bit all-zero key and nonce.
    assert salsa20(bytes(32), bytes(8), bytes(64))[:8].hex() == "9a97f65b9b4c721b"


def test_salsa20_accepts_a_128_bit_key():
    assert len(salsa20(bytes(16), bytes(8), bytes(32))) == 32


def test_salsa20_is_its_own_inverse():
    key, nonce = bytes(range(32)), bytes(range(8))
    plain = b"the quick brown fox" * 7
    assert salsa20(key, nonce, salsa20(key, nonce, plain)) == plain


def test_arc4_matches_the_classic_vector():
    assert arc4(b"Key", b"", b"Plaintext").hex() == "bbf316e8d940af0ad3"


# ---------------------------------------------------------------------------
# BLTE
# ---------------------------------------------------------------------------
def test_single_chunk_stream_round_trips():
    payload = b"hello casc " * 300
    assert blte.decode(CF.make_blte(payload)) == payload


def test_multi_chunk_stream_round_trips():
    payload = bytes(range(256)) * 40
    assert blte.decode(CF.make_blte(payload, chunk_size=97)) == payload


def test_headerless_stream_round_trips():
    payload = b"small file"
    assert blte.decode(CF.make_blte_single(payload)) == payload


def test_stored_chunks_round_trip():
    payload = b"incompressible" * 10
    assert blte.decode(CF.make_blte(payload, mode=b"N")) == payload


def test_nested_frames_are_decoded():
    payload = b"nested" * 50
    inner = CF.make_blte(payload)
    outer = b"BLTE" + struct.pack(">I", 0) + b"F" + inner
    assert blte.decode(outer) == payload


def test_encrypted_chunk_without_a_key_says_which_key():
    key_name = 0x0123456789ABCDEF
    body = b"F" + CF.make_blte(b"secret")
    encrypted = salsa20(bytes(16), b"\0" * 4, body)
    chunk = (b"E" + bytes([8]) + key_name.to_bytes(8, "little")
             + bytes([4]) + b"\0" * 4 + b"S" + encrypted)
    stream = b"BLTE" + struct.pack(">I", 0) + chunk
    with pytest.raises(EncryptedChunkError) as exc:
        blte.decode(stream, KeyRing())
    assert f"{key_name:016X}" in str(exc.value)


def test_encrypted_chunk_decodes_with_the_key():
    key_name = 0x0123456789ABCDEF
    key = bytes(range(16))
    payload = b"unreleased content"
    body = b"N" + payload
    iv = b"\x01\x02\x03\x04"
    mixed = bytearray(iv)
    for i in range(4):
        mixed[i] ^= (0 >> (i * 8)) & 0xFF   # chunk index 0
    chunk = (b"E" + bytes([8]) + key_name.to_bytes(8, "little")
             + bytes([4]) + iv + b"S"
             + salsa20(key, bytes(mixed), body))
    stream = b"BLTE" + struct.pack(">I", 0) + chunk
    ring = KeyRing()
    ring.add(key_name, key)
    assert blte.decode(stream, ring) == payload


def test_a_non_blte_buffer_is_rejected():
    with pytest.raises(MalformedFileError):
        blte.decode(b"NOPE" + b"\0" * 20)


def test_corrupt_zlib_is_reported_clearly():
    chunk = b"Z" + b"\xff" * 20
    stream = b"BLTE" + struct.pack(">I", 0) + chunk
    with pytest.raises(MalformedFileError, match="inflate"):
        blte.decode(stream)


def test_lz4_literal_only_block_decodes():
    # token 0x50 = 5 literals, no match
    payload = b"abcde"
    stream = b"BLTE" + struct.pack(">I", 0) + b"4" + bytes([0x50]) + payload
    assert blte.decode(stream) == payload


def test_lz4_match_copying_decodes():
    """The match path is what makes LZ4 a compressor; literals alone never
    exercise it."""
    # 4 literals "abcd", then a match of 4 bytes at offset 4 -> "abcdabcd"
    block = bytes([0x40]) + b"abcd" + bytes([0x04, 0x00])
    stream = b"BLTE" + struct.pack(">I", 0) + b"4" + block
    assert blte.decode(stream) == b"abcdabcd"


def test_lz4_overlapping_match_repeats_a_run():
    # 1 literal "a", then a 7-byte match at offset 1: the classic run-length case
    block = bytes([0x13]) + b"a" + bytes([0x01, 0x00])
    stream = b"BLTE" + struct.pack(">I", 0) + b"4" + block
    assert blte.decode(stream) == b"a" * 8


def test_lz4_extended_literal_length_decodes():
    """A literal run of 15 or more is extended by the bytes after the token."""
    literals = bytes(range(32))
    block = bytes([0xF0, 17]) + literals + bytes([0x20, 0x00])
    out = blte.decode(b"BLTE" + struct.pack(">I", 0) + b"4" + block)
    assert out.startswith(literals)
    assert out == (literals * 2)[:len(out)]


def test_a_zero_offset_lz4_match_is_rejected():
    block = bytes([0x40]) + b"abcd" + bytes([0x00, 0x00])
    stream = b"BLTE" + struct.pack(">I", 0) + b"4" + block
    with pytest.raises(MalformedFileError, match="zero match offset"):
        blte.decode(stream)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def test_build_info_columns_lose_their_type_suffix():
    rows = parse_build_info(
        "Branch!STRING:0|Active!DEC:1|Build Key!HEX:16\nus|1|deadbeef\n")
    assert rows == [{"Branch": "us", "Active": "1", "Build Key": "deadbeef"}]


def test_config_values_split_on_whitespace():
    cfg = parse_config("encoding = aaaa bbbb\n# comment\nroot = cccc\n")
    assert cfg["encoding"] == ["aaaa", "bbbb"]
    assert cfg["root"] == ["cccc"]


def test_opening_a_directory_that_is_not_an_install_explains_itself(tmp_path):
    with pytest.raises(MissingDependencyError, match=".build.info"):
        CascStorage.open(tmp_path)


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
def test_index_round_trips_locations():
    ekey = bytes(range(16))
    raw = CF.make_index(bucket_for(ekey), {ekey: (3, 0x1234, 500)})
    entries = parse_index(raw, "test.idx")
    entry = entries[ekey[:9]]
    assert (entry.archive, entry.offset, entry.size) == (3, 0x1234, 500)


def test_index_bucket_matches_the_fixture():
    for i in range(32):
        ekey = bytes([i]) + bytes(range(15))
        assert bucket_for(ekey) == CF.bucket_for(ekey)


def test_unknown_index_version_is_rejected():
    raw = bytearray(CF.make_index(0, {}))
    struct.pack_into("<H", raw, 8, 9)
    with pytest.raises(MalformedFileError, match="version 9"):
        parse_index(bytes(raw), "bad.idx")


# ---------------------------------------------------------------------------
# Encoding and root
# ---------------------------------------------------------------------------
def test_encoding_table_maps_content_keys_to_encoding_keys():
    entries = {bytes([i]) * 16: (bytes([i + 100]) * 16, i * 1000)
               for i in range(1, 60)}
    table = EncodingTable.parse(CF.make_encoding(entries))
    assert len(table) == len(entries)
    for ckey, (ekey, size) in entries.items():
        assert table.ekey_for(ckey) == ekey
        assert table.size_for(ckey) == size


def test_encoding_table_spans_multiple_pages():
    # 4 KiB pages hold ~107 entries, so 400 forces several.
    entries = {i.to_bytes(16, "big"): (bytes([i % 251]) * 16, i)
               for i in range(400)}
    table = EncodingTable.parse(CF.make_encoding(entries, page_kib=4))
    assert len(table) == 400


def test_root_table_decodes_file_id_deltas():
    entries = {1: b"a" * 16, 5: b"b" * 16, 1000000: b"c" * 16}
    table = RootTable.parse(CF.make_root(entries))
    assert sorted(table.file_ids()) == [1, 5, 1000000]
    for fid, ckey in entries.items():
        assert table.ckey_for(fid) == ckey


def test_root_table_reads_the_all_locales_flag():
    # 0xFFFFFFFF must not be read as a negative signed value.
    entries = {7: b"z" * 16}
    table = RootTable.parse(CF.make_root(entries, locale_flags=0xFFFFFFFF))
    assert table.ckey_for(7) == b"z" * 16


def test_root_table_reads_the_legacy_and_version_2_layouts():
    entries = {3: b"q" * 16, 9: b"r" * 16}
    for kwargs in ({"mfst": False}, {"mfst": True}, {"version2": True}):
        table = RootTable.parse(CF.make_root(entries, **kwargs))
        assert sorted(table.file_ids()) == [3, 9], kwargs


def test_root_table_skips_name_hashes_when_present():
    entries = {2: b"n" * 16, 4: b"m" * 16}
    table = RootTable.parse(CF.make_root(entries, with_names=True))
    assert sorted(table.file_ids()) == [2, 4]


def test_an_empty_root_is_an_error():
    with pytest.raises(MalformedFileError):
        RootTable.parse(b"TSFM" + struct.pack("<II", 0, 0))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
@pytest.fixture
def install(tmp_path):
    files = {
        123456: b"MD21" + b"\x00" * 200,
        900000: b"BLP2" + b"\x01" * 500,
        700001: b"a" * 20000,
        1: b"tiny",
    }
    CF.build_install(tmp_path / "wow", files)
    return tmp_path / "wow", files


def test_every_file_reads_back_byte_for_byte(install):
    root, files = install
    with CascStorage.open(root) as storage:
        for file_id, expected in files.items():
            assert storage.read_file_id(file_id) == expected


def test_stats_describe_the_build(install):
    root, files = install
    with CascStorage.open(root) as storage:
        stats = storage.stats()
        assert stats.product == "wow"
        assert stats.version == "11.0.5.57212"
        assert stats.files == len(files)
        assert stats.index_buckets >= 1


def test_a_file_outside_the_build_is_reported(install):
    root, _files = install
    with CascStorage.open(root) as storage:
        data, why = storage.try_read_file_id(999999)
        assert data is None and "root table" in why


def test_an_install_with_no_archive_fails_to_open(tmp_path):
    CF.build_install(tmp_path / "wow", {5: b"payload" * 100})
    (tmp_path / "wow" / "Data" / "data" / "data.000").unlink()
    with pytest.raises(FileNotInstalledError, match="data.000"):
        CascStorage.open(tmp_path / "wow")


def test_a_file_streamed_from_the_cdn_is_reported_not_guessed(install):
    """Partial installs leave files out of the local index entirely."""
    root, _files = install
    with CascStorage.open(root) as storage:
        with pytest.raises(FileNotInstalledError, match="CDN"):
            storage._read_by_ekey(b"\xee" * 16, "absent file")


def test_an_install_with_no_indices_explains_itself(tmp_path):
    CF.build_install(tmp_path / "wow", {5: b"x" * 50})
    for idx in (tmp_path / "wow" / "Data" / "data").glob("*.idx"):
        idx.unlink()
    with pytest.raises(MissingDependencyError, match="idx"):
        CascStorage.open(tmp_path / "wow")


def test_multi_chunk_storage_reads(tmp_path):
    files = {9: bytes(range(256)) * 200}
    CF.build_install(tmp_path / "wow", files, chunk_size=1024)
    with CascStorage.open(tmp_path / "wow") as storage:
        assert storage.read_file_id(9) == files[9]


def test_asking_for_the_wrong_product_lists_what_is_there(install):
    root, _files = install
    with pytest.raises(MissingDependencyError, match="wow"):
        CascStorage.open(root, product="wow_classic_era")


def test_an_unknown_locale_is_rejected(install):
    root, _files = install
    with pytest.raises(MissingDependencyError, match="unknown locale"):
        CascStorage.open(root, locale="elvish")


# ---------------------------------------------------------------------------
# Key ring
# ---------------------------------------------------------------------------
def test_key_ring_parses_the_community_format(tmp_path):
    path = tmp_path / "WoW.txt"
    path.write_text("FA505078126ACB3E 393ED9CD8AF8E5E0E31D9B6D0B0F3B0B\n"
                    "# a comment\n"
                    "BAD LINE\n")
    ring = KeyRing.load(path)
    assert len(ring) == 1
    assert ring.get(0xFA505078126ACB3E) == bytes.fromhex(
        "393ED9CD8AF8E5E0E31D9B6D0B0F3B0B")


def test_an_empty_key_ring_is_falsy():
    assert not KeyRing()


# ---------------------------------------------------------------------------
# How much of the build is actually here
# ---------------------------------------------------------------------------
@pytest.fixture
def partial_install(tmp_path):
    """An install like a real one: listed in full, downloaded in part."""
    return CF.build_install(
        tmp_path / "game",
        {100: b"stored one", 101: b"stored two"},
        not_downloaded={200: b"on the cdn", 201: b"also on the cdn",
                        202: b"and this"},
        no_encoding={300: b"no key at all"})


def test_coverage_separates_stored_from_streamed(partial_install):
    with CascStorage.open(partial_install) as storage:
        coverage = storage.coverage()
    assert coverage.listed == 6
    assert coverage.local == 2
    assert coverage.not_downloaded == 3
    assert coverage.no_encoding == 1
    assert coverage.fraction == pytest.approx(2 / 6)


def test_coverage_describes_itself_in_terms_of_the_cdn(partial_install):
    with CascStorage.open(partial_install) as storage:
        text = storage.coverage().describe()
    assert "2 of 6 files" in text and "33.3%" in text
    assert "3 would have to come from the CDN" in text


def test_a_file_that_is_only_listed_says_so_rather_than_being_fetched(
        partial_install):
    with CascStorage.open(partial_install) as storage:
        data, why = storage.try_read_file_id(200)
    assert data is None
    assert "streams it from the CDN rather than storing it" in why


def test_coverage_can_be_sampled_on_a_large_build(tmp_path):
    install = CF.build_install(tmp_path / "game",
                               {i: b"x" * 8 for i in range(100)})
    with CascStorage.open(install) as storage:
        full = storage.coverage()
        sampled = storage.coverage(sample=10)
    assert full.sampled == 0 and full.listed == 100 and full.local == 100
    assert sampled.listed == 100 and sampled.measured == 10
    assert sampled.local == 10 and sampled.fraction == 1.0


def test_a_sample_larger_than_the_build_measures_all_of_it(partial_install):
    with CascStorage.open(partial_install) as storage:
        assert storage.coverage(sample=1000).sampled == 0


def test_casc_info_states_the_cdn_boundary(partial_install, capsys):
    from wotlkconv.cli import main

    assert main(["casc", "info", "--casc", str(partial_install)]) == 0
    out = capsys.readouterr().out
    assert "local      2 of 6 files" in out
    assert "never fetches from the CDN" in out


def test_casc_info_on_a_complete_install_does_not_warn(tmp_path, capsys):
    install = CF.build_install(tmp_path / "game", {1: b"all here"})
    from wotlkconv.cli import main

    assert main(["casc", "info", "--casc", str(install)]) == 0
    out = capsys.readouterr().out
    assert "1 of 1 files (100.0%)" in out
    assert "never fetches from the CDN" not in out
