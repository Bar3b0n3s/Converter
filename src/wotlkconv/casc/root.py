"""The root table: FileDataID -> content key.

This is the file that makes FileDataIDs mean anything.  Two shapes exist:

*Pre-8.2* -- a bare sequence of blocks, each ``numRecords, contentFlags,
localeFlags`` followed by ID deltas, content keys and name hashes.

*8.2 and later* -- the same blocks behind a ``TSFM`` header, with name hashes
omitted when the block's content flags say so.  Dragonflight added an explicit
header-size/version prefix, which is detected rather than assumed.

FileDataIDs are stored as deltas: ``id = previous + delta + 1``.
"""

from __future__ import annotations

import struct

from .. import log
from ..errors import MalformedFileError

MAGIC = b"TSFM"

#: Locale bits in a root block's localeFlags.
LOCALE_ALL = 0xFFFFFFFF
LOCALE_EN_US = 0x2
LOCALE_EN_GB = 0x4
LOCALE_NAMES = {
    "enus": 0x2, "engb": 0x4, "krkr": 0x20, "frfr": 0x10, "dede": 0x8,
    "zhcn": 0x40, "eses": 0x80, "zhtw": 0x100, "esmx": 0x200, "ruru": 0x400,
    "ptbr": 0x800, "itit": 0x1000, "ptpt": 0x2000,
}

#: Content flag meaning "this block stores no name hashes".
CONTENT_NO_NAME_HASH = 0x10000000


class RootTable:
    """FileDataID -> content key, resolved for a preferred locale."""

    __slots__ = ("_by_id", "_fallback", "blocks", "truncated")

    def __init__(self) -> None:
        self._by_id: dict[int, bytes] = {}
        self._fallback: dict[int, bytes] = {}
        self.blocks = 0
        self.truncated = False

    def __len__(self) -> int:
        return len(self._fallback)

    def __contains__(self, file_id: int) -> bool:
        return file_id in self._fallback

    def ckey_for(self, file_id: int) -> bytes | None:
        return self._by_id.get(file_id) or self._fallback.get(file_id)

    def file_ids(self):
        return self._fallback.keys()

    @classmethod
    def parse(cls, data: bytes, locale: int = LOCALE_EN_US,
              name: str = "<root>") -> "RootTable":
        table = cls()
        pos = 0
        total_files = named_files = None

        if len(data) >= 4 and data[:4] == MAGIC:
            pos = 4
            first, second = struct.unpack_from("<II", data, pos)
            if first == 0x18:
                # Dragonflight: headerSize, version, totalFiles, namedFiles, pad
                _header_size, _version, total_files, named_files = \
                    struct.unpack_from("<IIII", data, pos)
                pos = 0x18
            else:
                total_files, named_files = first, second
                pos += 8
            log.debug(f"{name}: MFST root, {total_files} files, "
                      f"{named_files} named")

        while pos + 12 <= len(data):
            # All three are unsigned: localeFlags is 0xFFFFFFFF for "every
            # locale", which read as -1 if these were signed.
            num_records, content_flags, locale_flags = struct.unpack_from(
                "<III", data, pos)
            pos += 12
            if num_records == 0:
                continue
            deltas_end = pos + num_records * 4
            keys_end = deltas_end + num_records * 16
            if keys_end > len(data):
                table.truncated = True
                log.warn(f"{name}: root block claims {num_records} records but "
                         f"the file ends first; stopping after "
                         f"{len(table)} entries")
                break

            deltas = struct.unpack_from("<" + "i" * num_records, data, pos)
            pos = deltas_end

            wanted = (locale_flags == LOCALE_ALL
                      or bool(locale_flags & locale)
                      or locale_flags == 0)
            file_id = -1
            for i, delta in enumerate(deltas):
                file_id += delta + 1
                ckey = data[pos + i * 16 : pos + i * 16 + 16]
                table._fallback.setdefault(file_id, ckey)
                if wanted:
                    table._by_id.setdefault(file_id, ckey)
            pos = keys_end

            if not (content_flags & CONTENT_NO_NAME_HASH):
                # Name hashes are Jenkins hashes of the in-game path; this tool
                # resolves names through the listfile instead, so skip them.
                pos += num_records * 8
            table.blocks += 1

        if not table._fallback:
            raise MalformedFileError(f"{name}: root table contains no files")
        return table
