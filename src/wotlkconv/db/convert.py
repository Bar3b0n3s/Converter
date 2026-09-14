"""Turning a modern ``.db2`` into a 3.3.5a ``.dbc``.

Two things have to line up: the *data*, which comes out of the DB2 with column
names supplied by a DBD definition, and the *layout*, which comes from a
mapping file and ideally from the user's own client ``.dbc`` used as a
template.

Merging onto a template is the normal case. Converting a model is useless until
something references it, and what references it is a row in a table the user
already has -- so new rows are appended to their existing table rather than
replacing it, and ``--id-offset`` moves the modern ids into a range that will
not collide with Blizzard's.
"""

from __future__ import annotations

import os
import time
from typing import Any, Sequence

from ..listfile import Listfile
from ..options import Options
from ..report import FileResult, Status
from . import dbd
from .db2 import Db2Table, parse_db2
from .dbc import DbcBuilder, DbcTable
from .mapping import (MappingLibrary, TableMapping, TransformContext, apply_row,
                      missing_sources, require)

#: Where client databases live in the game's file tree.
DB2_DIRECTORY = "dbfilesclient"


def table_name_for(path: str) -> str:
    """``dbfilesclient/creaturedisplayinfo.db2`` -> ``creaturedisplayinfo``."""
    stem = os.path.splitext(os.path.basename(path.replace("\\", "/")))[0]
    return stem


def find_template(directory: str | os.PathLike[str] | None,
                  table: str) -> bytes | None:
    """Look for ``<Table>.dbc`` in a directory, case-insensitively."""
    if not directory:
        return None
    from pathlib import Path

    base = Path(directory)
    if not base.is_dir():
        return None
    wanted = f"{table}.dbc".lower()
    for candidate in base.iterdir():
        if candidate.is_file() and candidate.name.lower() == wanted:
            return candidate.read_bytes()
    return None


def _row_matches(row: dict[str, Any], where: Sequence[tuple[str, str]]) -> bool:
    for column, expected in where:
        value = row.get(column)
        if isinstance(value, list):
            if not any(str(v) == expected for v in value):
                return False
        elif str(value) != expected:
            return False
    return True


