"""Parsing M2 models, from Wrath's flat MD20 through to modern chunked files.

Modern files (Legion 272 and everything after) wrap the old MD20 blob in an
``MD21`` chunk and move cross-references out of the blob entirely: textures,
skins, animations and physics are named by FileDataID in sibling chunks rather
than by path.  The parser normalises both shapes into one :class:`M2Model`.
"""

from __future__ import annotations

import dataclasses
import struct
from typing import Sequence

from ..chunks import Chunk, ChunkReader
from ..errors import MalformedFileError, UnsupportedFormatError
from ..limits import M2_GLOBAL_FLAG_USE_COMBINER_COMBOS, M2_MAGIC
from . import schemas
from .types import Schema, StructReader

#: Chunk names that can appear next to MD21 in a modern M2.
KNOWN_M2_CHUNKS = {
    "MD21", "PFID", "SFID", "AFID", "BFID", "TXAC", "EXPT", "EXP2", "PABC",
    "PADC", "PSBC", "PEDC", "SKID", "TXID", "LDV1", "RPID", "GPID", "WFV1",
    "WFV2", "WFV3", "PGD1", "PFDC", "EDGF", "NERF", "DETL", "DBOC", "AFRA",
    "DPIV", "PCOL", "AFSA", "AFSB", "PSBC1",
}

#: Byte offsets of every field in the MD20 header (identical for 264..274).
VERTEX_SIZE = 48

_H = {
    "name": 0x08,
    "global_flags": 0x10,
    "global_loops": 0x14,
    "sequences": 0x1C,
    "sequence_lookups": 0x24,
    "bones": 0x2C,
    "key_bone_lookup": 0x34,
    "vertices": 0x3C,
    "num_skin_profiles": 0x44,
    "colors": 0x48,
    "textures": 0x50,
    "texture_weights": 0x58,
    "texture_transforms": 0x60,
    "replacable_texture_lookup": 0x68,
    "materials": 0x70,
    "bone_combos": 0x78,
    "texture_combos": 0x80,
    "texture_coord_combos": 0x88,
    "texture_weight_combos": 0x90,
    "texture_transform_combos": 0x98,
    "bounding_box": 0xA0,
    "bounding_sphere_radius": 0xB8,
    "collision_box": 0xBC,
    "collision_sphere_radius": 0xD4,
    "collision_indices": 0xD8,
    "collision_positions": 0xE0,
    "collision_face_normals": 0xE8,
    "attachments": 0xF0,
    "attachment_lookup": 0xF8,
    "events": 0x100,
    "lights": 0x108,
    "cameras": 0x110,
    "camera_lookup": 0x118,
    "ribbons": 0x120,
    "particles": 0x128,
    "texture_combiner_combos": 0x130,
}
HEADER_SIZE_BASE = 0x130
HEADER_SIZE_WITH_COMBINERS = 0x138


@dataclasses.dataclass(slots=True)
class AnimFileRef:
    """One AFID entry: which sequence variant lives in which external file."""

    anim_id: int
    sub_anim_id: int
    file_id: int



