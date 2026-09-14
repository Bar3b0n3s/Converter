"""Declarative DB2 column -> DBC column mappings.

A modern table and its 3.3.5a ancestor share a name and very little else: the
columns were reordered, split, merged and renamed across fifteen years. There
is no way to derive one layout from the other, so the correspondence is data:
a JSON file per table, shipped in ``builtin/`` and overridable by the user.

    {
      "table": "CreatureDisplayInfo",
      "target_field_count": 16,
      "id_index": 0,
      "columns": [
        {"index": 0,  "type": "uint",   "from": "ID", "id_offset": true},
        {"index": 1,  "type": "uint",   "from": "ModelID", "id_offset": true},
        {"index": 4,  "type": "float",  "from": "CreatureModelScale",
         "default": 1.0},
        {"index": 6,  "type": "string", "from": "TextureVariationFileDataID",
         "array_index": 0, "transform": "basename"}
      ]
    }

Target indices the mapping does not list are written as zero. ``from`` may be a
list, in which case the first column the source actually has wins -- which is
how one mapping covers several builds whose column names drifted.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Iterable

from ..errors import ConversionError, MissingDependencyError
from ..listfile import Listfile, normalise
from .dbc import TYPES

BUILTIN_DIR = Path(__file__).parent / "builtin"


def _strip_extension(path: str) -> str:
    return os.path.splitext(path)[0]


@dataclasses.dataclass(slots=True)
class ColumnMap:
    """How one 3.3.5a field gets its value."""

    index: int
    type: str = "uint"
    source: tuple[str, ...] = ()
    array_index: int | None = None
    const: Any = None
    default: Any = None
    transform: str = ""
    scale: float | None = None
    #: Add the run's id offset, for the id column and references to it.
    id_offset: bool = False
    note: str = ""

    def describe(self) -> str:
        if self.const is not None:
            return f"[{self.index}] = {self.const!r}"
        return f"[{self.index}] <- {'|'.join(self.source) or '?'}"


@dataclasses.dataclass
class TableMapping:
    """A whole table's worth of column maps."""

    table: str
    target_field_count: int = 0
    id_index: int = 0
    columns: list[ColumnMap] = dataclasses.field(default_factory=list)
    description: str = ""
    #: False when the field count has not been checked against a real client
    #: .dbc; the converter then presses for --template.
    verified: bool = False
    source: str = "<builtin>"

    @classmethod
    def from_dict(cls, payload: dict, source: str = "<memory>") -> "TableMapping":
        mapping = cls(
            table=payload["table"],
            target_field_count=int(payload.get("target_field_count", 0)),
            id_index=int(payload.get("id_index", 0)),
            description=payload.get("description", ""),
            verified=bool(payload.get("verified", False)),
            source=source,
        )
        seen: set[int] = set()
        for spec in payload.get("columns", []):
            index = int(spec["index"])
            if index in seen:
                raise ConversionError(
                    f"{source}: target field {index} is mapped twice")
            seen.add(index)
            kind = spec.get("type", "uint")
            if kind not in TYPES:
                raise ConversionError(
                    f"{source}: field {index} has unknown type {kind!r}; "
                    f"expected one of {', '.join(TYPES)}")
            raw_from = spec.get("from")
            if isinstance(raw_from, str):
                sources: tuple[str, ...] = (raw_from,)
            elif raw_from:
                sources = tuple(raw_from)
            else:
                sources = ()
            if not sources and "const" not in spec:
                raise ConversionError(
                    f"{source}: field {index} has neither 'from' nor 'const'")
            mapping.columns.append(ColumnMap(
                index=index, type=kind, source=sources,
                array_index=spec.get("array_index"),
                const=spec.get("const"),
                default=spec.get("default"),
                transform=spec.get("transform", ""),
                scale=spec.get("scale"),
                id_offset=bool(spec.get("id_offset", False)),
                note=spec.get("note", ""),
            ))
        if mapping.target_field_count:
            widest = max((c.index for c in mapping.columns), default=-1)
            if widest >= mapping.target_field_count:
                raise ConversionError(
                    f"{source}: field {widest} is mapped but the table only "
                    f"has {mapping.target_field_count} fields")
        return mapping

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "TableMapping":
        p = Path(path)
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")), str(p))


