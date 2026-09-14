import pytest

from wotlkconv.errors import MissingDependencyError
from wotlkconv.listfile import ENV_VAR, Listfile, normalise, placeholder_path, to_posix


def test_normalise_matches_the_client_convention():
    assert normalise("World/Foo/Bar.BLP") == "world\\foo\\bar.blp"
    assert to_posix("world\\foo\\bar.blp") == "world/foo/bar.blp"


@pytest.mark.parametrize("line", [
    "123;world/foo.blp",
    "123,world/foo.blp",
    "123 world/foo.blp",
    "123\tworld/foo.blp",
])
def test_every_separator_the_community_files_use_is_accepted(line):
    lf = Listfile()
    lf.update([line])
    assert lf.path_for(123) == "world\\foo.blp"


def test_non_numeric_lines_are_ignored():
    lf = Listfile()
    lf.update(["fdid;filename", "", "# comment", "42;a.blp"])
    assert len(lf) == 1 and lf.path_for(42) == "a.blp"


def test_reverse_lookup_keeps_the_oldest_path():
    lf = Listfile()
    lf.update(["10;shared.blp", "20;shared.blp"])
    assert lf.id_for("SHARED.BLP") == 10


def test_missing_entries_return_none():
    lf = Listfile()
    assert lf.path_for(999) is None
    assert lf.id_for("nope.blp") is None
    assert 999 not in lf


def test_require_path_explains_how_to_fix_it():
    with pytest.raises(MissingDependencyError, match="--listfile"):
        Listfile().require_path(7, "texture")


def test_loading_a_missing_file_is_an_error(tmp_path):
    with pytest.raises(MissingDependencyError):
        Listfile.load(tmp_path / "nope.csv")


def test_discover_prefers_the_explicit_path(tmp_path):
    p = tmp_path / "listfile.csv"
    p.write_text("1;a.blp\n")
    assert Listfile.discover(p).path_for(1) == "a.blp"


def test_discover_falls_back_to_the_environment(tmp_path, monkeypatch):
    p = tmp_path / "env.csv"
    p.write_text("2;b.blp\n")
    monkeypatch.setenv(ENV_VAR, str(p))
    assert Listfile.discover(None).path_for(2) == "b.blp"


def test_discover_probes_the_search_directories(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    (tmp_path / "listfile.csv").write_text("3;c.blp\n")
    assert Listfile.discover(None, [tmp_path]).path_for(3) == "c.blp"


def test_discover_returns_an_empty_listfile_rather_than_raising(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    assert not Listfile.discover(None)


def test_placeholder_paths_are_deterministic():
    assert placeholder_path(12345, "blp") == "unresolved\\blp\\12345.blp"