@dataclasses.dataclass
class M2Model:
    """An M2 normalised into plain Python structures."""

    version: int = 264
    name: str = ""
    global_flags: int = 0
    global_loops: list[int] = dataclasses.field(default_factory=list)
    sequences: list[dict] = dataclasses.field(default_factory=list)
    sequence_lookups: list[int] = dataclasses.field(default_factory=list)
    bones: list[dict] = dataclasses.field(default_factory=list)
    key_bone_lookup: list[int] = dataclasses.field(default_factory=list)
    vertices: bytes = b""
    vertex_count: int = 0
    num_skin_profiles: int = 0
    colors: list[dict] = dataclasses.field(default_factory=list)
    textures: list[dict] = dataclasses.field(default_factory=list)
    texture_weights: list[dict] = dataclasses.field(default_factory=list)
    texture_transforms: list[dict] = dataclasses.field(default_factory=list)
    replacable_texture_lookup: list[int] = dataclasses.field(default_factory=list)
    materials: list[dict] = dataclasses.field(default_factory=list)
    bone_combos: list[int] = dataclasses.field(default_factory=list)
    texture_combos: list[int] = dataclasses.field(default_factory=list)
    texture_coord_combos: list[int] = dataclasses.field(default_factory=list)
    texture_weight_combos: list[int] = dataclasses.field(default_factory=list)
    texture_transform_combos: list[int] = dataclasses.field(default_factory=list)
    bounding_box: tuple = (0.0,) * 6
    bounding_sphere_radius: float = 0.0
    collision_box: tuple = (0.0,) * 6
    collision_sphere_radius: float = 0.0
    collision_indices: list[int] = dataclasses.field(default_factory=list)
    collision_positions: list[tuple] = dataclasses.field(default_factory=list)
    collision_face_normals: list[tuple] = dataclasses.field(default_factory=list)
    attachments: list[dict] = dataclasses.field(default_factory=list)
    attachment_lookup: list[int] = dataclasses.field(default_factory=list)
    events: list[dict] = dataclasses.field(default_factory=list)
    lights: list[dict] = dataclasses.field(default_factory=list)
    cameras: list[dict] = dataclasses.field(default_factory=list)
    camera_lookup: list[int] = dataclasses.field(default_factory=list)
    ribbons: list[dict] = dataclasses.field(default_factory=list)
    particles: list[dict] = dataclasses.field(default_factory=list)
    texture_combiner_combos: list[int] = dataclasses.field(default_factory=list)

    # -- which schema each variable-layout struct was read with ---------
    sequence_schema: Schema = schemas.SEQUENCE_264
    camera_schema: Schema = schemas.CAMERA_264
    particle_schema: Schema = schemas.PARTICLE_264

    # -- modern chunk payloads ------------------------------------------
    chunked: bool = False
    skin_file_ids: list[int] = dataclasses.field(default_factory=list)
    texture_file_ids: list[int] = dataclasses.field(default_factory=list)
    anim_file_ids: list[AnimFileRef] = dataclasses.field(default_factory=list)
    bone_file_ids: list[int] = dataclasses.field(default_factory=list)
    recursive_particle_file_ids: list[int] = dataclasses.field(default_factory=list)
    geometry_particle_file_ids: list[int] = dataclasses.field(default_factory=list)
    phys_file_id: int = 0
    skeleton_file_id: int = 0
    #: Chunks present in the source that this tool does not translate.
    extra_chunks: dict[str, int] = dataclasses.field(default_factory=dict)

    # -- convenience ----------------------------------------------------
    @property
    def uses_combiner_combos(self) -> bool:
        return bool(self.global_flags & M2_GLOBAL_FLAG_USE_COMBINER_COMBOS)

    @property
    def uses_external_skeleton(self) -> bool:
        return bool(self.skeleton_file_id) and not self.bones

    def describe(self) -> str:
        return (f"M2 v{self.version} '{self.name}' "
                f"{self.vertex_count} verts, {len(self.bones)} bones, "
                f"{len(self.sequences)} seqs, {len(self.textures)} textures")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _array_header(data: bytes, pos: int) -> tuple[int, int]:
    return struct.unpack_from("<II", data, pos)


def _read_structs(sr: StructReader, schema: Schema, data: bytes,
                  pos: int) -> list[dict]:
    count, offset = _array_header(data, pos)
    if count == 0:
        return []
    end = offset + schema.size * count
    if offset <= 0 or end > len(data):
        raise MalformedFileError(
            f"{sr.name}: {schema.name}[{count}] at {offset} runs past end of file "
            f"({len(data)} bytes)"
        )
    return [sr.read_struct(schema, offset + i * schema.size) for i in range(count)]


def _plausible_particle(p: dict) -> int:
    """Crude score for a candidate particle parse; higher is better."""
    score = 0
    if 0 <= p.get("blending_type", 99) <= 10:
        score += 2
    if 1 <= p.get("emitter_type", 99) <= 4:
        score += 3
    if 0 <= p.get("texture_dimensions_rows", 999) <= 64:
        score += 1
    if 0 <= p.get("texture_dimensions_columns", 999) <= 64:
        score += 1
    pos = p.get("position", (0.0, 0.0, 0.0))
    if all(abs(v) < 1e6 for v in pos):
        score += 1
    return score


