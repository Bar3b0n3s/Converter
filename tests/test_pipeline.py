"""End-to-end pipeline and CLI behaviour."""

import json
from pathlib import Path

import pytest

import fixtures as F
from conftest import LISTFILE_ENTRIES
from wotlkconv import detect
from wotlkconv.blp.blp import PreferredFormat
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