class MappingLibrary:
    """Built-in mappings, with a user directory taking precedence."""

    def __init__(self, user_dir: str | os.PathLike[str] | None = None):
        self.user_dir = Path(user_dir) if user_dir else None
        self._cache: dict[str, TableMapping | None] = {}

    def get(self, table: str) -> TableMapping | None:
        key = table.lower()
        if key in self._cache:
            return self._cache[key]
        result = None
        for directory in (self.user_dir, BUILTIN_DIR):
            if directory is None or not directory.is_dir():
                continue
            for candidate in directory.glob("*.json"):
                if candidate.stem.lower() == key:
                    result = TableMapping.load(candidate)
                    break
            if result is not None:
                break
        self._cache[key] = result
        return result

    def tables(self) -> list[str]:
        names: dict[str, str] = {}
        for directory in (BUILTIN_DIR, self.user_dir):
            if directory is None or not directory.is_dir():
                continue
            for candidate in sorted(directory.glob("*.json")):
                names[candidate.stem.lower()] = candidate.stem
        return sorted(names.values())


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------
class TransformContext:
    """What a transform needs beyond the value itself."""

    __slots__ = ("listfile", "path_prefix", "missing")

    def __init__(self, listfile: Listfile | None = None, path_prefix: str = ""):
        self.listfile = listfile or Listfile()
        self.path_prefix = path_prefix
        self.missing: set[int] = set()

    def path_for(self, file_id: int) -> str:
        path = self.listfile.path_for(int(file_id))
        if path is None:
            self.missing.add(int(file_id))
            return ""
        if self.path_prefix:
            path = normalise(self.path_prefix.rstrip("\\/") + "\\" + path)
        return path


def _t_path(value: Any, ctx: TransformContext) -> str:
    return ctx.path_for(value) if value else ""


def _t_model_path(value: Any, ctx: TransformContext) -> str:
    """FileDataID -> path with an ``.mdx`` extension.

    3.3.5a DBCs name models with the Warcraft III extension and the client
    swaps it for ``.m2`` when it opens the file, so a ``.m2`` path here simply
    does not load.
    """
    path = ctx.path_for(value) if value else ""
    return _strip_extension(path) + ".mdx" if path else ""


def _t_basename(value: Any, ctx: TransformContext) -> str:
    """FileDataID -> bare filename, no directory and no extension.

    Texture variation columns hold names the client resolves against the
    model's own directory.
    """
    path = ctx.path_for(value) if value else ""
    return _strip_extension(os.path.basename(path.replace("\\", "/"))) if path else ""


def _t_identity(value: Any, _ctx: TransformContext) -> Any:
    return value


TRANSFORMS = {
    "": _t_identity,
    "identity": _t_identity,
    "path": _t_path,
    "model_path": _t_model_path,
    "basename": _t_basename,
    "lower": lambda v, _c: str(v).lower(),
    "upper": lambda v, _c: str(v).upper(),
    "strip_extension": lambda v, _c: _strip_extension(str(v)),
}


# ---------------------------------------------------------------------------
# Applying a mapping
# ---------------------------------------------------------------------------
def _coerce(value: Any, kind: str) -> Any:
    if kind == "string":
        return "" if value is None else str(value)
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def apply_row(mapping: TableMapping, row: dict[str, Any],
              ctx: TransformContext, id_offset: int = 0
              ) -> dict[int, tuple[str, Any]]:
    """Turn one modern row into ``{target index: (type, value)}``."""
    out: dict[int, tuple[str, Any]] = {}
    for column in mapping.columns:
        if column.const is not None:
            value: Any = column.const
        else:
            value = None
            for name in column.source:
                if name in row:
                    value = row[name]
                    break
            if isinstance(value, list):
                index = column.array_index or 0
                value = value[index] if index < len(value) else None
            if value is None:
                value = column.default

        if value is None:
            value = "" if column.type == "string" else 0

        transform = TRANSFORMS.get(column.transform)
        if transform is None:
            raise ConversionError(
                f"{mapping.source}: unknown transform {column.transform!r}; "
                f"known transforms are {', '.join(sorted(TRANSFORMS))}")
        value = transform(value, ctx)

        if column.scale is not None and column.type in ("float", "int", "uint"):
            value = _coerce(value, column.type) * column.scale
        if column.id_offset and id_offset and column.type != "string":
            numeric = _coerce(value, column.type)
            if numeric:
                value = numeric + id_offset

        out[column.index] = (column.type, _coerce(value, column.type))
    return out


def missing_sources(mapping: TableMapping,
                    available: Iterable[str]) -> list[ColumnMap]:
    """Mapped columns whose source is absent from this build's table."""
    have = {name.lower() for name in available}
    out = []
    for column in mapping.columns:
        if column.const is not None or not column.source:
            continue
        if not any(name.lower() in have for name in column.source):
            out.append(column)
    return out


def require(library: MappingLibrary, table: str) -> TableMapping:
    mapping = library.get(table)
    if mapping is None:
        known = ", ".join(library.tables()) or "(none)"
        raise MissingDependencyError(
            f"no mapping for table {table!r}. Built-in mappings: {known}. "
            f"Write one as JSON and point --db-mappings at its directory")
    return mapping
