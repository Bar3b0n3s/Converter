"""Schema-driven codec for the structs inside an M2.

Most M2 sub-structures (bones, colours, lights, ribbons, attachments, events,
texture transforms) are byte-identical between version 264 and version 274 --
what changed between Wrath and Legion is the *container*, not these.  Describing
them as field schemas rather than hand-written read/write pairs means one
generic walker handles both directions, and the handful of structs that really
did change (M2Sequence, M2Particle, M2Camera) only need a second schema plus an
explicit field mapping.

Field kinds
-----------
``('p', fmt)``       a struct.Struct format, read as a tuple (scalars unwrapped)
``('arr', kind)``    ``M2Array<kind>``; ``kind='char'`` yields a Python ``str``
``('trk', kind)``    ``M2Track<kind>`` -- per-sequence timestamp/value sub-arrays
``('trkb',)``        ``M2TrackBase`` -- timestamps only, used by M2Event
``('ptrk', kind)``   ``M2PartTrack<kind>`` -- particle FBlock (times + values)
"""

from __future__ import annotations

import dataclasses
import struct
from typing import Any, Iterable, Sequence

from ..binio import Writer

# ---------------------------------------------------------------------------
# Primitive value kinds that can appear inside an M2Array / M2Track
# ---------------------------------------------------------------------------
VALUE_FORMATS: dict[str, str] = {
    "u8": "B",
    "i8": "b",
    "u16": "H",
    "i16": "h",
    "u32": "I",
    "i32": "i",
    "f32": "f",
    "fixed16": "h",          # 16-bit fixed point, kept raw
    "vec2": "ff",
    "vec3": "fff",
    "vec4": "ffff",
    "quat": "ffff",          # C4Quaternion
    "quat16": "hhhh",        # M2CompQuat
    "splinef32": "fff",      # M2SplineKey<float>: value, in-tan, out-tan
    "splinevec3": "fffffffff",  # M2SplineKey<C3Vector>
    "u16x2": "HH",
    "u8x4": "BBBB",
}

#: Sizes of the container types themselves.
M2ARRAY_SIZE = 8
M2TRACK_SIZE = 20
M2TRACKBASE_SIZE = 12
M2PARTTRACK_SIZE = 16

_STRUCTS: dict[str, struct.Struct] = {}


def _fmt(kind: str) -> struct.Struct:
    s = _STRUCTS.get(kind)
    if s is None:
        try:
            s = struct.Struct("<" + VALUE_FORMATS[kind])
        except KeyError:
            raise KeyError(f"unknown M2 value kind {kind!r}") from None
        _STRUCTS[kind] = s
    return s


def value_size(kind: str) -> int:
    return _fmt(kind).size


def _is_scalar(kind: str) -> bool:
    return len(VALUE_FORMATS[kind]) == 1


# ---------------------------------------------------------------------------
# Track containers
# ---------------------------------------------------------------------------
@dataclasses.dataclass(slots=True)
class Track:
    """``M2Track<T>`` -- one timestamp/value pair of sub-arrays per sequence."""

    kind: str = "f32"
    interpolation: int = 0
    global_sequence: int = -1
    timestamps: list[list[int]] = dataclasses.field(default_factory=list)
    values: list[list[Any]] = dataclasses.field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(self.values)

    def key_count(self) -> int:
        return sum(len(v) for v in self.values)

    def first_value(self) -> Any | None:
        for seq in self.values:
            if seq:
                return seq[0]
        return None

    def trim_to(self, sequence_count: int) -> None:
        """Clamp the per-sequence sub-arrays to ``sequence_count`` entries."""
        del self.timestamps[sequence_count:]
        del self.values[sequence_count:]
        while len(self.timestamps) < len(self.values):
            self.timestamps.append([])
        while len(self.values) < len(self.timestamps):
            self.values.append([])


@dataclasses.dataclass(slots=True)
class TrackBase:
    """``M2TrackBase`` -- timestamps with no values (M2Event)."""

    interpolation: int = 0
    global_sequence: int = -1
    timestamps: list[list[int]] = dataclasses.field(default_factory=list)

    def trim_to(self, sequence_count: int) -> None:
        del self.timestamps[sequence_count:]


@dataclasses.dataclass(slots=True)
class PartTrack:
    """``M2PartTrack<T>`` -- the flat FBlock used by particle emitters."""

    kind: str = "f32"
    times: list[int] = dataclasses.field(default_factory=list)
    values: list[Any] = dataclasses.field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