def convert_db2(data: bytes, source_name: str, opts: Options,
                listfile: Listfile | None = None,
                definitions: dbd.DbdIndex | None = None,
                library: MappingLibrary | None = None,
                template_data: bytes | None = None,
                table: str | None = None,
                result: FileResult | None = None) -> tuple[bytes, FileResult]:
    """Convert one client database. Returns (DBC bytes, result)."""
    started = time.time()
    res = result or FileResult(source=source_name, kind="db2")
    res.kind = "db2"
    res.bytes_in = len(data)
    listfile = listfile or Listfile()
    library = library or MappingLibrary(opts.db_mappings or None)

    table_name = table or table_name_for(source_name)
    parsed: Db2Table = parse_db2(data, source_name, definitions, table_name)
    res.source_version = (f"{parsed.magic} {len(parsed.rows)} row(s), "
                          f"{len(parsed.columns)} column(s)")

    if not parsed.named:
        res.fail("db2.no_definition",
                 f"no DBD definition for {table_name}, so its columns are "
                 f"anonymous and cannot be mapped by name. Pass --dbd pointing "
                 f"at the 'definitions' folder of a WoWDBDefs checkout")
        res.elapsed = time.time() - started
        return b"", res

    if parsed.encrypted_sections:
        res.lossy("db2.encrypted",
                  f"{parsed.encrypted_sections} section(s) are encrypted and "
                  f"were skipped, losing {parsed.skipped_records} row(s); "
                  f"supply the TACT key with --casc-keys if you have it",
                  sections=parsed.encrypted_sections,
                  records=parsed.skipped_records)

    mapping: TableMapping = require(library, table_name)

    # -- layout ----------------------------------------------------------
    template: DbcTable | None = None
    if template_data:
        template = DbcTable.parse(template_data, f"{table_name}.dbc")
        if mapping.target_field_count and \
                template.field_count != mapping.target_field_count:
            res.fail("db2.layout_mismatch",
                     f"the template {table_name}.dbc has "
                     f"{template.field_count} fields but the mapping describes "
                     f"{mapping.target_field_count}. The mapping is wrong for "
                     f"this client build; correct its target_field_count and "
                     f"column indices, or drop the template to use the mapping "
                     f"as-is",
                     template_fields=template.field_count,
                     mapping_fields=mapping.target_field_count)
            res.elapsed = time.time() - started
            return b"", res
        res.info("db2.template",
                 f"layout verified against {table_name}.dbc "
                 f"({template.field_count} fields, {len(template)} existing row(s))")
    elif not mapping.verified and mapping.target_field_count:
        res.warn("db2.unverified_layout",
                 f"using the built-in field count for {table_name} "
                 f"({mapping.target_field_count}), which has not been checked "
                 f"against a real client .dbc. Pass --template with your "
                 f"client's {table_name}.dbc so the layout is verified rather "
                 f"than assumed")
    elif not mapping.target_field_count:
        res.fail("db2.no_layout",
                 f"the mapping for {table_name} declares no target_field_count, "
                 f"so the table's width is unknown; pass --template with your "
                 f"client's {table_name}.dbc")
        res.elapsed = time.time() - started
        return b"", res

    field_count = template.field_count if template else mapping.target_field_count

    absent = missing_sources(mapping, parsed.column_names())
    if absent:
        res.lossy("db2.columns_absent",
                  f"{len(absent)} mapped column(s) do not exist in this build's "
                  f"{table_name} and were written as their defaults: "
                  + ", ".join(c.describe() for c in absent[:8]),
                  columns=[c.describe() for c in absent])

    merge = bool(template) and opts.db_merge
    builder = DbcBuilder(field_count, template if merge else None)
    if merge:
        builder.index_existing(mapping.id_index)
    existing_before = len(builder.records)

    ctx = TransformContext(listfile, opts.path_prefix)

    # -- rows -------------------------------------------------------------
    wanted_ids = set(opts.db_row_ids) if opts.db_row_ids else None
    where = opts.db_where
    added = replaced = 0
    for row_id, row in parsed:
        if wanted_ids is not None and row_id not in wanted_ids:
            continue
        if where and not _row_matches(row, where):
            continue
        values = apply_row(mapping, row, ctx, opts.db_id_offset)
        target_id = values.get(mapping.id_index, ("uint", row_id))[1]
        if builder.add(values, int(target_id), mapping.id_index):
            added += 1
        else:
            replaced += 1

    if ctx.missing:
        sample = sorted(ctx.missing)[:8]
        res.lossy("db2.unresolved_files",
                  f"{len(ctx.missing)} FileDataID(s) referenced by this table "
                  f"are not in the listfile, so their paths came out empty "
                  f"(e.g. {', '.join(str(i) for i in sample)})",
                  count=len(ctx.missing))

    if added == 0 and replaced == 0:
        res.warn("db2.no_rows",
                 "no rows matched the selection, so the output only carries "
                 "whatever the template already had")

    out = builder.serialize()
    res.bytes_out = len(out)
    res.target_version = (f"DBC {field_count} fields, "
                          f"{len(builder.records)} row(s)")
    res.extra.update({
        "table": table_name,
        "source_rows": len(parsed.rows),
        "rows_added": added,
        "rows_replaced": replaced,
        "rows_kept": existing_before,
        "id_offset": opts.db_id_offset,
        "merged": merge,
    })
    res.info("db2.converted",
             f"{added} row(s) added" + (f", {replaced} replaced" if replaced else "")
             + (f", {existing_before} kept from the template" if merge else ""))
    if res.status is Status.OK:
        res.status = Status.LOSSY if absent else Status.OK
    res.elapsed = time.time() - started
    return out, res
