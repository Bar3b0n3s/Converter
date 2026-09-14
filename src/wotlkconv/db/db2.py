"""Reading modern client databases (``.db2``).

Cataclysm renamed ``.dbc`` to ``.db2`` and, from Legion onwards, stopped
storing records as plain structs. A WDC-family table packs each column to the
minimum width its values need, hoists constant columns into a side table, and
replaces repeated values with indices into a palette. Records can also be
scattered across sections, duplicated through a copy table, addressed by a
sparse offset map, and -- for unreleased content -- encrypted.

Supported magics:

===========  =============================  ===================================
``WDC5``     The War Within                 WDC4 body behind a schema string
``WDC4``     Dragonflight                   as WDC3
``WDC3``     BfA 8.2 .. Shadowlands         sections, 40-byte section header
``WDC2``     BfA 8.0                        sections, 36-byte section header
``WDC1``     Legion 7.3                     bitpacking, no sections
``WDB6``     Legion 7.2                     flat records plus common data
``WDB5``     Legion 7.0                     flat records
``WDB2``     Cataclysm .. Warlords          flat records
===========  =============================  ===================================

Column names, types and array sizes come from a DBD definition matched on the
file's own layout hash; without one the columns are still readable, just
anonymous.
"""

from __future__ import annotations

import dataclasses
import struct
from typing import Any, Iterator

from .. import log
from ..errors import MalformedFileError, UnsupportedFormatError
from . import dbd
from .bits import as_float, read_bits, sign_extend

#: Header size after the magic, for each supported family.
WDC3_BODY = 68
WDC1_BODY = 72
WDB5_BODY = 44
WDB2_BODY = 44

FIELD_STORAGE_INFO_SIZE = 24

# field_storage_info.storage_type
STORAGE_NONE = 0
STORAGE_BITPACKED = 1
STORAGE_COMMON_DATA = 2
STORAGE_BITPACKED_INDEXED = 3
STORAGE_BITPACKED_INDEXED_ARRAY = 4
STORAGE_BITPACKED_SIGNED = 5

#: header.flags
FLAG_SPARSE = 0x01
FLAG_SECONDARY_KEY = 0x02
FLAG_NON_INLINE_IDS = 0x04
FLAG_BITPACKED = 0x10


@dataclasses.dataclass(slots=True)
class FieldStorage:
    field_offset_bits: int
    field_size_bits: int
    additional_data_size: int
    storage_type: int
    compression: tuple[int, int, int]
    #: Byte offset of this column's slice inside the pallet/common blocks.
    data_offset: int = 0

    @property
    def array_count(self) -> int:
        if self.storage_type == STORAGE_BITPACKED_INDEXED_ARRAY:
            return max(1, self.compression[2])
        return 1


@dataclasses.dataclass(slots=True)
class Section:
    tact_key_hash: int = 0
    file_offset: int = 0
    record_count: int = 0
    string_table_size: int = 0
    offset_records_end: int = 0
    id_list_size: int = 0
    relationship_data_size: int = 0
    offset_map_id_count: int = 0
    copy_table_count: int = 0
    #: WDC2 stores the copy table in bytes rather than as an entry count.
    copy_table_size: int = 0
    offset_map_offset: int = 0

    @property
    def encrypted(self) -> bool:
        return self.tact_key_hash != 0


@dataclasses.dataclass
class Db2Table:
    """A decoded client database."""

    name: str = ""
    magic: str = ""
    version: int = 0
    table_hash: int = 0
    layout_hash: int = 0
    record_count: int = 0
    field_count: int = 0
    record_size: int = 0
    locale: int = 0
    flags: int = 0
    min_id: int = 0
    max_id: int = 0
    columns: list[dbd.Column] = dataclasses.field(default_factory=list)
    rows: dict[int, dict[str, Any]] = dataclasses.field(default_factory=dict)
    #: Sections whose data could not be read because they are encrypted.
    encrypted_sections: int = 0
    skipped_records: int = 0
    #: True when column names came from a DBD rather than being synthesised.
    named: bool = False

    @property
    def ids(self) -> list[int]:
        return sorted(self.rows)

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[tuple[int, dict[str, Any]]]:
        for key in sorted(self.rows):
            yield key, self.rows[key]

    def describe(self) -> str:
        return (f"{self.magic} '{self.name}' layout {self.layout_hash:08X}, "
                f"{len(self.rows)} row(s), {len(self.columns)} column(s)")


