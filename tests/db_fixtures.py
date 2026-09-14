"""Builds synthetic DB2/DBD/DBC files for the database tests.

Written from the format description rather than by round-tripping the reader,
so a mistake in either direction shows up rather than cancelling out. Covers
each field storage type the WDC family uses, plus id lists, copy tables and
sparse offset maps.
"""

from __future__ import annotations

import dataclasses
import struct
from typing import Any, Sequence

WDC3_HEADER_SIZE = 72
SECTION_HEADER_WDC3 = 40
SECTION_HEADER_WDC2 = 36
STORAGE_INFO_SIZE = 24

STORAGE_NONE = 0
STORAGE_BITPACKED = 1
STORAGE_COMMON_DATA = 2
STORAGE_BITPACKED_INDEXED = 3
STORAGE_BITPACKED_INDEXED_ARRAY = 4
STORAGE_BITPACKED_SIGNED = 5


@dataclasses.dataclass
class Col:
    """One column of a synthetic table."""

    name: str
    type: str = "int"           # int | float | string
    bits: int = 32
    array: int = 1
    storage: int = STORAGE_NONE
    signed: bool = True
    #: For STORAGE_COMMON_DATA: {row id: value}, plus a default.
    common: dict[int, int] = dataclasses.field(default_factory=dict)
    common_default: int = 0
    #: For the palletised types: the palette this column indexes into.
    pallet: list[Any] = dataclasses.field(default_factory=list)
    #: Excluded from the record; the value comes from the section's id list.
    non_inline: bool = False
    is_id: bool = False


def _as_words(value: Any, kind: str) -> int:
    if kind == "float":
        return struct.unpack("<I", struct.pack("<f", float(value)))[0]
    return int(value) & 0xFFFFFFFF


def _layout_block(columns: Sequence[Col], layout_hash: str, build: str) -> list[str]:
    lines = [f"LAYOUT {layout_hash}", f"BUILD {build}"]
    for col in columns:
        if col.non_inline:
            prefix = "$noninline,id$" if col.is_id else "$noninline$"
        elif col.is_id:
            prefix = "$id$"
        else:
            prefix = ""
        width = ""
        if col.type == "int":
            width = f"<{'' if col.signed else 'u'}{col.bits}>"
        array = f"[{col.array}]" if col.array > 1 else ""
        lines.append(f"{prefix}{col.name}{width}{array}")
    return lines


def build_dbd(table: str, columns: Sequence[Col], layout_hash: str,
              build: str = "11.0.5.57212",
              wotlk_columns: Sequence[Col] | None = None,
              wotlk_build: str = "3.3.5.12340",
              wotlk_hash: str = "0BADF00D") -> str:
    """A ``.dbd`` definition matching ``columns``.

    ``wotlk_columns`` adds a second layout for the 3.3.5a build, which is what
    the converter reads to learn the shape of the table it is writing into.
    """
    types = {"int": "int", "float": "float", "string": "string"}
    named = list(columns) + [c for c in (wotlk_columns or [])
                             if c.name not in {x.name for x in columns}]
    lines = ["COLUMNS"]
    for col in named:
        lines.append(f"{types[col.type]} {col.name}")
    lines.append("")
    lines += _layout_block(columns, layout_hash, build)
    if wotlk_columns is not None:
        lines.append("")
        lines += _layout_block(wotlk_columns, wotlk_hash, wotlk_build)
    return "\n".join(lines) + "\n"


