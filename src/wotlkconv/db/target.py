"""Working out the 3.3.5a side of a table.

The layouts this converter writes into used to be hardcoded guesses, which is
the worst place for a guess: a `.dbc` with the wrong number of columns loads
and renders nonsense rather than failing. They do not have to be guessed.

DBDefs carries a ``BUILD 3.3.5.12340`` layout for every table that existed in
Wrath, listing exactly the columns that build had, in order, with their types
and array sizes. That is the same source the modern side of the conversion
already depends on, and it is authoritative in a way a hand-written table never
is -- so it is used in preference to anything the mapping declares, and a
template `.dbc` cross-checks it.

Because both sides are then named, most columns map themselves: a modern
``CollisionHeight`` and a Wrath ``CollisionHeight`` are the same thing. A
mapping file only has to describe the columns that actually changed -- the
FileDataIDs that used to be paths, and the handful Blizzard renamed.
"""

from __future__ import annotations

import dataclasses

from .. import log
from . import dbd

#: The build this tool targets, as DBDefs spells it.
WOTLK_BUILD = "3.3.5.12340"

#: Other spellings of the same client seen in definition files.
WOTLK_BUILD_ALIASES = ("3.3.5.12340", "3.3.5a.12340", "3.3.5.12213",
                       "3.3.3.11723", "3.3.0.10772")


@dataclasses.dataclass(slots=True)
class TargetField:
    """One four-byte field of the 3.3.5a table."""

    index: int
    name: str
    type: str = "uint"
    #: Which element, for a column that occupies several fields.
    array_index: int = 0
    array_size: int = 1

    @property
    def label(self) -> str:
        if self.array_size > 1:
            return f"{self.name}[{self.array_index}]"
        return self.name


@dataclasses.dataclass
class TargetLayout:
    """The full field list of the table being written."""

    fields: list[TargetField] = dataclasses.field(default_factory=list)
    #: Where this came from, for the report.
    origin: str = "mapping"
    build: str = ""
    #: False when only the width is known, so the field names are placeholders
    #: and nothing can be matched to them by name.
    named: bool = True

    @property
    def field_count(self) -> int:
        return len(self.fields)

    def by_name(self, name: str, array_index: int = 0) -> TargetField | None:
        lowered = name.lower()
        for field in self.fields:
            if field.name.lower() == lowered and field.array_index == array_index:
                return field
        return None

    def column_names(self) -> list[str]:
        seen: list[str] = []
        for field in self.fields:
            if field.name not in seen:
                seen.append(field.name)
        return seen


def _dbc_type(column: dbd.Column) -> str:
    """A DBC field is always four bytes; only the reading of it varies."""
    if column.type == "float":
        return "float"
    if column.type == "string":
        return "string"
    return "int" if column.signed else "uint"


def layout_from_dbd(definition: dbd.Definition,
                    build: str = WOTLK_BUILD) -> TargetLayout | None:
    """Build the 3.3.5a field list from a table's own definition.

    Returns ``None`` when the definition has no layout covering that build,
    which means the table did not exist in Wrath -- there is nothing to write
    into and the caller has to say so.
    """
    layout = None
    matched = build
    for candidate in (build, *WOTLK_BUILD_ALIASES):
        layout = definition.by_build(candidate)
        if layout is not None:
            matched = candidate
            break
    if layout is None:
        return None

    out = TargetLayout(origin=f"dbd:{definition.name}", build=matched)
    index = 0
    for column in layout.columns:
        if column.relation:
            continue
        kind = _dbc_type(column)
        for element in range(max(1, column.array_size)):
            out.fields.append(TargetField(index, column.name, kind, element,
                                          max(1, column.array_size)))
            index += 1
    return out


def layout_from_field_count(count: int, origin: str = "mapping") -> TargetLayout:
    """A bare field list, for when only the width is known."""
    return TargetLayout(
        fields=[TargetField(i, f"field_{i}", "uint") for i in range(count)],
        origin=origin, named=False)


@dataclasses.dataclass(slots=True)
class AutoColumn:
    """A column matched between the two builds by name alone."""

    target: TargetField
    source: str
    array_index: int


def auto_map(target: TargetLayout, source_columns: list[dbd.Column],
             claimed: set[int]) -> list[AutoColumn]:
    """Match same-named columns on both sides.

    Only columns whose types agree are matched: a modern ``FileDataID`` integer
    and a Wrath ``ModelName`` string describe the same thing but need a
    transform, and that has to be written down rather than guessed at.
    """
    by_name = {c.name.lower(): c for c in source_columns}
    out: list[AutoColumn] = []
    for field in target.fields:
        if field.index in claimed:
            continue
        column = by_name.get(field.name.lower())
        if column is None:
            continue
        if _dbc_type(column) != field.type:
            continue
        if field.array_index >= max(1, column.array_size):
            continue
        out.append(AutoColumn(field, column.name, field.array_index))
    return out


def describe(target: TargetLayout) -> str:
    return (f"{target.field_count} fields from {target.origin}"
            + (f" (build {target.build})" if target.build else ""))


def cross_check(target: TargetLayout, template_fields: int,
                table: str) -> str | None:
    """Compare a derived layout with the user's own table. None if they agree."""
    if target.field_count == template_fields:
        return None
    return (f"{table}: the definition for build {target.build or '?'} describes "
            f"{target.field_count} fields but your {table}.dbc has "
            f"{template_fields}. Your client is not the build the definition "
            f"covers, or the definition is wrong for it")


def log_origin(target: TargetLayout, table: str) -> None:
    if target.origin.startswith("dbd:"):
        log.debug(f"{table}: 3.3.5a layout taken from its definition "
                  f"({target.field_count} fields, build {target.build})")