class _Cursor:
    """Sequential reader over the header area."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def u32(self) -> int:
        value = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return value

    def i32(self) -> int:
        value = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return value

    def u16(self) -> int:
        value = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return value

    def u64(self) -> int:
        value = struct.unpack_from("<Q", self.data, self.pos)[0]
        self.pos += 8
        return value

    def raw(self, count: int) -> bytes:
        chunk = self.data[self.pos : self.pos + count]
        self.pos += count
        return chunk


def _synthetic_columns(count: int, storages: list[FieldStorage],
                       id_index: int) -> list[dbd.Column]:
    """Anonymous columns, for when no DBD definition is available."""
    columns = []
    for i in range(count):
        storage = storages[i] if i < len(storages) else None
        width = storage.field_size_bits if storage else 32
        columns.append(dbd.Column(name=f"field_{i}", type="int",
                                  bit_width=max(1, width),
                                  array_size=storage.array_count if storage else 1,
                                  is_id=(i == id_index)))
    return columns


def _read_header(data: bytes, name: str) -> tuple[dict, _Cursor, str]:
    if len(data) < 8:
        raise MalformedFileError(f"{name}: file is only {len(data)} bytes")
    magic = data[:4].decode("latin-1")
    cursor = _Cursor(data, 4)
    header: dict[str, Any] = {"magic": magic, "version": 0, "schema": ""}

    if magic == "WDC5":
        header["version"] = cursor.u32()
        header["schema"] = cursor.raw(128).split(b"\0", 1)[0].decode("latin-1")
    elif magic not in ("WDC4", "WDC3", "WDC2", "1SLC", "WDC1",
                       "WDB6", "WDB5", "WDB4", "WDB3", "WDB2"):
        raise UnsupportedFormatError(
            f"{name}: not a client database (magic {magic!r})")

    header["record_count"] = cursor.u32()
    header["field_count"] = cursor.u32()
    header["record_size"] = cursor.u32()
    header["string_table_size"] = cursor.u32()

    if magic in ("WDB2", "WDB3", "WDB4"):
        header["table_hash"] = cursor.u32()
        header["build"] = cursor.u32()
        header["timestamp"] = cursor.u32()
        header["min_id"] = cursor.u32()
        header["max_id"] = cursor.u32()
        header["locale"] = cursor.u32()
        header["copy_table_size"] = cursor.u32()
        header["layout_hash"] = 0
        header["flags"] = 0
        header["id_index"] = 0
        header["total_field_count"] = header["field_count"]
        header["section_count"] = 1
        return header, cursor, magic

    header["table_hash"] = cursor.u32()
    header["layout_hash"] = cursor.u32()
    header["min_id"] = cursor.u32()
    header["max_id"] = cursor.u32()
    header["locale"] = cursor.u32()

    if magic in ("WDB5", "WDB6"):
        header["copy_table_size"] = cursor.u32()
        header["flags"] = cursor.u16()
        header["id_index"] = cursor.u16()
        header["total_field_count"] = (cursor.u32() if magic == "WDB6"
                                       else header["field_count"])
        header["common_data_size"] = cursor.u32() if magic == "WDB6" else 0
        header["section_count"] = 1
        return header, cursor, magic

    if magic == "WDC1":
        header["copy_table_size"] = cursor.u32()
        header["flags"] = cursor.u16()
        header["id_index"] = cursor.u16()
        header["total_field_count"] = cursor.u32()
        header["bitpacked_data_offset"] = cursor.u32()
        header["lookup_column_count"] = cursor.u32()
        header["field_storage_info_size"] = cursor.u32()
        header["common_data_size"] = cursor.u32()
        header["pallet_data_size"] = cursor.u32()
        header["relationship_data_size"] = cursor.u32()
        header["section_count"] = 1
        return header, cursor, magic

    # WDC2 / 1SLC / WDC3 / WDC4 / WDC5
    header["flags"] = cursor.u16()
    header["id_index"] = cursor.u16()
    header["total_field_count"] = cursor.u32()
    header["bitpacked_data_offset"] = cursor.u32()
    header["lookup_column_count"] = cursor.u32()
    header["field_storage_info_size"] = cursor.u32()
    header["common_data_size"] = cursor.u32()
    header["pallet_data_size"] = cursor.u32()
    header["section_count"] = cursor.u32()
    return header, cursor, magic


def _read_sections(cursor: _Cursor, magic: str, count: int) -> list[Section]:
    sections = []
    for _ in range(count):
        section = Section()
        section.tact_key_hash = cursor.u64()
        section.file_offset = cursor.u32()
        section.record_count = cursor.u32()
        section.string_table_size = cursor.u32()
        if magic in ("WDC2", "1SLC"):
            section.copy_table_size = cursor.u32()
            section.offset_map_offset = cursor.u32()
            section.id_list_size = cursor.u32()
            section.relationship_data_size = cursor.u32()
        else:
            section.offset_records_end = cursor.u32()
            section.id_list_size = cursor.u32()
            section.relationship_data_size = cursor.u32()
            section.offset_map_id_count = cursor.u32()
            section.copy_table_count = cursor.u32()
        sections.append(section)
    return sections


def _read_storage_info(cursor: _Cursor, total_bytes: int) -> list[FieldStorage]:
    storages = []
    pallet_offset = 0
    common_offset = 0
    for _ in range(total_bytes // FIELD_STORAGE_INFO_SIZE):
        offset_bits, size_bits, extra, storage_type = struct.unpack_from(
            "<HHII", cursor.data, cursor.pos)
        compression = struct.unpack_from("<3I", cursor.data, cursor.pos + 12)
        cursor.pos += FIELD_STORAGE_INFO_SIZE
        storage = FieldStorage(offset_bits, size_bits, extra, storage_type,
                               compression)
        # Pallet and common blocks are one flat run each; every column takes
        # the next slice, so the offsets accumulate in field order.
        if storage_type in (STORAGE_BITPACKED_INDEXED,
                            STORAGE_BITPACKED_INDEXED_ARRAY):
            storage.data_offset = pallet_offset
            pallet_offset += extra
        elif storage_type == STORAGE_COMMON_DATA:
            storage.data_offset = common_offset
            common_offset += extra
        storages.append(storage)
    return storages


def _common_map(common_data: bytes, storage: FieldStorage) -> dict[int, int]:
    out: dict[int, int] = {}
    start = storage.data_offset
    for i in range(storage.additional_data_size // 8):
        key, value = struct.unpack_from("<II", common_data, start + i * 8)
        out[key] = value
    return out


def _pallet_values(pallet_data: bytes, storage: FieldStorage) -> list[int]:
    start = storage.data_offset
    count = storage.additional_data_size // 4
    if count == 0:
        return []
    return list(struct.unpack_from("<" + "I" * count, pallet_data, start))


def _coerce(raw: int, column: dbd.Column, storage: FieldStorage | None) -> Any:
    if column.type == "float":
        return as_float(raw)
    width = storage.field_size_bits if storage else column.bit_width
    if column.signed and column.type == "int" and width and width < 64:
        return sign_extend(raw, width)
    return raw


def parse_db2(data: bytes, name: str = "<db2>",
              definitions: dbd.DbdIndex | None = None,
              table_name: str | None = None) -> Db2Table:
    """Decode a ``.db2`` into rows keyed by record ID."""
    header, cursor, magic = _read_header(data, name)
    table = Db2Table(
        name=table_name or name,
        magic=magic,
        version=header.get("version", 0),
        table_hash=header.get("table_hash", 0),
        layout_hash=header.get("layout_hash", 0),
        record_count=header["record_count"],
        field_count=header["field_count"],
        record_size=header["record_size"],
        locale=header.get("locale", 0),
        flags=header.get("flags", 0),
        min_id=header.get("min_id", 0),
        max_id=header.get("max_id", 0),
    )

    sectioned = magic in ("WDC2", "1SLC", "WDC3", "WDC4", "WDC5")
    if sectioned:
        sections = _read_sections(cursor, magic, header["section_count"])
    else:
        sections = [Section(file_offset=0, record_count=header["record_count"],
                            string_table_size=header["string_table_size"])]

    # -- field structures (widths and byte positions) -------------------
    field_structs = []
    for _ in range(header["field_count"]):
        size, position = struct.unpack_from("<hH", cursor.data, cursor.pos)
        cursor.pos += 4
        field_structs.append((32 - size, position))

    storages: list[FieldStorage] = []
    pallet_data = b""
    common_data = b""
    if magic in ("WDC1", "WDC2", "1SLC", "WDC3", "WDC4", "WDC5"):
        storages = _read_storage_info(cursor, header.get("field_storage_info_size", 0))
        pallet_data = cursor.raw(header.get("pallet_data_size", 0))
        common_data = cursor.raw(header.get("common_data_size", 0))
    elif magic == "WDB6":
        common_data = b""  # WDB6 common data follows the copy table; rare

    # -- columns --------------------------------------------------------
    layout = None
    resolved_table = table_name or name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if definitions:
        definition = definitions.get(resolved_table)
        if definition is not None:
            layout = (definition.by_hash(table.layout_hash)
                      or definition.newest())
            if layout is not None and not definition.by_hash(table.layout_hash):
                log.warn(f"{name}: no DBD layout for hash "
                         f"{table.layout_hash:08X}; using the newest one, so "
                         f"column names may be off")
    if layout is not None:
        table.columns = list(layout.columns)
        table.named = True
    else:
        table.columns = _synthetic_columns(
            header.get("total_field_count", header["field_count"]),
            storages, header.get("id_index", 0))

    inline = [c for c in table.columns if not (c.non_inline or c.relation)]
    if storages and len(inline) != len(storages):
        log.warn(f"{name}: DBD lists {len(inline)} inline column(s) but the "
                 f"file has {len(storages)}; falling back to anonymous columns")
        table.columns = _synthetic_columns(len(storages), storages,
                                           header.get("id_index", 0))
        table.named = False
        inline = table.columns

    id_column = next((c for c in table.columns if c.is_id), None)
    if id_column is None and table.columns:
        id_column = table.columns[header.get("id_index", 0)
                                  if header.get("id_index", 0) < len(table.columns)
                                  else 0]
        id_column.is_id = True

    caches = _StorageCaches(pallet_data, common_data, storages)

    # -- sections -------------------------------------------------------
    for section in sections:
        if section.encrypted:
            probe_at = section.file_offset
            probe = data[probe_at : probe_at + min(64, section.record_count *
                                                   max(1, table.record_size))]
            if not probe or not any(probe):
                table.encrypted_sections += 1
                table.skipped_records += section.record_count
                log.debug(f"{name}: section is encrypted with TACT key "
                          f"{section.tact_key_hash:016X}; skipped")
                continue
        _read_section(data, table, section, header, magic, inline, storages,
                      caches, field_structs, id_column, name)

    return table


class _StorageCaches:
    """Per-column pallet slices and common-data maps, built once."""

    __slots__ = ("pallets", "commons")

    def __init__(self, pallet_data: bytes, common_data: bytes,
                 storages: list[FieldStorage]):
        self.pallets: dict[int, list[int]] = {}
        self.commons: dict[int, dict[int, int]] = {}
        for index, storage in enumerate(storages):
            if storage.storage_type in (STORAGE_BITPACKED_INDEXED,
                                        STORAGE_BITPACKED_INDEXED_ARRAY):
                self.pallets[index] = _pallet_values(pallet_data, storage)
            elif storage.storage_type == STORAGE_COMMON_DATA:
                self.commons[index] = _common_map(common_data, storage)


def _read_section(data: bytes, table: Db2Table, section: Section, header: dict,
                  magic: str, inline: list[dbd.Column],
                  storages: list[FieldStorage], caches: _StorageCaches,
                  field_structs: list[tuple[int, int]],
                  id_column: dbd.Column | None, name: str) -> None:
    sparse = bool(header.get("flags", 0) & FLAG_SPARSE)
    pos = section.file_offset
    record_size = table.record_size

    if sparse:
        record_blobs = _read_sparse_records(data, table, section, header,
                                            magic, name)
        record_data = b""
        string_data = b""
    else:
        record_bytes = section.record_count * record_size
        record_data = data[pos : pos + record_bytes]
        pos += record_bytes
        string_data = data[pos : pos + section.string_table_size]
        pos += section.string_table_size
        record_blobs = None

    id_list: list[int] = []
    if section.id_list_size:
        count = section.id_list_size // 4
        id_list = list(struct.unpack_from("<" + "I" * count, data, pos))
        pos += section.id_list_size

    copy_pairs: list[tuple[int, int]] = []
    copy_count = section.copy_table_count or (section.copy_table_size // 8)
    if copy_count:
        for i in range(copy_count):
            new_id, old_id = struct.unpack_from("<II", data, pos + i * 8)
            copy_pairs.append((new_id, old_id))
        pos += copy_count * 8

    relationships: dict[int, int] = {}
    if section.relationship_data_size:
        entries, _min_id, _max_id = struct.unpack_from("<III", data, pos)
        for i in range(entries):
            foreign_id, record_index = struct.unpack_from(
                "<II", data, pos + 12 + i * 8)
            relationships[record_index] = foreign_id
        pos += section.relationship_data_size

    id_position = inline.index(id_column) if id_column in inline else -1

    count = (len(record_blobs) if record_blobs is not None
             else section.record_count)
    for index in range(count):
        if record_blobs is not None:
            blob, row_id = record_blobs[index]
            values = _decode_sparse_record(blob, inline, storages)
        else:
            start = index * record_size
            blob = record_data[start : start + record_size]
            # The id has to come first: common-data columns are keyed by it.
            if id_list:
                row_id = id_list[index] if index < len(id_list) else index
            elif id_position >= 0:
                raw = _decode_column(
                    blob, start, id_column,
                    storages[id_position] if id_position < len(storages) else None,
                    id_position, caches, record_data, string_data, index)
                row_id = int(raw[0])
            else:
                row_id = index
            values = _decode_record(blob, start, inline, storages, caches,
                                    record_data, string_data, row_id)

        if id_column is not None:
            values[id_column.name] = row_id
        for column in table.columns:
            if column.relation:
                values[column.name] = relationships.get(index, 0)
        table.rows[int(row_id)] = values

    for new_id, old_id in copy_pairs:
        source = table.rows.get(old_id)
        if source is None:
            continue
        clone = dict(source)
        if id_column is not None:
            clone[id_column.name] = new_id
        table.rows[new_id] = clone


def _read_sparse_records(data: bytes, table: Db2Table, section: Section,
                         header: dict, magic: str,
                         name: str) -> list[tuple[bytes, int]]:
    """Variable-length records addressed by an ``(offset, size)`` map."""
    span = table.max_id - table.min_id + 1
    if span <= 0 or span > 5_000_000:
        raise MalformedFileError(
            f"{name}: sparse table claims ids {table.min_id}..{table.max_id}")
    map_offset = (section.offset_map_offset if magic in ("WDC2", "1SLC")
                  else section.offset_records_end)
    blobs: list[tuple[bytes, int]] = []
    for i in range(span):
        offset, size = struct.unpack_from("<IH", data, map_offset + i * 6)
        if size:
            blobs.append((data[offset : offset + size], table.min_id + i))
    return blobs


def _decode_sparse_record(blob: bytes, inline: list[dbd.Column],
                          storages: list[FieldStorage]) -> dict[str, Any]:
    """Sparse records are byte-aligned with strings stored inline."""
    values: dict[str, Any] = {}
    pos = 0
    for index, column in enumerate(inline):
        storage = storages[index] if index < len(storages) else None
        width = (storage.field_size_bits if storage else column.bit_width) or 32
        size = max(1, width // 8)
        items = []
        for _ in range(column.array_size):
            if column.is_string:
                end = blob.find(b"\0", pos)
                if end < 0:
                    end = len(blob)
                items.append(blob[pos:end].decode("latin-1"))
                pos = end + 1
            else:
                raw = int.from_bytes(blob[pos : pos + size], "little")
                pos += size
                items.append(_coerce(raw, column, storage))
        values[column.name] = items[0] if column.array_size == 1 else items
    return values


def _decode_record(blob: bytes, record_start: int, inline: list[dbd.Column],
                   storages: list[FieldStorage], caches: _StorageCaches,
                   record_data: bytes, string_data: bytes,
                   row_id: int) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field_index, column in enumerate(inline):
        storage = storages[field_index] if field_index < len(storages) else None
        items = _decode_column(blob, record_start, column, storage, field_index,
                               caches, record_data, string_data, row_id)
        if len(items) < column.array_size:
            items = list(items) + [0] * (column.array_size - len(items))
        values[column.name] = items[0] if column.array_size == 1 else items
    return values


def _decode_column(blob: bytes, record_start: int, column: dbd.Column,
                   storage: FieldStorage | None, field_index: int,
                   caches: _StorageCaches, record_data: bytes,
                   string_data: bytes, row_id: int) -> list[Any]:
    if storage is None:
        return [0] * column.array_size

    kind = storage.storage_type
    if kind == STORAGE_COMMON_DATA:
        # One value per record id, defaulting to a constant for every row the
        # side table does not mention.
        default = storage.compression[0]
        raw = caches.commons.get(field_index, {}).get(row_id, default)
        return [_coerce(raw, column, storage)]

    if kind in (STORAGE_BITPACKED_INDEXED, STORAGE_BITPACKED_INDEXED_ARRAY):
        pallet = caches.pallets.get(field_index, [])
        slot = read_bits(blob, storage.field_offset_bits, storage.field_size_bits)
        count = storage.array_count
        base = slot * count
        raw_values = pallet[base : base + count]
        if len(raw_values) < count:
            raw_values = list(raw_values) + [0] * (count - len(raw_values))
        return [_coerce(v, column, storage) for v in raw_values]

    if kind in (STORAGE_BITPACKED, STORAGE_BITPACKED_SIGNED):
        raw = read_bits(blob, storage.field_offset_bits, storage.field_size_bits)
        if kind == STORAGE_BITPACKED_SIGNED:
            return [sign_extend(raw, storage.field_size_bits)]
        return [_coerce(raw, column, storage)]

    # STORAGE_NONE: one element per array slot, each field_size_bits wide.
    out: list[Any] = []
    for slot in range(column.array_size):
        bit_offset = storage.field_offset_bits + slot * storage.field_size_bits
        raw = read_bits(blob, bit_offset, storage.field_size_bits)
        if column.is_string:
            # String offsets are relative to the field's own position in the
            # record data block, which the string table follows.
            absolute = record_start + (bit_offset >> 3) + raw
            out.append(_read_string(record_data, string_data, absolute))
        else:
            out.append(_coerce(raw, column, storage))
    return out


def _read_string(record_data: bytes, string_data: bytes, absolute: int) -> str:
    offset = absolute - len(record_data)
    if offset < 0 or offset >= len(string_data):
        return ""
    end = string_data.find(b"\0", offset)
    if end < 0:
        end = len(string_data)
    return string_data[offset:end].decode("latin-1")


def inspect_db2(data: bytes, source_name: str,
                definitions: dbd.DbdIndex | None = None) -> dict:
    table = parse_db2(data, source_name, definitions)
    return {
        "kind": "db2",
        "magic": table.magic,
        "table": table.name,
        "layout_hash": f"{table.layout_hash:08X}",
        "rows": len(table.rows),
        "columns": len(table.columns),
        "column_names": table.column_names() if table.named else None,
        "named_columns": table.named,
        "encrypted_sections": table.encrypted_sections or None,
        "skipped_records": table.skipped_records or None,
        "wotlk_compatible": False,
    }
