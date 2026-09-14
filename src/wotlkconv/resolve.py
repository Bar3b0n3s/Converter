"""Finding the sibling files a model needs.

Converting one M2 pulls in its skins, its skeleton, its external animations and
its textures.  Where those live on disk depends entirely on how the user
extracted them, and the three layouts in common use are:

* **by FileDataID** -- ``1234567.skel`` next to ``1234568.m2`` (CASCExplorer's
  "no listfile" dump, and what ``wow.export`` writes in ID mode)
* **by in-game path** -- ``character/human/male/humanmale.m2`` mirroring the
  archive tree
* **flat** -- everything in one directory, basenames only

:class:`AssetSource` tries all three so the caller never has to care, and can
fall back to an open CASC install when the companion was never extracted at
all -- which is the normal case when converting straight out of a game
install.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from . import log
from .listfile import Listfile, to_posix


class AssetSource:
    """Locates companion assets for the file currently being converted."""

    def __init__(self, listfile: Listfile | None = None,
                 roots: Sequence[str | os.PathLike[str]] = (),
                 extensions: Sequence[str] = (), casc=None):
        self.listfile = listfile or Listfile()
        self.roots: list[Path] = [Path(r) for r in roots]
        self.extensions = list(extensions) or [".m2", ".skel", ".skin", ".anim",
                                               ".blp", ".bone", ".wmo"]
        self.casc = casc
        self._cache: dict[str, bytes | None] = {}
        self._index: dict[str, Path] | None = None

    # -- roots ----------------------------------------------------------
    def add_root(self, root: str | os.PathLike[str]) -> None:
        p = Path(root)
        if p.is_file():
            p = p.parent
        if p not in self.roots:
            self.roots.append(p)
            self._index = None

    def _basename_index(self) -> dict[str, Path]:
        """Lazily index every candidate file under the roots by lowercase name."""
        if self._index is not None:
            return self._index
        index: dict[str, Path] = {}
        exts = set(self.extensions)
        for root in self.roots:
            if not root.is_dir():
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() in exts:
                        index.setdefault(fn.lower(), Path(dirpath) / fn)
        self._index = index
        log.debug(f"indexed {len(index)} companion files under "
                  f"{', '.join(str(r) for r in self.roots) or '(no roots)'}")
        return index

    # -- lookup ---------------------------------------------------------
    def _read(self, path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except OSError:
            return None

    def by_path(self, game_path: str) -> bytes | None:
        """Find a file given its in-game path (``\\`` or ``/`` separated)."""
        key = f"p:{game_path.lower()}"
        if key in self._cache:
            return self._cache[key]
        rel = to_posix(game_path)
        result: bytes | None = None
        for root in self.roots:
            candidate = root / rel
            if candidate.is_file():
                result = self._read(candidate)
                break
        if result is None:
            hit = self._basename_index().get(os.path.basename(rel).lower())
            if hit is not None:
                result = self._read(hit)
        self._cache[key] = result
        return result

    def by_file_id(self, file_id: int, extension: str = "") -> bytes | None:
        """Find a file given its FileDataID, trying ID-named files first."""
        if not file_id:
            return None
        key = f"i:{file_id}:{extension}"
        if key in self._cache:
            return self._cache[key]

        result: bytes | None = None
        exts = [extension] if extension else self.extensions
        for root in self.roots:
            for ext in exts:
                candidate = root / f"{file_id}{ext}"
                if candidate.is_file():
                    result = self._read(candidate)
                    break
            if result is not None:
                break

        if result is None:
            path = self.listfile.path_for(file_id)
            if path:
                result = self.by_path(path)

        if result is None and self.casc is not None:
            # Files on disk win, so a user's edited copy overrides the install.
            data, why = self.casc.try_read_file_id(file_id)
            if data is None:
                log.debug(f"CASC lookup for {file_id} failed: {why}")
            result = data

        self._cache[key] = result
        return result

    def loader_for(self, extension: str):
        """Return a ``(file_id) -> bytes | None`` closure, for chain walkers."""
        return lambda fid: self.by_file_id(fid, extension)


