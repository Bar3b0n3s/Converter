"""End-to-end pipeline and CLI behaviour."""

import json
from pathlib import Path

import pytest

import fixtures as F
from conftest import LISTFILE_ENTRIES
from wotlkconv import detect
from wotlkconv.blp.blp import PreferredFormat
from wotlkconv.chunks import ChunkReader
from wotlkconv.cli import main
from wotlkconv.listfile import Listfile
from wotlkconv.options import Options
from wotlkconv.pipeline import plan, run


@pytest.fixture
def extraction(tmp_path: Path) -> Path:
    """A synthetic extraction laid out the way wow.export dumps by FileDataID."""
    src = tmp_path / "in"
    src.mkdir()

    model = F.build_modern_model()
    (src / "123456.m2").write_bytes(
        F.serialise_modern_m2(model, skeleton_id=940000))
    for fid in (910000, 910001, 910002, 910003):
        (src / f"{fid}.skin").write_bytes(F.build_skin(legion=True))
    for fid in (920000, 920001):
        (src / f"{fid}.anim").write_bytes(F.build_anim())
    (src / "940000.skel").write_bytes(F.build_skel(bones=4, sequences=2))

    (src / "900000.blp").write_bytes(
        F.build_blp(F.build_gradient_image(32, 32), PreferredFormat.DXT5))
    (src / "900001.blp").write_bytes(F.build_bc5_blp(16, 16))

    wmo_dir = src / "world" / "wmo"
    wmo_dir.mkdir(parents=True)
    (wmo_dir / "House.wmo").write_bytes(F.build_modern_wmo_root())
    (src / "830001.wmo").write_bytes(F.build_modern_wmo_group())
    (src / "830002.wmo").write_bytes(F.build_modern_wmo_group())

    maps = src / "world" / "maps" / "azeroth"
    maps.mkdir(parents=True)
    root, tex, obj = F.build_split_adt(chunks=2)
    (maps / "Azeroth_32_48.adt").write_bytes(root)
    (maps / "Azeroth_32_48_tex0.adt").write_bytes(tex)
    (maps / "Azeroth_32_48_obj0.adt").write_bytes(obj)

    (tmp_path / "listfile.csv").write_text("\n".join(LISTFILE_ENTRIES) + "\n")
    return src


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def test_plan_classifies_every_input(extraction):
    jobs, skipped = plan([extraction])
    kinds = sorted(j.kind for j in jobs)
    assert kinds == [detect.ADT, detect.BLP, detect.BLP, detect.M2, detect.WMO_ROOT]
    assert [s.kind for s in skipped] == ["skel"]


def test_split_terrain_pieces_are_grouped(extraction):
    jobs, _ = plan([extraction])
    adt = next(j for j in jobs if j.kind == detect.ADT)
    assert sorted(adt.extra) == ["obj0", "tex0"]
    assert adt.source.name == "Azeroth_32_48.adt"


def test_companions_are_not_planned_as_separate_jobs(extraction):
    jobs, _ = plan([extraction])
    names = {j.source.name for j in jobs}
    assert not (names & {"910000.skin", "920000.anim", "830001.wmo"})


def test_companion_claiming_can_be_turned_off(extraction):
    jobs, _ = plan([extraction], claim_companions=False)
    names = {j.source.name for j in jobs}
    assert {"910000.skin", "920000.anim", "830001.wmo"} <= names


def test_split_pieces_without_a_root_are_skipped(tmp_path):
    d = tmp_path / "partial"
    d.mkdir()
    _root, tex, obj = F.build_split_adt(chunks=1)
    (d / "Foo_1_1_tex0.adt").write_bytes(tex)
    (d / "Foo_1_1_obj0.adt").write_bytes(obj)
    jobs, skipped = plan([d])
    assert jobs == []
    assert any(n.code == "adt.no_root" for s in skipped for n in s.notes)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
def convert_tree(extraction: Path, out: Path, **kw) -> tuple:
    listfile = Listfile.load(extraction.parent / "listfile.csv")
    jobs, skipped = plan([extraction])
    report = run(jobs, Options(**kw), listfile, out, roots=[str(extraction)],
                 listfile_path=str(extraction.parent / "listfile.csv"),
                 skipped=skipped)
    return report, sorted(p.relative_to(out).as_posix()
                          for p in out.rglob("*") if p.is_file())