def build_wdc3(columns: Sequence[Col], rows: Sequence[dict[str, Any]], *,
               magic: str = "WDC3", layout_hash: int = 0x1FE1BDA4,
               table_hash: int = 0xDEADBEEF, use_id_list: bool = False,
               copies: Sequence[tuple[int, int]] = (),
               id_column: str | None = None,
               relationship: dict[int, int] | None = None,
               encrypted: bool = False) -> bytes:
    """Assemble a WDC2/3/4/5 table from column specs and Python rows."""
    inline = [c for c in columns if not c.non_inline]
    id_name = id_column or next((c.name for c in columns if c.is_id),
                                columns[0].name)

    # -- lay out the record ------------------------------------------------
    bit_offset = 0
    storages: list[tuple[Col, int, int]] = []   # column, offset bits, size bits
    for col in inline:
        if col.storage in (STORAGE_BITPACKED_INDEXED,
                           STORAGE_BITPACKED_INDEXED_ARRAY):
            size = col.bits
            total = size
        elif col.storage in (STORAGE_BITPACKED, STORAGE_BITPACKED_SIGNED):
            size = col.bits
            total = size
        elif col.storage == STORAGE_COMMON_DATA:
            size = 0
            total = 0
        else:
            size = col.bits
            total = size * col.array
        storages.append((col, bit_offset, size))
        bit_offset += total
    record_size = max(1, (bit_offset + 7) // 8)
    # Records are padded so the next one starts on a byte boundary.
    record_size = (record_size + 3) & ~3

    # -- string table ------------------------------------------------------
    string_block = bytearray(b"\0")
    string_offsets: dict[str, int] = {"": 0}

    def intern(text: str) -> int:
        if text in string_offsets:
            return string_offsets[text]
        offset = len(string_block)
        string_block.extend(text.encode("latin-1") + b"\0")
        string_offsets[text] = offset
        return offset

    for row in rows:
        for col in inline:
            if col.type == "string":
                value = row.get(col.name, "")
                for item in (value if isinstance(value, list) else [value]):
                    intern(str(item))

    record_data = bytearray(record_size * len(rows))
    pending_strings: list[tuple[int, int, str]] = []

    def put_bits(buf: bytearray, base_bit: int, value: int, width: int) -> None:
        for i in range(width):
            if (value >> i) & 1:
                bit = base_bit + i
                buf[bit >> 3] |= 1 << (bit & 7)

    for index, row in enumerate(rows):
        record_base = index * record_size * 8
        for col, offset_bits, size_bits in storages:
            if col.storage == STORAGE_COMMON_DATA:
                continue
            value = row.get(col.name)
            if col.storage in (STORAGE_BITPACKED_INDEXED,
                               STORAGE_BITPACKED_INDEXED_ARRAY):
                items = value if isinstance(value, list) else [value]
                words = tuple(_as_words(v, col.type) for v in items)
                if words not in getattr(col, "_slots", {}):
                    slots = getattr(col, "_slots", None)
                    if slots is None:
                        slots = {}
                        col._slots = slots            # noqa: SLF001 - fixture
                    slots[words] = len(col.pallet) // max(1, col.array)
                    col.pallet.extend(words)
                slot = col._slots[words]              # noqa: SLF001 - fixture
                put_bits(record_data, record_base + offset_bits, slot, size_bits)
                continue

            items = value if isinstance(value, list) else [value]
            for slot_index in range(col.array):
                item = items[slot_index] if slot_index < len(items) else 0
                bit_at = record_base + offset_bits + slot_index * size_bits
                if col.type == "string":
                    field_byte = (offset_bits + slot_index * size_bits) // 8
                    pending_strings.append(
                        (bit_at, index * record_size + field_byte, str(item)))
                    continue
                put_bits(record_data, bit_at, _as_words(item, col.type), size_bits)

    # String columns store an offset measured from the field's own position.
    for bit_at, field_absolute, text in pending_strings:
        raw = len(record_data) + string_offsets[text] - field_absolute
        put_bits(record_data, bit_at, raw & 0xFFFFFFFF, 32)

    # -- pallet and common blocks -----------------------------------------
    pallet_data = bytearray()
    common_data = bytearray()
    storage_infos = bytearray()
    for col, offset_bits, size_bits in storages:
        extra = 0
        compression = [0, 0, 0]
        if col.storage in (STORAGE_BITPACKED_INDEXED,
                           STORAGE_BITPACKED_INDEXED_ARRAY):
            extra = len(col.pallet) * 4
            pallet_data.extend(struct.pack("<" + "I" * len(col.pallet),
                                           *col.pallet))
            compression = [offset_bits, size_bits,
                           col.array if col.storage ==
                           STORAGE_BITPACKED_INDEXED_ARRAY else 0]
        elif col.storage == STORAGE_COMMON_DATA:
            entries = sorted(col.common.items())
            extra = len(entries) * 8
            for key, value in entries:
                common_data.extend(struct.pack("<II", key,
                                               _as_words(value, col.type)))
            compression = [_as_words(col.common_default, col.type), 0, 0]
        elif col.storage in (STORAGE_BITPACKED, STORAGE_BITPACKED_SIGNED):
            compression = [offset_bits, size_bits, 0]
        storage_infos.extend(struct.pack("<HHII3I", offset_bits, size_bits,
                                         extra, col.storage, *compression))

    # -- section payload ---------------------------------------------------
    section = bytearray()
    section.extend(record_data)
    section.extend(string_block)
    id_list = b""
    if use_id_list:
        ids = [int(row[id_name]) for row in rows]
        id_list = struct.pack("<" + "I" * len(ids), *ids)
        section.extend(id_list)
    copy_data = b""
    if copies:
        copy_data = b"".join(struct.pack("<II", new, old) for new, old in copies)
        section.extend(copy_data)
    relationship_data = b""
    if relationship:
        entries = sorted(relationship.items())
        relationship_data = struct.pack("<III", len(entries), 0, len(rows) - 1)
        relationship_data += b"".join(struct.pack("<II", foreign, index)
                                      for index, foreign in entries)
        section.extend(relationship_data)

    # -- header ------------------------------------------------------------
    if magic == "WDC1":
        section_header_size = 0
    elif magic in ("WDC2", "1SLC"):
        section_header_size = SECTION_HEADER_WDC2
    else:
        section_header_size = SECTION_HEADER_WDC3
    prefix = 8 + 128 if magic == "WDC5" else 4
    # WDC1 keeps copy_table_size in the header in place of a section table,
    # so its header is one word longer than WDC3's.
    body = WDC3_HEADER_SIZE - 4 + (4 if magic == "WDC1" else 0)
    header_size = (prefix + body + section_header_size
                   + len(inline) * 4 + len(storage_infos)
                   + len(pallet_data) + len(common_data))

    if magic == "WDC1":
        out = bytearray(b"WDC1")
        out += struct.pack("<I", len(rows))
        out += struct.pack("<I", len(inline))
        out += struct.pack("<I", record_size)
        out += struct.pack("<I", len(string_block))
        out += struct.pack("<I", table_hash)
        out += struct.pack("<I", layout_hash)
        out += struct.pack("<I", min(int(r[id_name]) for r in rows) if rows else 0)
        out += struct.pack("<I", max(int(r[id_name]) for r in rows) if rows else 0)
        out += struct.pack("<I", 0)                    # locale
        out += struct.pack("<I", len(copies) * 8)      # copy_table_size
        out += struct.pack("<H", 0x04 if use_id_list else 0)
        out += struct.pack("<H", next((i for i, c in enumerate(inline)
                                       if c.name == id_name), 0))
        out += struct.pack("<I", len(columns))
        out += struct.pack("<I", 0)
        out += struct.pack("<I", 0)
        out += struct.pack("<I", len(storage_infos))
        out += struct.pack("<I", len(common_data))
        out += struct.pack("<I", len(pallet_data))
        out += struct.pack("<I", len(relationship_data))
        for col, offset_bits, size_bits in storages:
            out += struct.pack("<hH", 32 - (size_bits or 32), offset_bits // 8)
        out += storage_infos
        out += pallet_data
        out += common_data
        assert len(out) == header_size, (len(out), header_size)
        return bytes(out) + bytes(section)

    out = bytearray(magic.encode("latin-1"))
    if magic == "WDC5":
        out += struct.pack("<I", 5)
        out += b"TestSchema".ljust(128, b"\0")
    out += struct.pack("<I", len(rows))
    out += struct.pack("<I", len(inline))
    out += struct.pack("<I", record_size)
    out += struct.pack("<I", len(string_block))
    out += struct.pack("<I", table_hash)
    out += struct.pack("<I", layout_hash)
    out += struct.pack("<I", min(int(r[id_name]) for r in rows) if rows else 0)
    out += struct.pack("<I", max(int(r[id_name]) for r in rows) if rows else 0)
    out += struct.pack("<I", 0)                       # locale
    out += struct.pack("<H", 0x04 if use_id_list else 0)
    out += struct.pack("<H", next((i for i, c in enumerate(inline)
                                   if c.name == id_name), 0))
    out += struct.pack("<I", len(columns))            # total_field_count
    out += struct.pack("<I", 0)                       # bitpacked_data_offset
    out += struct.pack("<I", 0)                       # lookup_column_count
    out += struct.pack("<I", len(storage_infos))
    out += struct.pack("<I", len(common_data))
    out += struct.pack("<I", len(pallet_data))
    out += struct.pack("<I", 1)                       # section_count

    out += struct.pack("<Q", 0x1122334455667788 if encrypted else 0)
    out += struct.pack("<I", header_size)             # file_offset
    out += struct.pack("<I", len(rows))
    out += struct.pack("<I", len(string_block))
    if magic in ("WDC2", "1SLC"):
        out += struct.pack("<I", len(copy_data))
        out += struct.pack("<I", 0)                   # offset_map_offset
        out += struct.pack("<I", len(id_list))
        out += struct.pack("<I", len(relationship_data))
    else:
        out += struct.pack("<I", 0)                   # offset_records_end
        out += struct.pack("<I", len(id_list))
        out += struct.pack("<I", len(relationship_data))
        out += struct.pack("<I", 0)                   # offset_map_id_count
        out += struct.pack("<I", len(copies))

    for col, offset_bits, size_bits in storages:
        out += struct.pack("<hH", 32 - (size_bits or 32), offset_bits // 8)
    out += storage_infos
    out += pallet_data
    out += common_data

    assert len(out) == header_size, (len(out), header_size)
    out += section if not encrypted else bytes(len(section))
    return bytes(out)


def build_dbc(field_count: int, rows: Sequence[Sequence[Any]],
              types: Sequence[str] | None = None) -> bytes:
    """A 3.3.5a ``.dbc`` from typed Python rows."""
    types = list(types or ["uint"] * field_count)
    strings = bytearray(b"\0")
    offsets: dict[str, int] = {"": 0}

    def intern(text: str) -> int:
        if text in offsets:
            return offsets[text]
        at = len(strings)
        strings.extend(text.encode("latin-1") + b"\0")
        offsets[text] = at
        return at

    records = bytearray()
    for row in rows:
        record = bytearray(field_count * 4)
        for index in range(field_count):
            value = row[index] if index < len(row) else 0
            kind = types[index] if index < len(types) else "uint"
            at = index * 4
            if kind == "float":
                struct.pack_into("<f", record, at, float(value))
            elif kind == "string":
                struct.pack_into("<I", record, at, intern(str(value)))
            elif kind == "int":
                struct.pack_into("<i", record, at, int(value))
            else:
                struct.pack_into("<I", record, at, int(value) & 0xFFFFFFFF)
        records.extend(record)

    out = bytearray(b"WDBC")
    out += struct.pack("<4I", len(rows), field_count, field_count * 4,
                       len(strings))
    out += records
    out += strings
    return bytes(out)


def build_sparse_wdc3(columns, rows, *, magic: str = "WDC3",
                      layout_hash: int = 0x1FE1BDA4,
                      id_column: str | None = None) -> bytes:
    """A sparse (offset-map) table: variable-length records, inline strings.

    Laid out the way the format describes it -- record region, id list, copy
    table, offset map, then the offset map's own id list -- so the reader's
    walk of that order is what is being tested.
    """
    inline = [c for c in columns if not c.non_inline]
    id_name = id_column or next((c.name for c in columns if c.is_id),
                                columns[0].name)
    ids = [int(r[id_name]) for r in rows]

    blobs = []
    for row in rows:
        blob = bytearray()
        for col in inline:
            value = row.get(col.name)
            items = value if isinstance(value, list) else [value]
            for slot in range(col.array):
                item = items[slot] if slot < len(items) else 0
                if col.type == "string":
                    blob += str(item).encode("latin-1") + b"\0"
                else:
                    blob += _as_words(item, col.type).to_bytes(
                        max(1, col.bits // 8), "little")
        while len(blob) % 4:
            blob += b"\0"
        blobs.append(bytes(blob))

    storage_infos = bytearray()
    bit_offset = 0
    for col in inline:
        size_bits = col.bits
        storage_infos += struct.pack("<HHII3I", bit_offset, size_bits, 0,
                                     STORAGE_NONE, 0, 0, 0)
        bit_offset += size_bits * col.array

    section_header_size = SECTION_HEADER_WDC3
    header_size = (4 + WDC3_HEADER_SIZE - 4 + section_header_size
                   + len(inline) * 4 + len(storage_infos))

    records = b"".join(blobs)
    offsets = []
    cursor = header_size
    for blob in blobs:
        offsets.append((cursor, len(blob)))
        cursor += len(blob)
    records_end = cursor

    offset_map = b"".join(struct.pack("<IH", o, n) for o, n in offsets)
    offset_map_ids = struct.pack("<" + "I" * len(ids), *ids)

    out = bytearray(magic.encode("latin-1"))
    out += struct.pack("<I", len(rows))
    out += struct.pack("<I", len(inline))
    out += struct.pack("<I", 0)                       # record_size: sparse
    out += struct.pack("<I", 0)                       # no string table
    out += struct.pack("<I", 0xDEADBEEF)
    out += struct.pack("<I", layout_hash)
    out += struct.pack("<I", min(ids))
    out += struct.pack("<I", max(ids))
    out += struct.pack("<I", 0)
    out += struct.pack("<H", 0x01)                    # FLAG_SPARSE
    out += struct.pack("<H", 0)
    out += struct.pack("<I", len(columns))
    out += struct.pack("<I", 0)
    out += struct.pack("<I", 0)
    out += struct.pack("<I", len(storage_infos))
    out += struct.pack("<I", 0)
    out += struct.pack("<I", 0)
    out += struct.pack("<I", 1)                       # section_count

    out += struct.pack("<Q", 0)
    out += struct.pack("<I", header_size)             # file_offset
    out += struct.pack("<I", len(rows))
    out += struct.pack("<I", 0)                       # string_table_size
    out += struct.pack("<I", records_end)             # offset_records_end
    out += struct.pack("<I", 0)                       # id_list_size
    out += struct.pack("<I", 0)                       # relationship_data_size
    out += struct.pack("<I", len(rows))               # offset_map_id_count
    out += struct.pack("<I", 0)                       # copy_table_count

    for col in inline:
        out += struct.pack("<hH", 32 - col.bits, 0)
    out += storage_infos
    assert len(out) == header_size, (len(out), header_size)
    return bytes(out) + records + offset_map + offset_map_ids
