import json

from wotlkconv.report import FileResult, Report, Status


def test_status_escalates_but_never_downgrades():
    r = FileResult(source="a.m2")
    assert r.status is Status.OK
    r.info("x.info", "fine")
    assert r.status is Status.OK
    r.lossy("x.lossy", "lost something")
    assert r.status is Status.LOSSY
    r.info("x.info2", "still fine")
    assert r.status is Status.LOSSY
    r.fail("x.fail", "broke")
    assert r.status is Status.FAILED
    r.lossy("x.lossy2", "more loss")
    assert r.status is Status.FAILED
    assert not r.ok


def test_notes_carry_machine_readable_detail():
    r = FileResult(source="a.m2")
    r.lossy("m2.particle.multitexture", "dropped textures", emitters=3)
    note = r.as_dict()["notes"][0]
    assert note["code"] == "m2.particle.multitexture"
    assert note["detail"] == {"emitters": 3}


def test_report_counts_and_json(tmp_path):
    report = Report()
    ok = FileResult(source="a.blp", kind="blp")
    lossy = FileResult(source="b.m2", kind="m2")
    lossy.lossy("m2.x", "lost")
    failed = FileResult(source="c.m2", kind="m2")
    failed.fail("m2.y", "broke")
    report.extend([ok, lossy, failed])
    report.close()

    assert report.counts() == {"ok": 1, "lossy": 1, "failed": 1}
    assert [f.source for f in report.failed] == ["c.m2"]
    assert [f.source for f in report.lossy] == ["b.m2"]

    path = tmp_path / "report.json"
    report.write_json(path)
    data = json.loads(path.read_text())
    assert data["target"] == {"patch": "3.3.5a", "build": 12340}
    assert len(data["files"]) == 3


def test_summary_lists_failures_without_verbose():
    report = Report()
    failed = FileResult(source="c.m2")
    failed.fail("m2.y", "it broke")
    report.add(failed)
    lines = report.summary_lines()
    assert any("FAILED c.m2: it broke" in line for line in lines)