def test_outputs_use_the_layout_the_client_globs_for(extraction, tmp_path):
    _report, files = convert_tree(extraction, tmp_path / "out")
    assert files == [
        "creature/testbeast/testbeast.m2",
        "creature/testbeast/testbeast00.skin",
        "creature/testbeast/testbeast0000-00.anim",
        "creature/testbeast/testbeast0001-00.anim",
        "creature/testbeast/testbeast01.skin",
        "creature/testbeast/testbeast02.skin",
        "creature/testbeast/testbeast03.skin",
        "creature/testbeast/testbeast_normal.blp",
        "creature/testbeast/testbeast_skin.blp",
        "world/maps/azeroth/Azeroth_32_48.adt",
        "world/wmo/House.wmo",
        "world/wmo/House_000.wmo",
        "world/wmo/House_001.wmo",
    ]


def test_every_output_is_loadable_by_the_wrath_parsers(extraction, tmp_path):
    out = tmp_path / "out"
    convert_tree(extraction, out)
    from wotlkconv.adt import inspect_adt
    from wotlkconv.blp import inspect_blp
    from wotlkconv.m2 import inspect_m2, inspect_skin
    from wotlkconv.wmo import inspect_wmo_root

    m2 = inspect_m2((out / "creature/testbeast/testbeast.m2").read_bytes(), "m")
    assert m2["version"] == 264 and not m2["chunked"]
    assert inspect_skin(
        (out / "creature/testbeast/testbeast00.skin").read_bytes(),
        "s")["header"] == "wotlk"
    assert inspect_blp(
        (out / "creature/testbeast/testbeast_normal.blp").read_bytes(),
        "b")["wotlk_compatible"]
    assert inspect_wmo_root(
        (out / "world/wmo/House.wmo").read_bytes(), "w")["wotlk_compatible"]
    assert inspect_adt(
        (out / "world/maps/azeroth/Azeroth_32_48.adt").read_bytes(),
        "a")["wotlk_compatible"]


def test_numeric_names_can_be_kept(extraction, tmp_path):
    _report, files = convert_tree(extraction, tmp_path / "out",
                                  name_from_listfile=False)
    assert "123456.m2" in files
    assert "123456" + "00.skin" in files


def test_flatten_writes_everything_to_the_root(extraction, tmp_path):
    _report, files = convert_tree(extraction, tmp_path / "out", flatten=True)
    assert all("/" not in f for f in files)


def test_dry_run_writes_nothing(extraction, tmp_path):
    report, files = convert_tree(extraction, tmp_path / "out", dry_run=True)
    assert files == []
    assert report.counts()


def test_existing_files_are_kept_unless_overwrite(extraction, tmp_path):
    out = tmp_path / "out"
    convert_tree(extraction, out)
    target = out / "creature/testbeast/testbeast.m2"
    target.write_bytes(b"SENTINEL")
    convert_tree(extraction, out)
    assert target.read_bytes() == b"SENTINEL"
    convert_tree(extraction, out, overwrite=True)
    assert target.read_bytes() != b"SENTINEL"


def test_parallel_and_serial_runs_agree(extraction, tmp_path):
    _r1, serial = convert_tree(extraction, tmp_path / "a")
    _r2, parallel = convert_tree(extraction, tmp_path / "b", jobs=3)
    assert serial == parallel
    for name in serial:
        assert (tmp_path / "a" / name).read_bytes() == \
            (tmp_path / "b" / name).read_bytes()


def test_a_corrupt_file_fails_alone(extraction, tmp_path):
    (extraction / "broken.m2").write_bytes(b"MD20" + b"\xff" * 600)
    report, files = convert_tree(extraction, tmp_path / "out")
    assert [f.source for f in report.failed] == ["broken.m2"]
    assert any(f.startswith("creature/") for f in files)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_convert(extraction, tmp_path, capsys):
    out = tmp_path / "cli-out"
    code = main(["convert", str(extraction), "-o", str(out),
                 "--listfile", str(extraction.parent / "listfile.csv"),
                 "--report", str(tmp_path / "r.json")])
    assert code == 0
    assert (out / "creature/testbeast/testbeast.m2").is_file()
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["target"]["build"] == 12340
    assert report["counts"]


def test_cli_convert_reports_failures_in_its_exit_code(tmp_path):
    src = tmp_path / "bad"
    src.mkdir()
    (src / "broken.m2").write_bytes(b"MD20" + b"\xff" * 600)
    assert main(["convert", str(src), "-o", str(tmp_path / "o")]) == 1


