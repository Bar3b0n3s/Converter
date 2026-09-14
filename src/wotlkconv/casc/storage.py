"""Reading files straight out of a local CASC install.

Putting the pieces together, resolving one FileDataID means::

    root      FileDataID -> content key
    encoding  content key -> encoding key
    index     encoding key -> (archive number, offset, size)
    data.NNN  bytes at that offset, behind a 30-byte entry header
    BLTE      decode the stream

Only *local* storage is read.  Modern installs can be partial, with the rest
streamed from Blizzard's CDN on demand; files that are not on disk are reported
as missing rather than downloaded, because fetching them would mean pulling
content the user has not installed.
"""

from __future__ import annotations

import dataclasses
import struct
from pathlib import Path

from .. import log
from ..errors import MalformedFileError, MissingDependencyError
from . import blte
from .config import BuildInfo
from .encoding import EncodingTable
from .index import ARCHIVE_ENTRY_HEADER, LocalIndex
from .keys import KeyRing
from .root import LOCALE_NAMES, RootTable


class FileNotInstalledError(MissingDependencyError):
    """The file exists in the build but its data is not on this machine."""


@dataclasses.dataclass(slots=True)
class StorageStats:
    product: str
    version: str
    build: str
    locale: str
    files: int
    index_buckets: int
    encoding_entries: int
    keys: int


class CascStorage:
    """Read-only view of a local CASC install."""

    def __init__(self, install_dir: Path, build: BuildInfo, index: LocalIndex,
                 encoding: EncodingTable, root: RootTable, keys: KeyRing,
                 locale_name: str):
        self.install_dir = install_dir
        self.build = build
        self.index = index
        self.encoding = encoding
        self.root = root
        self.keys = keys
        self.locale_name = locale_name
        self._handles: dict[int, object] = {}
        self._data_dir = install_dir / "Data" / "data"

    # -- construction ---------------------------------------------------
    @classmethod
    def open(cls, install_dir: str | Path, *, product: str | None = None,
             locale: str = "enus", keys: KeyRing | None = None) -> "CascStorage":
        install_dir = Path(install_dir)
        keys = keys or KeyRing()
        build = BuildInfo.load(install_dir, product)
        log.info(f"opening CASC install: {build.describe()}")

        data_dir = install_dir / "Data" / "data"
        if not data_dir.is_dir():
            raise MissingDependencyError(
                f"{data_dir} does not exist; --casc wants the folder that "
                f"contains Data/, not Data/ itself")
        index = LocalIndex(data_dir)
        if not index.bucket_count:
            raise MissingDependencyError(
                f"no .idx index files in {data_dir}; the install may be "
                f"mid-update")

        storage = cls(install_dir, build, index, EncodingTable(), RootTable(),
                      keys, locale)

        raw = storage._read_by_ekey(bytes.fromhex(build.encoding_ekey), "encoding")
        storage.encoding = EncodingTable.parse(raw, "encoding")
        log.debug(f"encoding table: {len(storage.encoding)} content keys")

        locale_bits = LOCALE_NAMES.get(locale.lower().replace("_", ""))
        if locale_bits is None:
            raise MissingDependencyError(
                f"unknown locale {locale!r}; try one of "
                f"{', '.join(sorted(LOCALE_NAMES))}")
        root_raw = storage.read_by_ckey(bytes.fromhex(build.root_ckey), "root")
        storage.root = RootTable.parse(root_raw, locale_bits, "root")
        log.info(f"CASC ready: {len(storage.root)} files, locale {locale}")
        return storage

    # -- low level ------------------------------------------------------
    def _archive(self, number: int):
        handle = self._handles.get(number)
        if handle is None:
            path = self._data_dir / f"data.{number:03d}"
            if not path.is_file():
                raise FileNotInstalledError(
                    f"archive {path.name} is missing from the install")
            handle = path.open("rb")
            self._handles[number] = handle
        return handle

    def _read_by_ekey(self, ekey: bytes, what: str = "file") -> bytes:
        entry = self.index.find(ekey)
        if entry is None:
            raise FileNotInstalledError(
                f"{what} {ekey.hex()[:18]} is not in the local index; this "
                f"install streams it from the CDN rather than storing it "
                f"on disk")
        handle = self._archive(entry.archive)
        handle.seek(entry.offset)
        raw = handle.read(entry.size)
        if len(raw) < ARCHIVE_ENTRY_HEADER:
            raise MalformedFileError(
                f"{what}: archive entry is only {len(raw)} bytes")
        declared = struct.unpack_from("<I", raw, 16)[0]
        if declared and declared <= len(raw):
            raw = raw[:declared]
        return blte.decode(raw[ARCHIVE_ENTRY_HEADER:], self.keys)

    def read_by_ckey(self, ckey: bytes, what: str = "file") -> bytes:
        ekey = self.encoding.ekey_for(ckey)
        if ekey is None:
            raise FileNotInstalledError(
                f"{what} {ckey.hex()[:16]} has no entry in the encoding table")
        return self._read_by_ekey(ekey, what)

    # -- public ---------------------------------------------------------
    def __contains__(self, file_id: int) -> bool:
        return file_id in self.root

    def file_ids(self):
        return self.root.file_ids()

    def read_file_id(self, file_id: int) -> bytes:
        """Read one file. Raises on missing, not-installed or encrypted."""
        ckey = self.root.ckey_for(file_id)
        if ckey is None:
            raise MissingDependencyError(
                f"FileDataID {file_id} is not in this build's root table")
        return self.read_by_ckey(ckey, f"FileDataID {file_id}")

    def try_read_file_id(self, file_id: int) -> tuple[bytes | None, str]:
        """Read a file, returning ``(data, "")`` or ``(None, reason)``."""
        try:
            return self.read_file_id(file_id), ""
        except blte.EncryptedChunkError as exc:
            return None, str(exc)
        except (MissingDependencyError, MalformedFileError) as exc:
            return None, str(exc)

    def stats(self) -> StorageStats:
        return StorageStats(
            product=self.build.product or "wow",
            version=self.build.version,
            build=self.build.build_name or self.build.build_key[:8],
            locale=self.locale_name,
            files=len(self.root),
            index_buckets=self.index.bucket_count,
            encoding_entries=len(self.encoding),
            keys=len(self.keys),
        )

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()  # type: ignore[attr-defined]
        self._handles.clear()

    def __enter__(self) -> "CascStorage":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
