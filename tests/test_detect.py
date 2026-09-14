"""Every file gets a deliberate decision, named or not."""

import pytest

from listfile_extensions import LISTFILE_EXTENSIONS, LISTFILE_TOTAL
from wotlkconv import detect

#: One real signature per format a modern build ships, so detection is tested
#: against the bytes the client actually writes rather than against itself.
SIGNATURES = {
    detect.WAV: b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x02\x00",
    detect.AVI: b"RIFF\x24\x08\x00\x00AVI LIST\x00\x01\x00\x00hdrl",
    detect.OGG: b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
    detect.MP3: b"ID3\x03\x00\x00\x00\x00\x00\x21\x00\x00\x00\x00",
    detect.MP4: b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00mp41",
    detect.TTF: b"\x00\x01\x00\x00\x00\x0c\x00\x80\x00\x03\x00\x20",
    detect.OTF: b"OTTO\x00\x0c\x00\x80\x00\x03\x00\x20\x00\x00",
    detect.DDS: b"DDS \x7c\x00\x00\x00\x07\x10\x00\x00\x00\x01\x00\x00",
    detect.PNG: b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR",
    detect.BLS: b"GXSH\x03\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00",
    detect.TEXT: b"## Interface: 110005\n## Title: Something\n",
}


@pytest.mark.parametrize("kind,data", sorted(SIGNATURES.items()))
def test_every_format_is_recognised_from_its_bytes_alone(kind, data):
    """A CASC build has no filenames; the listfile never covers everything."""
    assert detect.detect(data, "") == kind


@pytest.mark.parametrize("kind,data", sorted(SIGNATURES.items()))
def test_a_nameless_file_still_lands_under_its_own_extension(kind, data):
    assert detect.EXTENSIONS[detect.detect(data, "")] != ".bin"


@pytest.mark.parametrize("kind", sorted(SIGNATURES))
def test_every_detected_kind_has_a_decision(kind):
    action, _reason = detect.classify(kind, f"x{detect.EXTENSIONS[kind]}")
    assert action in (detect.CONVERT, detect.COPY, detect.SKIP)


def test_a_decision_to_skip_always_says_why():
    for kind in sorted(SIGNATURES):
        action, reason = detect.classify(kind, "")
        if action == detect.SKIP:
            assert reason, f"{kind} is skipped without a reason"


# ---------------------------------------------------------------------------
# The families that are easy to confuse
# ---------------------------------------------------------------------------
def test_a_wave_with_an_unusual_format_tag_is_still_a_sound():
    """WoW ships no Wwise, so nothing may reclassify a WAVE out of being one."""
    odd = (b"RIFF\x24\x08\x00\x00WAVE"
           + b"fmt \x10\x00\x00\x00" + b"\xff\xff\x02\x00" + b"\x00" * 12)
    assert detect.detect(odd, "") == detect.WAV
    assert detect.classify(detect.WAV, "a.wav")[0] == detect.COPY


def test_an_avi_is_not_mistaken_for_a_sound():
    assert detect.detect(SIGNATURES[detect.AVI], "") == detect.AVI
    assert detect.classify(detect.AVI, "movie.avi")[0] == detect.COPY


def test_text_detection_does_not_swallow_binary_data():
    """It runs last, and only on bytes that really read as text."""
    assert detect.detect(b"\x00\x01\x02\x03" * 8, "x.bin") == detect.UNKNOWN
    assert detect.detect(b"plain ascii, nothing else here\n", "") == detect.TEXT


def test_a_named_text_file_keeps_its_own_extension():
    """Detection says 'text'; the path still decides what it is called."""
    assert detect.detect(b"-- lua\nlocal x = 1\n", "Foo.lua") == detect.TEXT
    assert detect.classify(detect.TEXT, "Interface/Foo.lua")[0] == detect.COPY


# ---------------------------------------------------------------------------
# Nothing falls through
# ---------------------------------------------------------------------------
#: Every extension a modern build actually ships.
#: Measured, not remembered -- see tests/listfile_extensions.py.
MODERN_EXTENSIONS = sorted(e for e in LISTFILE_EXTENSIONS if e)


@pytest.mark.parametrize("ext", MODERN_EXTENSIONS)
def test_no_modern_extension_falls_into_the_catch_all(ext):
    """'unrecognised format X' is for formats nobody anticipated, not these."""
    _action, reason = detect.classify(detect.UNKNOWN, f"something{ext}")
    assert "unrecognised" not in reason, f"{ext} has no deliberate decision"


def test_a_file_that_lies_about_its_extension_is_refused_not_copied():
    """A .m2 that is not a model would be a broken file in the patch."""
    action, reason = detect.classify(detect.UNKNOWN, "creature/bear/bear.m2")
    assert action == detect.SKIP
    assert "named as a model" in reason and "truncated" in reason


@pytest.mark.parametrize("ext", MODERN_EXTENSIONS)
def test_every_modern_extension_is_copied_or_skipped_with_a_reason(ext):
    action, reason = detect.classify(detect.UNKNOWN, f"something{ext}")
    assert action in (detect.CONVERT, detect.COPY, detect.SKIP)
    if action == detect.SKIP:
        assert reason


def test_a_format_nobody_anticipated_is_still_carried_through():
    """Better a file the client ignores than a file the patch is missing."""
    action, reason = detect.classify(detect.UNKNOWN, "thing.qqq")
    assert action == detect.COPY and "unrecognised" in reason


def test_the_copy_and_skip_tables_do_not_disagree():
    overlap = set(detect.COPY_EXTENSIONS) & set(detect.UNSUPPORTED_EXTENSIONS)
    assert not overlap, f"{overlap} are both copied and skipped"


