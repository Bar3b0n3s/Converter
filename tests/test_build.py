"""One command that turns a game install into a finished patch."""

import json
import zipfile

import pytest

import casc_fixtures as CF
import fixtures as F
from wotlkconv.cli import main
from wotlkconv.errors import ConverterError
from wotlkconv.fetch import (default_cache, fetch_definitions, fetch_listfile)


@pytest.fixture
def install(tmp_path):
    """A small CASC install plus the listfile that names its files."""
    model = F.build_modern_model()
    files = {
        1001: F.serialise_modern_m2(model, skeleton_id=0, skin_ids=(1002,),
                                    anim_ids=()),
        1002: F.build_skin(legion=True),
        1003: F.build_blp(F.build_gradient_image(8, 8)),
        1004: b"OggS" + b"\0" * 60,
    }
    root = CF.build_install(tmp_path / "game", files)
    listfile = tmp_path / "listfile.csv"
    listfile.write_text(
        "1001;creature/bear/bear.m2\n1002;creature/bear/bear00.skin\n"
        "1003;creature/bear/bear.blp\n1004;sound/creature/bear/growl.ogg\n")
    return root, listfile


def written(out):
    return sorted(p.relative_to(out).as_posix()
                  for p in out.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------
def test_one_command_turns_an_install_into_a_patch(install, tmp_path):
    """No --include, no per-format flags: extract and convert the lot."""
    root, listfile = install
    out = tmp_path / "patch"
    assert main(["build", "--casc", str(root), "-o", str(out),
                 "-l", str(listfile)]) == 0
    assert written(out) == [
        "creature/bear/bear.blp",
        "creature/bear/bear.m2",
        "creature/bear/bear00.skin",
        "sound/creature/bear/growl.ogg",
        "wotlkconv-report.json",
    ]


def test_the_whole_build_is_the_default_selection(install, tmp_path):
    """convert makes you ask for '**' on purpose; build is the asking."""
    root, listfile = install
    out = tmp_path / "patch"
    assert main(["build", "--casc", str(root), "-o", str(out),
                 "-l", str(listfile)]) == 0
    assert (out / "creature/bear/bear.m2").exists()


def test_a_narrower_selection_is_still_possible(install, tmp_path):
    root, listfile = install
    out = tmp_path / "patch"
    assert main(["build", "--casc", str(root), "-o", str(out),
                 "-l", str(listfile), "--include", "creature/**"]) == 0
    assert not (out / "sound").exists()
    assert (out / "creature/bear/bear.m2").exists()


def test_the_report_is_written_without_being_asked(install, tmp_path):
    """A patch build is big; the record of what happened is part of it."""
    root, listfile = install
    out = tmp_path / "patch"
    main(["build", "--casc", str(root), "-o", str(out), "-l", str(listfile)])
    report = json.loads((out / "wotlkconv-report.json").read_text())
    assert report["accounting"]["inputs"] == 4
    assert sum(report["accounting"][k] for k in
               ("written", "merged", "skipped", "failed")) == 4


def test_the_report_can_go_somewhere_else(install, tmp_path):
    root, listfile = install
    out = tmp_path / "patch"
    elsewhere = tmp_path / "run.json"
    main(["build", "--casc", str(root), "-o", str(out), "-l", str(listfile),
          "--report", str(elsewhere)])
    assert elsewhere.is_file()
    assert not (out / "wotlkconv-report.json").exists()


def test_building_from_a_folder_works_too(tmp_path):
    """The same command, for an extraction someone already has on disk."""
    src = tmp_path / "in"
    src.mkdir()
    (src / "bear.blp").write_bytes(F.build_blp(F.build_gradient_image(8, 8)))
    out = tmp_path / "patch"
    assert main(["build", str(src), "-o", str(out)]) == 0
    assert (out / "bear.blp").exists()


def test_a_missing_listfile_is_a_warning_not_a_failure(install, tmp_path,
                                                       monkeypatch, capsys):
    """Plenty converts without one; the references are what suffer."""
    from wotlkconv import listfile as lf

    root, _listfile = install
    monkeypatch.delenv(lf.ENV_VAR, raising=False)
    elsewhere = tmp_path / "elsewhere"      # nothing here for discovery to find
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert main(["build", "--casc", str(root), "-o", str(elsewhere / "p")]) == 0
    assert "--fetch to download it" in capsys.readouterr().err


def test_a_missing_definition_set_says_what_it_costs(install, tmp_path,
                                                     monkeypatch, capsys):
    from wotlkconv.db import dbd

    root, listfile = install
    monkeypatch.delenv(dbd.ENV_VAR, raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    main(["build", "--casc", str(root), "-o", str(elsewhere / "p"),
          "-l", str(listfile)])
    assert "every .db2 will be skipped" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Fetching the two references
# ---------------------------------------------------------------------------
def test_the_listfile_is_downloaded_and_then_cached(tmp_path):
    source = tmp_path / "remote.csv"
    source.write_text("1001;creature/bear/bear.m2\n")
    cache = tmp_path / "cache"

    got = fetch_listfile(cache, url=source.as_uri())
    assert got.read_text() == source.read_text()

    # Second time it must not need the remote at all.
    source.unlink()
    again = fetch_listfile(cache, url=source.as_uri())
    assert again == got and again.is_file()


def test_a_failed_download_leaves_no_half_written_cache(tmp_path):
    cache = tmp_path / "cache"
    missing = (tmp_path / "nope.csv").as_uri()
    with pytest.raises(ConverterError, match="could not download"):
        fetch_listfile(cache, url=missing)
    assert not list(cache.glob("*.part"))
    assert not (cache / "community-listfile.csv").exists()


def test_definitions_are_taken_out_of_the_source_archive(tmp_path):
    archive = tmp_path / "defs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("WoWDBDefs-master/definitions/CreatureModelData.dbd",
                    "COLUMNS\nint ID\n")
        zf.writestr("WoWDBDefs-master/definitions/ItemDisplayInfo.dbd",
                    "COLUMNS\nint ID\n")
        zf.writestr("WoWDBDefs-master/README.md", "not a definition")
        zf.writestr("WoWDBDefs-master/code/Program.cs", "also not")

    cache = tmp_path / "cache"
    got = fetch_definitions(cache, url=archive.as_uri())
    assert sorted(p.name for p in got.glob("*.dbd")) == [
        "CreatureModelData.dbd", "ItemDisplayInfo.dbd"]


def test_an_archive_with_no_definitions_says_so(tmp_path):
    archive = tmp_path / "defs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("WoWDBDefs-master/README.md", "nothing useful here")
    with pytest.raises(ConverterError, match="held no .dbd definitions"):
        fetch_definitions(tmp_path / "cache", url=archive.as_uri())


def test_cached_definitions_are_reused(tmp_path):
    archive = tmp_path / "defs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("WoWDBDefs-master/definitions/A.dbd", "COLUMNS\nint ID\n")
    cache = tmp_path / "cache"
    first = fetch_definitions(cache, url=archive.as_uri())
    archive.unlink()
    assert fetch_definitions(cache, url=archive.as_uri()) == first


def test_the_cache_lives_somewhere_predictable(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_cache() == tmp_path / "wotlkconv"


def test_fetching_is_never_automatic(install, tmp_path, monkeypatch):
    """Without --fetch the tool must not reach for the network."""
    import wotlkconv.fetch as fetch_mod

    def explode(*_a, **_k):
        raise AssertionError("build downloaded something without --fetch")

    monkeypatch.setattr(fetch_mod, "_download", explode)
    root, listfile = install
    assert main(["build", "--casc", str(root), "-o", str(tmp_path / "p"),
                 "-l", str(listfile)]) == 0