def _read_variable_structs(sr: StructReader, data: bytes, pos: int,
                           candidates: Sequence[Schema]) -> tuple[list[dict], Schema]:
    """Try each candidate schema and keep the parse that looks sanest.

    Used for M2Particle and M2Camera, whose size changed without any in-file
    marker, so a mismatched header version would otherwise silently produce
    garbage emitters.
    """
    count, _offset = _array_header(data, pos)
    if count == 0:
        return [], candidates[0]

    best: tuple[int, list[dict], Schema] | None = None
    errors: list[str] = []
    for schema in candidates:
        try:
            items = _read_structs(sr, schema, data, pos)
        except (MalformedFileError, ValueError, struct.error) as exc:
            errors.append(f"{schema.name}: {exc}")
            continue
        if schema.name.startswith("M2Particle"):
            score = sum(_plausible_particle(p) for p in items)
        else:
            score = 1
        if best is None or score > best[0]:
            best = (score, items, schema)
        # A first candidate that parses perfectly needs no second opinion.
        if schema is candidates[0] and score >= 8 * len(items):
            break
    if best is None:
        raise MalformedFileError(
            f"{sr.name}: could not parse {candidates[0].name}[{count}]: "
            + "; ".join(errors)
        )
    return best[1], best[2]


# ---------------------------------------------------------------------------
# MD20 body
# ---------------------------------------------------------------------------
def parse_md20(data: bytes, name: str = "<m2>") -> M2Model:
    """Parse a flat MD20 blob (offsets relative to ``data[0]``)."""
    if len(data) < HEADER_SIZE_BASE:
        raise MalformedFileError(f"{name}: MD20 body is only {len(data)} bytes")
    magic = data[:4].decode("latin-1")
    if magic != M2_MAGIC:
        raise UnsupportedFormatError(f"{name}: expected MD20 magic, found {magic!r}")
    version = struct.unpack_from("<I", data, 4)[0]
    if version < 264:
        raise UnsupportedFormatError(
            f"{name}: M2 version {version} predates Wrath; this tool converts "
            f"modern assets down to 264, not old ones up"
        )

    sr = StructReader(data, name)
    m = M2Model(version=version)
    m.name = sr.read_string_array(_H["name"])
    m.global_flags = struct.unpack_from("<I", data, _H["global_flags"])[0]
    m.global_loops = sr.read_array("u32", _H["global_loops"])

    m.sequence_schema = schemas.sequence_schema(version)
    m.sequences = _read_structs(sr, m.sequence_schema, data, _H["sequences"])
    m.sequence_lookups = sr.read_array("u16", _H["sequence_lookups"])

    m.bones = _read_structs(sr, schemas.BONE, data, _H["bones"])
    m.key_bone_lookup = sr.read_array("u16", _H["key_bone_lookup"])

    vcount, voff = _array_header(data, _H["vertices"])
    if vcount:
        end = voff + vcount * VERTEX_SIZE
        if voff <= 0 or end > len(data):
            raise MalformedFileError(
                f"{name}: {vcount} vertices at {voff} run past end of file")
        m.vertices = data[voff:end]
    m.vertex_count = vcount

    m.num_skin_profiles = struct.unpack_from("<I", data, _H["num_skin_profiles"])[0]
    m.colors = _read_structs(sr, schemas.COLOR, data, _H["colors"])
    m.textures = _read_structs(sr, schemas.TEXTURE, data, _H["textures"])
    m.texture_weights = _read_structs(sr, schemas.TEXTURE_WEIGHT, data,
                                      _H["texture_weights"])
    m.texture_transforms = _read_structs(sr, schemas.TEXTURE_TRANSFORM, data,
                                         _H["texture_transforms"])
    m.replacable_texture_lookup = sr.read_array("u16", _H["replacable_texture_lookup"])
    m.materials = _read_structs(sr, schemas.MATERIAL, data, _H["materials"])

    for field in ("bone_combos", "texture_combos", "texture_coord_combos",
                  "texture_weight_combos", "texture_transform_combos"):
        setattr(m, field, sr.read_array("u16", _H[field]))

    m.bounding_box = struct.unpack_from("<6f", data, _H["bounding_box"])
    m.bounding_sphere_radius = struct.unpack_from("<f", data,
                                                  _H["bounding_sphere_radius"])[0]
    m.collision_box = struct.unpack_from("<6f", data, _H["collision_box"])
    m.collision_sphere_radius = struct.unpack_from("<f", data,
                                                   _H["collision_sphere_radius"])[0]
    m.collision_indices = sr.read_array("u16", _H["collision_indices"])
    m.collision_positions = sr.read_array("vec3", _H["collision_positions"])
    m.collision_face_normals = sr.read_array("vec3", _H["collision_face_normals"])

    m.attachments = _read_structs(sr, schemas.ATTACHMENT, data, _H["attachments"])
    m.attachment_lookup = sr.read_array("u16", _H["attachment_lookup"])
    m.events = _read_structs(sr, schemas.EVENT, data, _H["events"])
    m.lights = _read_structs(sr, schemas.LIGHT, data, _H["lights"])

    m.cameras, m.camera_schema = _read_variable_structs(
        sr, data, _H["cameras"], schemas.camera_candidates(version))
    m.camera_lookup = sr.read_array("u16", _H["camera_lookup"])
    m.ribbons = _read_structs(sr, schemas.RIBBON, data, _H["ribbons"])
    m.particles, m.particle_schema = _read_variable_structs(
        sr, data, _H["particles"], schemas.particle_candidates(version))

    if m.uses_combiner_combos and len(data) >= HEADER_SIZE_WITH_COMBINERS:
        m.texture_combiner_combos = sr.read_array("u16",
                                                  _H["texture_combiner_combos"])
    return m


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------
def _u32_list(chunk: Chunk) -> list[int]:
    n = len(chunk.data) // 4
    return list(struct.unpack_from("<" + "I" * n, chunk.data, 0)) if n else []