class StructReader:
    """Reads schema-described structs out of a flat M2 buffer."""

    def __init__(self, data: bytes, name: str = "<m2>"):
        self.data = data
        self.name = name

    # -- low level ------------------------------------------------------
    def array_header(self, pos: int) -> tuple[int, int]:
        count, offset = struct.unpack_from("<II", self.data, pos)
        return count, offset

    def read_values(self, kind: str, count: int, offset: int) -> list[Any]:
        if count == 0:
            return []
        s = _fmt(kind)
        end = offset + s.size * count
        if offset <= 0 or end > len(self.data):
            # Blizzard leaves stale offsets on zero-length arrays; a non-zero
            # count pointing outside the file is a genuinely broken model.
            raise ValueError(
                f"{self.name}: array of {count} {kind} at {offset} runs past "
                f"end of file ({len(self.data)})"
            )
        if _is_scalar(kind):
            fmt = struct.Struct("<" + VALUE_FORMATS[kind] * count)
            return list(fmt.unpack_from(self.data, offset))
        return [s.unpack_from(self.data, offset + i * s.size) for i in range(count)]

    def read_array(self, kind: str, pos: int) -> list[Any]:
        count, offset = self.array_header(pos)
        if kind == "char":
            if count == 0:
                return []
            raw = self.data[offset : offset + count]
            return [raw.split(b"\0", 1)[0].decode("latin-1")]
        return self.read_values(kind, count, offset)

    def read_string_array(self, pos: int) -> str:
        count, offset = self.array_header(pos)
        if count == 0 or offset == 0:
            return ""
        raw = self.data[offset : offset + count]
        return raw.split(b"\0", 1)[0].decode("latin-1")

    def read_track(self, kind: str, pos: int) -> Track:
        interp, gseq = struct.unpack_from("<hh", self.data, pos)
        t = Track(kind=kind, interpolation=interp, global_sequence=gseq)
        tcount, toff = self.array_header(pos + 4)
        vcount, voff = self.array_header(pos + 12)
        for i in range(tcount):
            c, o = self.array_header(toff + i * M2ARRAY_SIZE)
            t.timestamps.append(self.read_values("u32", c, o))
        for i in range(vcount):
            c, o = self.array_header(voff + i * M2ARRAY_SIZE)
            t.values.append(self.read_values(kind, c, o))
        # Some tools emit mismatched counts; pad so the two stay in lockstep.
        while len(t.timestamps) < len(t.values):
            t.timestamps.append([])
        while len(t.values) < len(t.timestamps):
            t.values.append([])
        return t

    def read_trackbase(self, pos: int) -> TrackBase:
        interp, gseq = struct.unpack_from("<hh", self.data, pos)
        t = TrackBase(interpolation=interp, global_sequence=gseq)
        tcount, toff = self.array_header(pos + 4)
        for i in range(tcount):
            c, o = self.array_header(toff + i * M2ARRAY_SIZE)
            t.timestamps.append(self.read_values("u32", c, o))
        return t

    def read_parttrack(self, kind: str, pos: int) -> PartTrack:
        p = PartTrack(kind=kind)
        c, o = self.array_header(pos)
        p.times = self.read_values("u16", c, o)
        c, o = self.array_header(pos + M2ARRAY_SIZE)
        p.values = self.read_values(kind, c, o)
        return p

    # -- schema ---------------------------------------------------------
    def read_struct(self, schema: "Schema", pos: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, kind, offset in schema.layout:
            p = pos + offset
            tag = kind[0]
            if tag == "p":
                vals = struct.unpack_from("<" + kind[1], self.data, p)
                out[name] = vals[0] if len(vals) == 1 else vals
            elif tag == "arr":
                out[name] = (self.read_string_array(p) if kind[1] == "char"
                             else self.read_array(kind[1], p))
            elif tag == "trk":
                out[name] = self.read_track(kind[1], p)
            elif tag == "trkb":
                out[name] = self.read_trackbase(p)
            elif tag == "ptrk":
                out[name] = self.read_parttrack(kind[1], p)
            else:  # pragma: no cover - schemas are internal
                raise AssertionError(f"bad field kind {kind!r}")
        return out


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
class DeferredWriter(Writer):
    """A :class:`~wotlkconv.binio.Writer` that resolves M2Array back-patches.

    Structs are emitted with zeroed ``(count, offset)`` pairs and their payloads
    queued; :meth:`flush` then appends each payload and patches the pair.  The
    queue is processed FIFO, so payloads that themselves contain arrays (tracks
    inside bones, for instance) simply enqueue more work and still resolve.
    """

    __slots__ = ("_queue", "alignment")

    def __init__(self, alignment: int = 4):
        super().__init__()
        self._queue: list[tuple[int, int, Any]] = []
        self.alignment = alignment

    def reserve_array(self) -> int:
        return self.reserve(M2ARRAY_SIZE)

    def defer(self, pos: int, count: int, emit) -> None:
        """Queue ``emit(writer)`` and patch the M2Array at ``pos`` when it runs."""
        if count == 0:
            # Blizzard writes (0, 0) for empty arrays and the client accepts it.
            self.patch_u32(pos, 0)
            self.patch_u32(pos + 4, 0)
            return
        self._queue.append((pos, count, emit))

    def write_array(self, pos: int, kind: str, values: Sequence[Any]) -> None:
        self.defer(pos, len(values), lambda w: w.write_values(kind, values))

    def write_values(self, kind: str, values: Sequence[Any]) -> None:
        if not values:
            return
        if _is_scalar(kind):
            self.array(VALUE_FORMATS[kind], list(values))
        else:
            s = _fmt(kind)
            for v in values:
                self.raw(s.pack(*v))

    def write_string_array(self, pos: int, text: str) -> None:
        if not text:
            self.patch_u32(pos, 0)
            self.patch_u32(pos + 4, 0)
            return
        payload = text.encode("latin-1") + b"\0"
        self.defer(pos, len(payload), lambda w: w.raw(payload))

    def write_track(self, pos: int, track: Track) -> None:
        self.patch_u16(pos, track.interpolation & 0xFFFF)
        self.patch_u16(pos + 2, track.global_sequence & 0xFFFF)
        stamps = track.timestamps
        values = track.values

        def emit_stamps(w: "DeferredWriter") -> None:
            heads = [w.reserve_array() for _ in stamps]
            for head, seq in zip(heads, stamps):
                w.write_array(head, "u32", seq)

        def emit_values(w: "DeferredWriter") -> None:
            heads = [w.reserve_array() for _ in values]
            for head, seq in zip(heads, values):
                w.write_array(head, track.kind, seq)

        self.defer(pos + 4, len(stamps), emit_stamps)
        self.defer(pos + 12, len(values), emit_values)

    def write_trackbase(self, pos: int, track: TrackBase) -> None:
        self.patch_u16(pos, track.interpolation & 0xFFFF)
        self.patch_u16(pos + 2, track.global_sequence & 0xFFFF)
        stamps = track.timestamps

        def emit_stamps(w: "DeferredWriter") -> None:
            heads = [w.reserve_array() for _ in stamps]
            for head, seq in zip(heads, stamps):
                w.write_array(head, "u32", seq)

        self.defer(pos + 4, len(stamps), emit_stamps)

    def write_parttrack(self, pos: int, track: PartTrack) -> None:
        self.write_array(pos, "u16", track.times)
        self.write_array(pos + M2ARRAY_SIZE, track.kind, track.values)

    def write_struct(self, schema: "Schema", pos: int, values: dict[str, Any]) -> None:
        for name, kind, offset in schema.layout:
            p = pos + offset
            tag = kind[0]
            value = values.get(name)
            if tag == "p":
                fmt = kind[1]
                if value is None:
                    continue
                # A tuple/list means a multi-component field (vec3, quat...);
                # anything else -- including the bytes of a 4CC -- is one value.
                if isinstance(value, (tuple, list)):
                    self.patch_bytes(p, struct.pack("<" + fmt, *value))
                else:
                    self.patch_bytes(p, struct.pack("<" + fmt, value))
            elif tag == "arr":
                if kind[1] == "char":
                    self.write_string_array(p, value or "")
                else:
                    self.write_array(p, kind[1], value or [])
            elif tag == "trk":
                self.write_track(p, value if value is not None else Track(kind[1]))
            elif tag == "trkb":
                self.write_trackbase(p, value if value is not None else TrackBase())
            elif tag == "ptrk":
                self.write_parttrack(p, value if value is not None else PartTrack(kind[1]))

    def emit_struct(self, schema: "Schema", values: dict[str, Any]) -> int:
        """Append one struct's fixed part, patch it in place, return its offset."""
        pos = self.reserve(schema.size)
        self.write_struct(schema, pos, values)
        return pos

    def flush(self) -> None:
        """Drain the deferred queue until no more payloads are pending."""
        while self._queue:
            pos, count, emit = self._queue.pop(0)
            self.align(self.alignment)
            offset = self.tell()
            emit(self)
            self.patch_u32(pos, count)
            self.patch_u32(pos + 4, offset)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
def kind_size(kind: tuple) -> int:
    tag = kind[0]
    if tag == "p":
        return struct.calcsize("<" + kind[1])
    if tag == "arr":
        return M2ARRAY_SIZE
    if tag == "trk":
        return M2TRACK_SIZE
    if tag == "trkb":
        return M2TRACKBASE_SIZE
    if tag == "ptrk":
        return M2PARTTRACK_SIZE
    raise AssertionError(f"bad field kind {kind!r}")


class Schema:
    """An ordered list of named fields with computed offsets."""

    __slots__ = ("name", "fields", "layout", "size")

    def __init__(self, name: str, fields: Iterable[tuple[str, tuple]]):
        self.name = name
        self.fields = list(fields)
        self.layout: list[tuple[str, tuple, int]] = []
        offset = 0
        for fname, kind in self.fields:
            self.layout.append((fname, kind, offset))
            offset += kind_size(kind)
        self.size = offset

    def defaults(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, kind, _ in self.layout:
            tag = kind[0]
            if tag == "p":
                fmt = kind[1]
                zero = struct.unpack("<" + fmt, b"\0" * struct.calcsize("<" + fmt))
                out[name] = zero[0] if len(zero) == 1 else zero
            elif tag == "arr":
                out[name] = "" if kind[1] == "char" else []
            elif tag == "trk":
                out[name] = Track(kind[1])
            elif tag == "trkb":
                out[name] = TrackBase()
            elif tag == "ptrk":
                out[name] = PartTrack(kind[1])
        return out

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Schema {self.name} size={self.size} fields={len(self.fields)}>"