def test_cli_inspect_json(extraction, capsys):
    code = main(["inspect", str(extraction / "123456.m2"), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["version"] == 272
    assert payload[0]["wotlk_compatible"] is False


def test_cli_plan_lists_jobs(extraction, capsys):
    assert main(["plan", str(extraction)]) == 0
    out = capsys.readouterr().out
    assert "Azeroth_32_48.adt (+obj0, tex0)" in out
    assert "940000.skel" in out


def test_cli_listfile_summary(extraction, capsys):
    path = extraction.parent / "listfile.csv"
    assert main(["listfile", str(path), "--lookup", "123456",
                 "--lookup", "world/wmo/tex1.blp"]) == 0
    out = capsys.readouterr().out
    assert "creature/testbeast/testbeast.m2".replace("/", "\\") in out
    assert "800001" in out


def test_cli_rejects_an_empty_input_set(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["convert", str(empty), "-o", str(tmp_path / "o")]) == 1


# ---------------------------------------------------------------------------
# Convert / copy / skip classification
# ---------------------------------------------------------------------------
@pytest.fixture
def mixed_tree(tmp_path: Path) -> Path:
    src = tmp_path / "mixed"
    src.mkdir()
    (src / "art.blp").write_bytes(
        F.build_blp(F.build_gradient_image(16, 16), PreferredFormat.DXT1))
    (src / "sound.wav").write_bytes(b"RIFF\0\0\0\0WAVEfmt ")
    (src / "ui.lua").write_bytes(b"-- script\n")
    (src / "db.db2").write_bytes(b"WDC5" + b"\0" * 40)
    (src / "hd.tex").write_bytes(b"\0" * 40)
    (src / "rig.skel").write_bytes(F.build_skel(bones=1, sequences=1))
    return src


def test_formats_the_client_reads_unchanged_are_copied(mixed_tree, tmp_path):
    jobs, _skipped = plan([mixed_tree])
    actions = {j.source.name: j.action for j in jobs}
    assert actions == {"art.blp": detect.CONVERT,
                       "sound.wav": detect.COPY,
                       "ui.lua": detect.COPY}


def test_copied_files_arrive_byte_for_byte(mixed_tree, tmp_path):
    out = tmp_path / "out"
    jobs, skipped = plan([mixed_tree])
    run(jobs, Options(), Listfile(), out, skipped=skipped)
    assert (out / "sound.wav").read_bytes() == (mixed_tree / "sound.wav").read_bytes()
    assert (out / "ui.lua").read_bytes() == (mixed_tree / "ui.lua").read_bytes()


def test_unusable_formats_are_skipped_with_a_specific_reason(mixed_tree):
    _jobs, skipped = plan([mixed_tree])
    reasons = {Path(s.source).name: s.notes[0].message for s in skipped}
    assert "wotlkconv db convert" in reasons["db.db2"]
    assert "high-resolution" in reasons["hd.tex"]
    assert "merged into the model" in reasons["rig.skel"]


def test_databases_join_a_run_once_definitions_are_available(mixed_tree):
    jobs, skipped = plan([mixed_tree], convert_databases=True)
    assert detect.DB2 in {j.kind for j in jobs}
    assert "db.db2" not in {Path(s.source).name for s in skipped}


def test_copying_can_be_turned_off(mixed_tree):
    jobs, skipped = plan([mixed_tree], copy_unconverted=False)
    assert {j.source.name for j in jobs} == {"art.blp"}
    assert any("--no-copy-unconverted" in s.notes[0].message for s in skipped)


# ---------------------------------------------------------------------------
# Converting straight out of a CASC install
# ---------------------------------------------------------------------------
@pytest.fixture
def casc_install(tmp_path: Path):
    import casc_fixtures as CF
    root, tex, obj = F.build_split_adt(chunks=2)
    files = {
        123456: F.serialise_modern_m2(F.build_modern_model(), skeleton_id=940000),
        910000: F.build_skin(legion=True), 910001: F.build_skin(legion=True),
        910002: F.build_skin(legion=True), 910003: F.build_skin(legion=True),
        920000: F.build_anim(), 920001: F.build_anim(),
        940000: F.build_skel(bones=4, sequences=2),
        900000: F.build_blp(F.build_gradient_image(16, 16), PreferredFormat.DXT5),
        900001: F.build_bc5_blp(16, 16),
        830000: F.build_modern_wmo_root(),
        830001: F.build_modern_wmo_group(), 830002: F.build_modern_wmo_group(),
        700100: root, 700101: tex, 700102: obj,
        600001: b"RIFF\0\0\0\0WAVEfmt ",
        600003: b"WDC5" + b"\0" * 40,
    }
    CF.build_install(tmp_path / "wow", files)
    entries = [
        "123456;creature/testbeast/testbeast.m2",
        "910000;creature/testbeast/testbeast00.skin",
        "910001;creature/testbeast/testbeast01.skin",
        "910002;creature/testbeast/testbeast02.skin",
        "910003;creature/testbeast/testbeast03.skin",
        "920000;creature/testbeast/testbeast0000-00.anim",
        "920001;creature/testbeast/testbeast0001-00.anim",
        "940000;creature/testbeast/testbeast.skel",
        "900000;creature/testbeast/testbeast_skin.blp",
        "900001;creature/testbeast/testbeast_normal.blp",
        "830000;world/wmo/house/house.wmo",
        "830001;world/wmo/house/house_000.wmo",
        "830002;world/wmo/house/house_001.wmo",
        "700100;world/maps/azeroth/azeroth_32_48.adt",
        "700101;world/maps/azeroth/azeroth_32_48_tex0.adt",
        "700102;world/maps/azeroth/azeroth_32_48_obj0.adt",
        "600001;sound/creature/bear/attack.wav",
        "600003;dbfilesclient/creaturedisplayinfo.db2",
    ] + LISTFILE_ENTRIES
    (tmp_path / "listfile.csv").write_text("\n".join(entries) + "\n")
    return tmp_path / "wow", tmp_path / "listfile.csv"


def test_casc_selection_groups_and_claims_like_the_disk_planner(casc_install):
    from wotlkconv.casc import CascStorage
    from wotlkconv.pipeline import plan_casc
    install, listfile_path = casc_install
    listfile = Listfile.load(listfile_path)
    with CascStorage.open(install) as storage:
        jobs, skipped = plan_casc(storage, listfile, include=["**"])
        # Skins, anims and WMO groups are claimed by the model and root that
        # pull them in, so they are not planned separately; the .wav is
        # recognised as audio and copied.
        assert sorted(j.kind for j in jobs) == [
            detect.ADT, detect.BLP, detect.BLP, detect.M2,
            detect.WAV, detect.WMO_ROOT]
        assert [j.action for j in jobs if j.kind == detect.WAV] == [detect.COPY]
        # The tile's three pieces became one job.
        adt = next(j for j in jobs if j.kind == detect.ADT)
        assert sorted(adt.extra_ids) == ["obj0", "tex0"]
        assert any("db convert" in s.notes[0].message for s in skipped)


def test_casc_convert_produces_the_client_layout(casc_install, tmp_path):
    from wotlkconv.casc import CascStorage
    from wotlkconv.pipeline import plan_casc
    install, listfile_path = casc_install
    listfile = Listfile.load(listfile_path)
    out = tmp_path / "out"
    with CascStorage.open(install) as storage:
        jobs, skipped = plan_casc(storage, listfile, include=["**"])
        report = run(jobs, Options(), listfile, out, skipped=skipped,
                     storage=storage)
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*")
                   if p.is_file())
    assert files == [
        "creature/testbeast/testbeast.m2",
        "creature/testbeast/testbeast00.skin",
        "creature/testbeast/testbeast0000-00.anim",
        "creature/testbeast/testbeast0001-00.anim",
        "creature/testbeast/testbeast01.skin",
        "creature/testbeast/testbeast02.skin",
        "creature/testbeast/testbeast03.skin",
        "creature/testbeast/testbeast_normal.blp",
        "creature/testbeast/testbeast_skin.blp",
        "sound/creature/bear/attack.wav",
        "world/maps/azeroth/azeroth_32_48.adt",
        "world/wmo/house/house.wmo",
        "world/wmo/house/house_000.wmo",
        "world/wmo/house/house_001.wmo",
    ]
    assert not report.failed


def test_casc_convert_merges_the_split_tile(casc_install, tmp_path):
    from wotlkconv.adt import inspect_adt
    from wotlkconv.casc import CascStorage
    from wotlkconv.pipeline import plan_casc
    install, listfile_path = casc_install
    listfile = Listfile.load(listfile_path)
    out = tmp_path / "out"
    with CascStorage.open(install) as storage:
        jobs, skipped = plan_casc(storage, listfile, include=["world/maps/**"])
        run(jobs, Options(), listfile, out, skipped=skipped, storage=storage)
    info = inspect_adt(
        (out / "world/maps/azeroth/azeroth_32_48.adt").read_bytes(), "a")
    assert info["wotlk_compatible"] and info["map_chunks"] == 2


def test_casc_selection_by_file_id_needs_no_listfile(casc_install, tmp_path):
    from wotlkconv.casc import CascStorage
    from wotlkconv.pipeline import plan_casc
    install, _listfile_path = casc_install
    with CascStorage.open(install) as storage:
        jobs, _skipped = plan_casc(storage, Listfile(), file_ids=[900000])
    assert len(jobs) == 1
    assert jobs[0].file_id == 900000
    assert jobs[0].relpath.startswith("unknown/")


def test_cli_convert_from_casc(casc_install, tmp_path):
    install, listfile_path = casc_install
    out = tmp_path / "cli-out"
    code = main(["convert", "--casc", str(install), "-l", str(listfile_path),
                 "--include", "creature/**", "-o", str(out)])
    assert code == 0
    assert (out / "creature/testbeast/testbeast.m2").is_file()
    assert (out / "creature/testbeast/testbeast00.skin").is_file()


def test_cli_casc_selection_is_required(casc_install, tmp_path):
    install, listfile_path = casc_install
    assert main(["convert", "--casc", str(install), "-l", str(listfile_path),
                 "-o", str(tmp_path / "o")]) == 1


def test_cli_casc_info(casc_install, capsys):
    install, _ = casc_install
    assert main(["casc", "info", "--casc", str(install)]) == 0
    assert "product    wow" in capsys.readouterr().out


def test_cli_casc_list(casc_install, capsys):
    install, listfile_path = casc_install
    assert main(["casc", "list", "--casc", str(install),
                 "-l", str(listfile_path), "--include", "creature/**"]) == 0
    out = capsys.readouterr().out
    assert "creature\\testbeast\\testbeast.m2" in out
    assert "world\\wmo" not in out


def test_cli_casc_list_respects_the_limit(casc_install, capsys):
    install, listfile_path = casc_install
    assert main(["casc", "list", "--casc", str(install),
                 "-l", str(listfile_path), "--include", "**",
                 "--limit", "2"]) == 0
    assert "stopping at --limit 2" in capsys.readouterr().out


def test_cli_casc_list_needs_a_listfile(casc_install, tmp_path, monkeypatch):
    install, _listfile_path = casc_install
    monkeypatch.chdir(tmp_path / "empty" if (tmp_path / "empty").is_dir()
                      else tmp_path)
    (tmp_path / "listfile.csv").unlink(missing_ok=True)
    assert main(["casc", "list", "--casc", str(install), "--include", "**"]) == 1


def test_cli_casc_extract_is_raw(casc_install, tmp_path, capsys):
    install, listfile_path = casc_install
    out = tmp_path / "raw"
    assert main(["casc", "extract", "--casc", str(install),
                 "-l", str(listfile_path), "--include", "creature/**/*.m2",
                 "-o", str(out)]) == 0
    extracted = out / "creature/testbeast/testbeast.m2"
    assert extracted.is_file()
    # extract does not convert: the file is still the chunked original
    assert extracted.read_bytes()[:4] == b"MD21"


def test_a_wdl_is_recognised_and_converted(tmp_path):
    """It used to be listed as unsupported; now it goes through the pipeline."""
    src = tmp_path / "in"
    (src / "world/maps/azeroth").mkdir(parents=True)
    wdl = src / "world/maps/azeroth/azeroth.wdl"
    wdl.write_bytes(F.build_wdl())

    assert detect.detect(wdl.read_bytes(), str(wdl)) == detect.WDL
    assert detect.classify(detect.WDL, str(wdl))[0] == "convert"

    out = tmp_path / "out"
    jobs, skipped = plan([src])
    report = run(jobs, Options(), Listfile(), out, roots=[str(src)],
                 skipped=skipped)
    assert [r.status.value for r in report.files] == ["lossy"]
    written = out / "world/maps/azeroth/azeroth.wdl"
    assert written.exists()
    assert "MLHD" not in [c.name for c in ChunkReader(written.read_bytes(),
                                                      reverse=True)]