def parse_m2(data: bytes, name: str = "<m2>") -> M2Model:
    """Parse either a flat MD20 file or a modern MD21-chunked file."""
    if len(data) < 8:
        raise MalformedFileError(f"{name}: file is only {len(data)} bytes")

    head = data[:4].decode("latin-1")
    if head == M2_MAGIC:
        model = parse_md20(data, name)
        model.chunked = False
        return model

    reader = ChunkReader.auto(data, KNOWN_M2_CHUNKS, name=name)
    chunks: dict[str, list[Chunk]] = {}
    for chunk in reader:
        chunks.setdefault(chunk.name, []).append(chunk)

    md21 = chunks.get("MD21")
    if not md21:
        raise UnsupportedFormatError(
            f"{name}: not an M2 -- no MD20 magic and no MD21 chunk "
            f"(first chunk was {head!r})"
        )

    model = parse_md20(md21[0].data, name)
    model.chunked = True

    for cname, entries in chunks.items():
        chunk = entries[0]
        if cname == "MD21":
            continue
        if cname == "SFID":
            ids = _u32_list(chunk)
            n = model.num_skin_profiles or len(ids)
            model.skin_file_ids = ids[:n]
            # Anything past num_skin_profiles is the Legion LOD skin block.
            model.extra_chunks.setdefault("SFID.lod", len(ids) - len(model.skin_file_ids))
        elif cname == "TXID":
            model.texture_file_ids = _u32_list(chunk)
        elif cname == "AFID":
            n = len(chunk.data) // 8
            model.anim_file_ids = [
                AnimFileRef(*struct.unpack_from("<HHI", chunk.data, i * 8))
                for i in range(n)
            ]
        elif cname == "BFID":
            model.bone_file_ids = _u32_list(chunk)
        elif cname == "PFID":
            ids = _u32_list(chunk)
            model.phys_file_id = ids[0] if ids else 0
        elif cname == "SKID":
            ids = _u32_list(chunk)
            model.skeleton_file_id = ids[0] if ids else 0
        elif cname == "RPID":
            model.recursive_particle_file_ids = _u32_list(chunk)
        elif cname == "GPID":
            model.geometry_particle_file_ids = _u32_list(chunk)
        else:
            model.extra_chunks[cname] = len(chunk.data)

    return model
