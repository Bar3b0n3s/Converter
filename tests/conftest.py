"""Shared fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Allow ``pytest tests/`` from a checkout without installing the package.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from wotlkconv.listfile import Listfile  # noqa: E402
from wotlkconv.options import Options  # noqa: E402
from wotlkconv.report import FileResult  # noqa: E402
from wotlkconv.resolve import AssetSource  # noqa: E402

#: FileDataIDs used by the synthetic fixtures, and the paths they map to.
LISTFILE_ENTRIES = [
    "123456;creature/testbeast/testbeast.m2",
    "900000;creature/testbeast/testbeast_skin.blp",
    "900001;creature/testbeast/testbeast_normal.blp",
    "800001;world/wmo/tex1.blp",
    "800002;world/wmo/tex2.blp",
    "810001;world/doodads/tree.m2",
    "810002;world/doodads/rock.m2",
    "820001;environments/stars/sky.m2",
    "700001;tileset/generic/grass.blp",
    "700002;tileset/generic/rock.blp",
]


@pytest.fixture
def listfile() -> Listfile:
    lf = Listfile("<test>")
    lf.update(LISTFILE_ENTRIES)
    return lf


@pytest.fixture
def opts() -> Options:
    return Options()


@pytest.fixture
def result() -> FileResult:
    return FileResult(source="<test>")


@pytest.fixture
def asset_dir(tmp_path: Path) -> Path:
    d = tmp_path / "assets"
    d.mkdir()
    return d


@pytest.fixture
def source(listfile: Listfile, asset_dir: Path) -> AssetSource:
    return AssetSource(listfile, roots=[asset_dir])
