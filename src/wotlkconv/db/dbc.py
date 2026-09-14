"""3.3.5a client databases (``.dbc``).

The format is as simple as the modern one is not::

    char   magic[4] = 'WDBC'
    uint32 record_count
    uint32 field_count
    uint32 record_size      == field_count * 4
    uint32 string_block_size
    uint8  records[record_count][record_size]
    uint8  strings[string_block_size]

Every field is four bytes. Nothing in the file says whether a field is an int,
a float or an offset into the string block -- the client knows, and any tool
has to be told.

That matters for merging. When new rows are appended to a table the user
already has, this module **keeps the original records and string block byte for
byte and appends to them**. Existing string offsets stay valid, so the merge
needs to know the types only of the fields it actually writes, never of the
ones it copies through. Getting a field type wrong elsewhere in the row cannot
corrupt anything.
"""

from __future__ import annotations

import dataclasses
import struct
from typing import Any

from ..errors import MalformedFileError, UnsupportedFormatError

MAGIC = b"WDBC"
HEADER_SIZE = 20
FIELD_SIZE = 4

#: Field types a mapping can declare for a column it writes.
TYPES = ("int", "uint", "float", "string")


@dataclasses.dataclass
class DbcTable:
    """A parsed ``.dbc``, kept as raw words plus the untouched string block."""

    field_count: int = 0
    record_size: int = 0
    records: list[bytes] = dataclasses.field(default_factory=list)
    strings: bytes = b"\0"

    def __len__(self) -> int:
        return len(self.records)

    @classmethod
    def parse(cls, data: bytes, name: str = "<dbc>") -> "DbcTable":
        if len(data) < HEADER_SIZE:
            raise MalformedFileError(f"{name}: file is only {len(data)} bytes")
        if data[:4] != MAGIC:
            raise UnsupportedFormatError(
                f"{name}: not a 3.3.5a .dbc (magic {data[:4]!r})")
        record_count, field_count, record_size, string_size = struct.unpack_from(
            "<4I", data, 4)
        if field_count and record_size != field_count * FIELD_SIZE:
            raise MalformedFileError(
                f"{name}: record size {record_size} does not match "
                f"{field_count} four-byte fields")
        start = HEADER_SIZE
        end = start + record_count * record_size
        if end > len(data):
            raise MalformedFileError(
                f"{name}: {record_count} records of {record_size} bytes run "
                f"past the end of the file")
        table = cls(field_count=field_count, record_size=record_size)
        table.records = [data[start + i * record_size:
                              start + (i + 1) * record_size]
                         for i in range(record_count)]
        table.strings = data[end : end + string_size] or b"\0"
        return table

    # -- typed access ---------------------------------------------------
    def word(self, row: int, index: int) -> int:
        return struct.unpack_from("<I", self.records[row], index * FIELD_SIZE)[0]

    def value(self, row: int, index: int, kind: str) -> Any:
        raw = self.records[row][index * FIELD_SIZE:(index + 1) * FIELD_SIZE]
        if kind == "float":
            return struct.unpack("<f", raw)[0]
        if kind == "int":
            return struct.unpack("<i", raw)[0]
        if kind == "string":
            return self.string_at(struct.unpack("<I", raw)[0])
        return struct.unpack("<I", raw)[0]

    def string_at(self, offset: int) -> str:
        if offset <= 0 or offset >= len(self.strings):
            return ""
        end = self.strings.find(b"\0", offset)
        if end < 0:
            end = len(self.strings)
        return self.strings[offset:end].decode("latin-1")

    def ids(self, index: int = 0) -> list[int]:
        return [self.word(r, index) for r in range(len(self.records))]

    def serialize(self) -> bytes:
        out = bytearray(MAGIC)
        out += struct.pack("<4I", len(self.records), self.field_count,
                           self.record_size, len(self.strings))
        for record in self.records:
            out += record.ljust(self.record_size, b"\0")[:self.record_size]
        out += self.strings
        return bytes(out)


class DbcBuilder:
    """Builds a ``.dbc``, optionally on top of an existing one.

    Starting from a template keeps that table's records and string block
    untouched; new strings are appended so every offset already in the file
    stays correct.
    """

    def __init__(self, field_count: int, template: DbcTable | None = None):
        if field_count <= 0:
            raise ValueError("a .dbc needs at least one field")
        self.field_count = field_count
        self.record_size = field_count * FIELD_SIZE
        self.template = template
        if template is not None:
            if template.field_count != field_count:
                raise MalformedFileError(
                    f"template has {template.field_count} fields but the "
                    f"mapping describes {field_count}; one of them is wrong "
                    f"for this client build")
            self.records: list[bytearray] = [bytearray(r) for r in template.records]
            self._strings = bytearray(template.strings or b"\0")
        else:
            self.records = []
            self._strings = bytearray(b"\0")
        self._string_offsets: dict[str, int] = {}
        self._row_index: dict[int, int] = {}

    # -- strings --------------------------------------------------------
    def intern(self, text: str) -> int:
        """Append a string and return its offset; the empty string is 0."""
        if not text:
            return 0
        hit = self._string_offsets.get(text)
        if hit is not None:
            return hit
        offset = len(self._strings)
        self._strings += text.encode("latin-1", errors="replace") + b"\0"
        self._string_offsets[text] = offset
        return offset

    # -- rows -----------------------------------------------------------
    def index_existing(self, id_index: int = 0) -> None:
        """Note where each existing row's id lives, so rows can be replaced."""
        for position, record in enumerate(self.records):
            if len(record) >= (id_index + 1) * FIELD_SIZE:
                row_id = struct.unpack_from("<I", record, id_index * FIELD_SIZE)[0]
                self._row_index[row_id] = position

    def encode(self, values: dict[int, tuple[str, Any]]) -> bytearray:
        """Pack ``{field index: (type, value)}`` into one record."""
        record = bytearray(self.record_size)
        for index, (kind, value) in values.items():
            if index < 0 or index >= self.field_count:
                raise MalformedFileError(
                    f"field index {index} is outside the table's "
                    f"{self.field_count} fields")
            at = index * FIELD_SIZE
            if kind == "float":
                struct.pack_into("<f", record, at, float(value))
            elif kind == "string":
                struct.pack_into("<I", record, at, self.intern(str(value)))
            elif kind == "int":
                struct.pack_into("<i", record, at, int(value))
            else:
                struct.pack_into("<I", record, at, int(value) & 0xFFFFFFFF)
        return record

    def add(self, values: dict[int, tuple[str, Any]], row_id: int | None = None,
            id_index: int = 0, replace: bool = True) -> bool:
        """Append a row, or replace one with the same id. Returns True if new."""
        record = self.encode(values)
        if row_id is None:
            row_id = struct.unpack_from("<I", record, id_index * FIELD_SIZE)[0]
        existing = self._row_index.get(row_id)
        if existing is not None and replace:
            self.records[existing] = record
            return False
        self._row_index[row_id] = len(self.records)
        self.records.append(record)
        return True

    def has(self, row_id: int) -> bool:
        return row_id in self._row_index

    def build(self) -> DbcTable:
        table = DbcTable(field_count=self.field_count,
                         record_size=self.record_size)
        table.records = [bytes(r) for r in self.records]
        table.strings = bytes(self._strings)
        return table

    def serialize(self) -> bytes:
        return self.build().serialize()


def inspect_dbc(data: bytes, source_name: str) -> dict:
    table = DbcTable.parse(data, source_name)
    return {
        "kind": "dbc",
        "records": len(table.records),
        "fields": table.field_count,
        "record_size": table.record_size,
        "string_block": len(table.strings),
        "wotlk_compatible": True,
    }


