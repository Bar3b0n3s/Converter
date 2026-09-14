"""Every file gets a deliberate decision, named or not."""

import pytest

from wotlkconv import detect

#: One real signature per format a modern build ships, so detection is tested
#: against the bytes the client actually writes rather than against itself.
SIGNATURES = {
    detect.WAV: b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x02\x00",
    detect.AVI: b"RIFF\x24\x08\x00\x00AVI LIST\x00\x01\x00\x00hdrl",
    detect.WEM: b"RIFF\x24\x08\x00\x00WAVEfmt \x18\x00\x00\x00\xff\xff\x02\x00",
    detect.OGG: b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
    detect.MP3: b"ID3\x03\x00\x00\x00\x00\x00\x21\x00\x00\x00\x00",
    detect.MP4: b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00mp41",
    detect.BNK: b"BKHD\x1c\x00\x00\x00\x8d\x00\x00\x00\x00\x00\x00\x00",
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
def test_wwise_media_is_told_apart_from_a_sound_the_client_can_play():
    """Both are RIFF/WAVE; only one is worth putting in a patch."""
    assert detect.detect(SIGNATURES[detect.WAV], "") == detect.WAV
    assert detect.detect(SIGNATURES[detect.WEM], "") == detect.WEM
    assert detect.classify(detect.WAV, "a.wav")[0] == detect.COPY
    assert detect.classify(detect.WEM, "a.wem")[0] == detect.SKIP


def test_wwise_media_is_recognised_by_its_own_chunks_too():
    """Not every .wem announces itself with the vendor format tag."""
    vorbis = (b"RIFF\x24\x08\x00\x00WAVE"
              + b"fmt \x10\x00\x00\x00" + b"\x01\x00\x02\x00" + b"\x00" * 12
              + b"vorb\x2a\x00\x00\x00")
    assert detect.detect(vorbis, "") == detect.WEM


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
MODERN_EXTENSIONS = [
    ".m2", ".skin", ".anim", ".skel", ".phys", ".bone",
    ".blp", ".tex", ".wmo", ".adt", ".wdt", ".wdl",
    ".wlw", ".wlq", ".wlm",
    ".db2", ".dbc", ".sig", ".meta", ".blob", ".trs", ".zmp",
    ".lua", ".xml", ".toc", ".xsd", ".html", ".css", ".js", ".txt",
    ".wav", ".mp3", ".ogg", ".bnk", ".wem",
    ".avi", ".mp4", ".ttf", ".otf", ".bls",
    ".pd4", ".pm4", ".lit", ".def", ".tga", ".dds", ".png",
    ".wtf", ".ini", ".sbt", ".cfg",
]


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