def test_every_kind_action_names_a_real_kind():
    assert set(detect.KIND_ACTIONS) <= set(detect.EXTENSIONS)
    assert not set(detect.KIND_ACTIONS) & detect.CONVERTIBLE


# ---------------------------------------------------------------------------
# Map sidecars
# ---------------------------------------------------------------------------
def chunked(items) -> bytes:
    import struct
    return b"".join(name[::-1].encode() + struct.pack("<I", len(payload)) + payload
                    for name, payload in items)


SIDECARS = {
    "_lgt.wdt": [("MVER", b"\0" * 4), ("MPLT", b"\0" * 32)],
    "_occ.wdt": [("MVER", b"\0" * 4), ("MAOI", b"\0" * 16)],
    "_fogs.wdt": [("MVER", b"\0" * 4), ("MVFX", b"\0" * 16)],
    "_mpv.wdt": [("MVER", b"\0" * 4), ("MPVD", b"\0" * 16)],
    "_lod.adt": [("MVER", b"\0" * 4), ("MLHD", b"\0" * 16)],
}


@pytest.mark.parametrize("suffix,items", sorted(SIDECARS.items()))
def test_a_map_sidecar_is_not_mistaken_for_the_map_file(suffix, items):
    """They share an extension with the tile they sit beside."""
    path = f"world/maps/azeroth/azeroth{suffix}"
    assert detect.detect(chunked(items), path) == detect.MAP_SIDECAR


@pytest.mark.parametrize("suffix,items", sorted(SIDECARS.items()))
def test_a_map_sidecar_is_skipped_saying_what_it_held(suffix, items):
    path = f"world/maps/azeroth/azeroth{suffix}"
    action, reason = detect.classify(detect.MAP_SIDECAR, path)
    assert action == detect.SKIP
    assert "never looks for the file" in reason
    assert reason != "a map sidecar carrying data added after Wrath; " \
                     "3.3.5a keeps none of this and never looks for the file"


def test_a_real_wdt_is_still_a_wdt():
    """The sidecar check must not swallow the map index itself."""
    real = chunked([("MVER", b"\0" * 4), ("MPHD", b"\0" * 32), ("MAIN", b"\0" * 64)])
    assert detect.detect(real, "azeroth.wdt") == detect.WDT


# ---------------------------------------------------------------------------
# The tables against what the game actually ships
# ---------------------------------------------------------------------------
def declared_extensions() -> set[str]:
    """Every extension the tool claims to have an opinion about."""
    return (set(detect.COPY_EXTENSIONS) | set(detect.UNSUPPORTED_EXTENSIONS)
            | set(detect.CONVERTIBLE_EXTENSIONS) | set(detect.SYSTEM_EXTENSIONS)
            | set(detect.PLACEHOLDER_EXTENSIONS))


#: Formats World of Warcraft does not ship but a user might still hand us,
#: each stated as a fact about 3.3.5a rather than a claim about the game.
NOT_SHIPPED_BUT_DECIDED = {".dds", ".mp4", ".otf", ".wlq"}


def test_no_table_entry_is_invented():
    """Every claimed extension is one the game really has, or a known input."""
    invented = declared_extensions() - set(LISTFILE_EXTENSIONS)
    assert invented <= NOT_SHIPPED_BUT_DECIDED, (
        f"{sorted(invented - NOT_SHIPPED_BUT_DECIDED)} are claimed by the "
        f"tables but appear nowhere in {LISTFILE_TOTAL:,} real filenames")


@pytest.mark.parametrize("ext", NOT_SHIPPED_BUT_DECIDED)
def test_a_format_the_game_lacks_is_described_by_what_wrath_does(ext):
    """It may say what 3.3.5a reads; it may not imply WoW ships the format."""
    _action, reason = detect.classify(detect.UNKNOWN, f"x{ext}")
    assert "3.3.5a" in reason or "liquid" in reason


def test_every_extension_the_game_ships_is_decided_deliberately():
    missing = []
    for ext, (count, sample) in LISTFILE_EXTENSIONS.items():
        if not ext:
            continue
        _action, reason = detect.classify(detect.UNKNOWN, f"file{ext}")
        if "unrecognised" in reason:
            missing.append((ext, count, sample))
    assert not missing, (
        "these really ship and fall into the catch-all: "
        + ", ".join(f"{e} ({n} files, e.g. {s})" for e, n, s in missing))


def test_the_decided_share_of_the_game_is_total():
    """Weighted by how many files there are, not how many extensions."""
    decided = sum(n for e, (n, _s) in LISTFILE_EXTENSIONS.items() if e
                  and "unrecognised" not in
                  detect.classify(detect.UNKNOWN, f"f{e}")[1])
    named = sum(n for e, (n, _s) in LISTFILE_EXTENSIONS.items() if e)
    assert decided == named


def test_the_biggest_formats_are_the_ones_worth_converting():
    """A sanity check on the census itself, not on the tables."""
    top = sorted(LISTFILE_EXTENSIONS.items(), key=lambda kv: -kv[1][0])
    assert [e for e, _ in top[:6]] == [".blp", ".adt", ".skin", ".ogg",
                                       ".m2", ".wmo"]
    for ext in (".blp", ".adt", ".skin", ".m2", ".wmo"):
        assert detect.classify(detect.UNKNOWN, f"x{ext}")[0] == detect.SKIP
        # ... by name alone; by content they are converted
    assert detect.CONVERTIBLE >= {detect.BLP, detect.ADT, detect.SKIN,
                                  detect.M2, detect.WMO_ROOT}
