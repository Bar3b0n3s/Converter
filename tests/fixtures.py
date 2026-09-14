"""Synthetic modern-format assets for the test suite.

Shipping real Blizzard data is not an option, so the tests build files that
match the modern wire formats byte for byte and push them through the
converter.  The M2 fixtures deliberately reuse the production writer (with
version-272 schemas) so a change to the serialiser cannot quietly diverge from
what the parser expects.
"""

from __future__ import annotations

import struct

from wotlkconv.chunks import ChunkWriter
from wotlkconv.blp import bcn
from wotlkconv.blp.blp import Blp, PreferredFormat
from wotlkconv.blp.image import Image
from wotlkconv.m2 import schemas
from wotlkconv.m2.model import M2Model
from wotlkconv.m2.types import DeferredWriter, PartTrack, Track
from wotlkconv.m2.write import write_md20
from wotlkconv.limits import SKIN_MAGIC

# M2 chunk magics are stored in reading order, unlike ADT/WMO.
M2_CHUNKS_FORWARD = False


def make_track(kind: str, sequences: int, keys_per_sequence: int = 2,
               value=None) -> Track:
    t = Track(kind=kind, interpolation=1, global_sequence=-1)
    for s in range(sequences):
        t.timestamps.append([i * 100 for i in range(keys_per_sequence)])
        vals = []
        for i in range(keys_per_sequence):
            if value is not None:
                vals.append(value)
            elif kind == "vec3":
                vals.append((float(i), float(s), 1.0))
            elif kind == "quat16":
                vals.append((0, 0, 0, 32767))
            elif kind == "quat":
                vals.append((0.0, 0.0, 0.0, 1.0))
            elif kind == "f32":
                vals.append(float(i))
            elif kind in ("u8",):
                vals.append(i & 0xFF)
            elif kind in ("u16",):
                vals.append(i)
            elif kind == "fixed16":
                vals.append(32767)
            elif kind == "vec2":
                vals.append((float(i), float(i)))
            elif kind == "splinef32":
                vals.append((0.8 + 0.1 * i, 0.0, 0.0))
            elif kind == "splinevec3":
                vals.append(tuple(float(i)) * 0 + (0.0,) * 9)
            else:
                raise AssertionError(kind)
        t.values.append(vals)
    return t


def external_track(kind: str, sequences: int, index: int, offset: int,
                   keys: int = 2) -> Track:
    """A track whose sequence ``index`` points into a sibling .anim."""
    t = make_track(kind, sequences, keys)
    t.timestamps[index] = []
    t.values[index] = []
    t.timestamp_spans = [(0, 0)] * sequences
    t.value_spans = [(0, 0)] * sequences
    t.timestamp_spans[index] = (keys, offset)
    t.value_spans[index] = (keys, offset + keys * 4)
    t.external = {index}
    return t


def build_modern_model(*, sequences: int = 2, bones: int = 3, vertices: int = 6,
                       textures: int = 2, particles: int = 1, cameras: int = 1,
                       ribbons: int = 1, lights: int = 1,
                       external_sequence: int | None = None,
                       version: int = 272) -> M2Model:
    """A small but structurally complete Legion-era model."""
    m = M2Model(version=version)
    m.name = "TestModel"
    m.global_flags = 0x08 | 0x80  # combiner combos + a post-Wrath bit
    m.global_loops = [1000, 2000]

    seq_schema = schemas.sequence_schema(version)
    m.sequence_schema = seq_schema
    for i in range(sequences):
        s = seq_schema.defaults()
        # A sequence with none of the 0x130 bits keeps its keys in an .anim.
        flags = 0x00 if i == external_sequence else 0x20
        s.update(id=i, variation_index=0, duration=1000 + i, movespeed=1.5,
                 flags=flags, frequency=32767, replay_min=0, replay_max=0,
                 bounds_min=(-1.0, -1.0, -1.0), bounds_max=(1.0, 1.0, 1.0),
                 bounds_radius=1.732, variation_next=-1, alias_next=0)
        if version >= 272:
            s["blend_time_in"] = 150
            s["blend_time_out"] = 250
        else:
            s["blend_time"] = 150
        m.sequences.append(s)
    m.sequence_lookups = [0] * max(1, sequences)

    for i in range(bones):
        b = schemas.BONE.defaults()
        b.update(key_bone_id=-1 if i else 0,
                 # 0x400 is a Cataclysm kinematic-bone flag the converter clears.
                 flags=0x200 | (0x400 if i == 1 else 0),
                 parent_bone=i - 1, submesh_id=0, bone_name_crc=0xDEADBEEF,
                 translation=make_track("vec3", sequences),
                 rotation=make_track("quat16", sequences),
                 scale=make_track("vec3", sequences, value=(1.0, 1.0, 1.0)),
                 pivot=(0.0, 0.0, float(i)))
        m.bones.append(b)
    m.key_bone_lookup = [0] + [0xFFFF] * 26

    vbuf = bytearray()
    for i in range(vertices):
        vbuf += struct.pack("<3f4B4B3f4f",
                            float(i), 0.0, 0.0,
                            255, 0, 0, 0,
                            max(0, min(i, bones - 1)), 0, 0, 0,
                            0.0, 0.0, 1.0,
                            i / vertices, 0.0, 0.0, 0.0)
    m.vertices = bytes(vbuf)
    m.vertex_count = vertices
    m.num_skin_profiles = 4

    for i in range(textures):
        t = schemas.TEXTURE.defaults()
        # Legion leaves the filename empty and names the texture in TXID.
        t.update(type=0, flags=3, filename="")
        m.textures.append(t)
    m.texture_file_ids = [900000 + i for i in range(textures)]

    for blend in (0, 7):  # 7 = Legion's BlendAdd, absent from 3.3.5a
        mat = schemas.MATERIAL.defaults()
        mat.update(flags=0x04 | 0x800, blending_mode=blend)
        m.materials.append(mat)

    c = schemas.COLOR.defaults()
    c.update(color=make_track("vec3", sequences), alpha=make_track("fixed16", sequences))
    m.colors.append(c)

    tw = schemas.TEXTURE_WEIGHT.defaults()
    tw.update(weight=make_track("fixed16", sequences))
    m.texture_weights.append(tw)

    tt = schemas.TEXTURE_TRANSFORM.defaults()
    tt.update(translation=make_track("vec3", sequences),
              rotation=make_track("quat", sequences),
              scaling=make_track("vec3", sequences, value=(1.0, 1.0, 1.0)))
    m.texture_transforms.append(tt)

    m.bone_combos = list(range(bones))
    m.texture_combos = list(range(textures))
    m.texture_coord_combos = [0, 1]
    m.texture_weight_combos = [0]
    m.texture_transform_combos = [0xFFFF]
    m.texture_combiner_combos = [0, 1]
    m.replacable_texture_lookup = [0xFFFF]

    m.bounding_box = (-1.0, -1.0, -1.0, 1.0, 1.0, 1.0)
    m.bounding_sphere_radius = 1.732
    m.collision_box = (-1.0, -1.0, -1.0, 1.0, 1.0, 1.0)
    m.collision_sphere_radius = 1.732
    m.collision_indices = [0, 1, 2]
    m.collision_positions = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    m.collision_face_normals = [(0.0, 0.0, 1.0)]

    att = schemas.ATTACHMENT.defaults()
    att.update(id=0, bone=0, position=(0.0, 0.0, 1.0),
               animate_attached=make_track("u8", sequences))
    m.attachments.append(att)
    m.attachment_lookup = [0] + [0xFFFF] * 4

    ev = schemas.EVENT.defaults()
    ev.update(identifier=b"$AH0", data=0, bone=0, position=(0.0, 0.0, 0.0))
    ev["enabled"].timestamps = [[0] for _ in range(sequences)]
    m.events.append(ev)

    for i in range(lights):
        li = schemas.LIGHT.defaults()
        li.update(type=1, bone=0, position=(0.0, 0.0, 1.0),
                  ambient_color=make_track("vec3", sequences),
                  ambient_intensity=make_track("f32", sequences),
                  diffuse_color=make_track("vec3", sequences),
                  diffuse_intensity=make_track("f32", sequences),
                  attenuation_start=make_track("f32", sequences),
                  attenuation_end=make_track("f32", sequences),
                  visibility=make_track("u8", sequences))
        m.lights.append(li)

    cam_schema = schemas.camera_candidates(version)[0]
    m.camera_schema = cam_schema
    for _ in range(cameras):
        cam = cam_schema.defaults()
        cam.update(type=0, far_clip=100.0, near_clip=0.1,
                   position_base=(0.0, -5.0, 2.0),
                   target_position_base=(0.0, 0.0, 1.0))
        if "fov_track" in cam:
            cam["fov_track"] = make_track("splinef32", sequences)
        else:
            cam["fov"] = 0.97
        m.cameras.append(cam)
    m.camera_lookup = [0, 0xFFFF]

    for i in range(ribbons):
        rib = schemas.RIBBON.defaults()
        rib.update(ribbon_id=i, bone_index=0, position=(0.0, 0.0, 0.0),
                   texture_indices=[0], material_indices=[0],
                   color_track=make_track("vec3", sequences),
                   alpha_track=make_track("fixed16", sequences),
                   height_above_track=make_track("f32", sequences),
                   height_below_track=make_track("f32", sequences),
                   edges_per_second=30.0, edge_lifetime=0.5, gravity=0.0,
                   texture_rows=1, texture_cols=1,
                   tex_slot_track=make_track("u16", sequences),
                   visibility_track=make_track("u8", sequences),
                   priority_plane=0,
                   ribbon_color_index=2, texture_transform_lookup_index=1)
        m.ribbons.append(rib)

    part_schema = schemas.particle_candidates(version)[0]
    m.particle_schema = part_schema
    for i in range(particles):
        p = part_schema.defaults()
        p.update(particle_id=i,
                 # 0x10000000 is the Cataclysm multi-texture emitter flag.
                 flags=0x10000000 | 0x1,
                 position=(0.0, 0.0, 1.0), bone=0,
                 texture=(3 | (4 << 5) | (5 << 10)),
                 blending_type=4,
                 emitter_type=4,           # bone emitter: Legion only
                 particle_color_index=0,
                 texture_dimensions_rows=1, texture_dimensions_columns=1,
                 emission_speed=make_track("f32", sequences),
                 lifespan=make_track("f32", sequences),
                 emission_rate=make_track("f32", sequences),
                 enabled_in=make_track("u8", sequences))
        p["color_track"] = PartTrack("vec3", [0, 32767],
                                     [(1.0, 1.0, 1.0), (1.0, 0.0, 0.0)])
        p["alpha_track"] = PartTrack("fixed16", [0, 32767], [32767, 0])
        p["scale_track"] = PartTrack("vec2", [0, 32767], [(1.0, 1.0), (2.0, 2.0)])
        p["spline_points"] = [(0.0, 0.0, 0.0)]
        if "multi_texture_param_x" in p:
            p["multi_texture_param_x"] = (1, 2)
            p["multi_texture_param0"] = (1, 2, 3, 4)
            p["multi_texture_param1"] = (5, 6, 7, 8)
        m.particles.append(p)

    return m


def serialise_modern_m2(model: M2Model, *, chunked: bool = True,
                        skin_ids=(910000, 910001, 910002, 910003),
                        anim_ids=((0, 0, 920000), (1, 0, 920001)),
                        skeleton_id: int = 0,
                        phys_id: int = 930000,
                        extra_chunks=(("PABC", b"\0" * 8), ("LDV1", b"\0" * 16)),
                        ) -> bytes:
    """Write a model as a Legion-style chunked .m2 (or a flat MD20)."""
    body = write_md20(model, version=model.version,
                      sequence_schema=model.sequence_schema,
                      camera_schema=model.camera_schema,
                      particle_schema=model.particle_schema)
    if not chunked:
        return body

    cw = ChunkWriter(reverse=M2_CHUNKS_FORWARD)
    cw.add("MD21", body)
    if phys_id:
        cw.add("PFID", struct.pack("<I", phys_id))
    if skin_ids:
        cw.add("SFID", struct.pack("<" + "I" * len(skin_ids), *skin_ids))
    if model.texture_file_ids:
        cw.add("TXID", struct.pack("<" + "I" * len(model.texture_file_ids),
                                   *model.texture_file_ids))
    if anim_ids:
        payload = b"".join(struct.pack("<HHI", a, s, f) for a, s, f in anim_ids)
        cw.add("AFID", payload)
    if skeleton_id:
        cw.add("SKID", struct.pack("<I", skeleton_id))
    for name, payload in extra_chunks:
        cw.add(name, payload)
    return cw.getvalue()


# ---------------------------------------------------------------------------
# SKIN
# ---------------------------------------------------------------------------
def build_skin(*, vertices: int = 6, triangles: int = 2, submeshes: int = 1,
               batches: int = 1, legion: bool = True,
               shadow_batches: int = 2, texture_count: int = 4,
               shader_id: int = 0x8001) -> bytes:
    """A .skin with either the 48-byte Wrath or 56-byte Legion header."""
    header_size = 56 if legion else 48
    vert_list = list(range(vertices))
    index_list = [i % vertices for i in range(triangles * 3)]
    bone_table = bytes(bytearray([0, 0, 0, 0] * vertices))

    submesh_blobs = []
    for i in range(submeshes):
        submesh_blobs.append(struct.pack(
            "<10H3f3ff", i, 0, 0, vertices, 0, triangles * 3, 4, 0, 4, 0,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0))
    batch_blobs = []
    for i in range(batches):
        batch_blobs.append(struct.pack(
            "<BbHHHHHHHHHHH", 0, 0, shader_id, 0, 0, 0, 0, 0,
            texture_count, 0, 0, 0, 0))

    payload = bytearray()
    offsets = {}

    def place(name: str, blob: bytes) -> None:
        while len(payload) % 4:
            payload.append(0)
        offsets[name] = header_size + len(payload)
        payload.extend(blob)

    place("vertices", struct.pack("<" + "H" * len(vert_list), *vert_list))
    place("indices", struct.pack("<" + "H" * len(index_list), *index_list))
    place("bones", bone_table)
    place("submeshes", b"".join(submesh_blobs))
    place("batches", b"".join(batch_blobs))
    if legion and shadow_batches:
        place("shadow", bytes(shadow_batches * 12))

    head = bytearray()
    head += SKIN_MAGIC.encode("latin-1")
    head += struct.pack("<II", len(vert_list), offsets["vertices"])
    head += struct.pack("<II", len(index_list), offsets["indices"])
    head += struct.pack("<II", vertices, offsets["bones"])
    head += struct.pack("<II", len(submesh_blobs), offsets["submeshes"])
    head += struct.pack("<II", len(batch_blobs), offsets["batches"])
    head += struct.pack("<I", 4)
    if legion:
        head += struct.pack("<II", shadow_batches if shadow_batches else 0,
                            offsets.get("shadow", 0))
    assert len(head) == header_size, (len(head), header_size)
    return bytes(head) + bytes(payload)


# ---------------------------------------------------------------------------
# ANIM
# ---------------------------------------------------------------------------
def build_anim(payload: bytes = b"\x01\x02\x03\x04" * 8, *,
               chunked: bool = True) -> bytes:
    if not chunked:
        return payload
    cw = ChunkWriter(reverse=M2_CHUNKS_FORWARD)
    cw.add("AFM2", payload)
    cw.add("AFSA", b"\0" * 16)
    cw.add("AFSB", b"\0" * 8)
    return cw.getvalue()


# ---------------------------------------------------------------------------
# SKEL
# ---------------------------------------------------------------------------
def build_skel(*, bones: int = 3, sequences: int = 2, attachments: int = 1,
               parent_id: int = 0, name: str = "TestSkeleton") -> bytes:
    """A Legion .skel carrying bones, sequences and attachments."""
    def section(fields) -> bytes:
        """Write ``fields`` as leading M2Arrays with payloads after them."""
        w = DeferredWriter(alignment=4)
        heads = [w.reserve_array() for _ in fields]
        for head, (kind, payload) in zip(heads, fields):
            if kind == "structs":
                schema, items = payload

                def emit(writer, schema=schema, items=items):
                    for item in items:
                        writer.emit_struct(schema, item)

                w.defer(head, len(items), emit)
            else:
                w.write_array(head, kind, payload)
        w.flush()
        return w.getvalue()

    bone_items = []
    for i in range(bones):
        b = schemas.BONE.defaults()
        b.update(key_bone_id=-1 if i else 0, flags=0x200, parent_bone=i - 1,
                 translation=make_track("vec3", sequences),
                 rotation=make_track("quat16", sequences),
                 scale=make_track("vec3", sequences, value=(1.0, 1.0, 1.0)),
                 pivot=(0.0, 0.0, float(i)))
        bone_items.append(b)

    seq_items = []
    for i in range(sequences):
        s = schemas.SEQUENCE_272.defaults()
        s.update(id=i, duration=500 + i, movespeed=1.0, flags=0x20,
                 frequency=32767, blend_time_in=100, blend_time_out=200,
                 bounds_min=(-1.0,) * 3, bounds_max=(1.0,) * 3,
                 bounds_radius=1.7, variation_next=-1)
        seq_items.append(s)

    att_items = []
    for i in range(attachments):
        a = schemas.ATTACHMENT.defaults()
        a.update(id=i, bone=0, position=(0.0, 0.0, 1.0),
                 animate_attached=make_track("u8", sequences))
        att_items.append(a)

    skl1 = DeferredWriter(alignment=4)
    skl1.u32(0)
    name_pos = skl1.reserve_array()
    skl1.zeros(4)
    skl1.write_string_array(name_pos, name)
    skl1.flush()

    cw = ChunkWriter(reverse=M2_CHUNKS_FORWARD)
    cw.add("SKL1", skl1.getvalue())
    cw.add("SKA1", section([("structs", (schemas.ATTACHMENT, att_items)),
                            ("u16", [0] * 5)]))
    cw.add("SKB1", section([("structs", (schemas.BONE, bone_items)),
                            ("u16", [0] * 27)]))
    cw.add("SKS1", section([("u32", [1000]),
                            ("structs", (schemas.SEQUENCE_272, seq_items)),
                            ("u16", [0] * sequences),
                            ("u16", [])]))
    if parent_id:
        cw.add("SKPD", struct.pack("<8sI4s", b"\0" * 8, parent_id, b"\0" * 4))
    return cw.getvalue()


# ---------------------------------------------------------------------------
# BLP
# ---------------------------------------------------------------------------
def build_gradient_image(width: int = 64, height: int = 64,
                         alpha: str = "smooth") -> Image:
    img = Image.new(width, height)
    for y in range(height):
        for x in range(width):
            p = (y * width + x) * 4
            img.data[p] = (x * 255) // max(1, width - 1)
            img.data[p + 1] = (y * 255) // max(1, height - 1)
            img.data[p + 2] = 128
            if alpha == "opaque":
                a = 255
            elif alpha == "binary":
                a = 255 if (x // 8 + y // 8) % 2 == 0 else 0
            else:
                a = (x * 255) // max(1, width - 1)
            img.data[p + 3] = a
    return img


def build_blp(image: Image, fmt: int = PreferredFormat.DXT5,
              mips: bool = True) -> bytes:
    chain = image.mip_chain() if mips else [image]
    encoders = {
        PreferredFormat.DXT1: lambda m: bcn.encode_bc1(m.data, m.width, m.height, None),
        PreferredFormat.DXT3: lambda m: bcn.encode_bc2(m.data, m.width, m.height),
        PreferredFormat.DXT5: lambda m: bcn.encode_bc3(m.data, m.width, m.height),
    }
    payloads = [encoders[fmt](m) for m in chain]
    alpha_size = 0 if fmt == PreferredFormat.DXT1 else 8
    blp = Blp.from_images(chain, compression=2, alpha_type=fmt,
                          alpha_size=alpha_size, payloads=payloads)
    return blp.serialize()


def build_bc5_blp(width: int = 32, height: int = 32) -> bytes:
    """A BC5 normal map -- the encoding 3.3.5a cannot sample at all."""
    blocks = bytearray()
    for _ in range((width // 4) * (height // 4)):
        blocks += bytes((128, 120, 0, 0, 0, 0, 0, 0))   # red (X)
        blocks += bytes((140, 130, 0, 0, 0, 0, 0, 0))   # green (Y)
    blp = Blp(width, height, 2, 8, PreferredFormat.BC5, 0, [0] * 256, [bytes(blocks)])
    return blp.serialize()


# ---------------------------------------------------------------------------
# WMO
# ---------------------------------------------------------------------------
# ADT/WDT/WMO chunk magics are stored byte-reversed on disk.
WMO_CHUNKS_REVERSED = True


def _pad4(blob: bytes) -> bytes:
    return blob + b"\0" * ((-len(blob)) % 4)


def build_modern_wmo_root(*, groups: int = 2, materials: int = 2,
                          doodads: int = 2, texture_ids=(800001, 800002),
                          doodad_ids=(810001, 810002), skybox_id: int = 820001,
                          group_ids=(830001, 830002), num_lod: int = 2) -> bytes:
    """A BfA-shaped root: FileDataIDs instead of MOTX/MODN/MOSB string tables."""
    cw = ChunkWriter(reverse=WMO_CHUNKS_REVERSED)
    cw.add("MVER", struct.pack("<I", 17))

    mohd = bytearray(64)
    struct.pack_into("<7I", mohd, 0, len(texture_ids), groups, 0, 0, 0, doodads, 1)
    struct.pack_into("<I", mohd, 28, 0xFF808080)        # ambient colour
    struct.pack_into("<I", mohd, 32, 1234)              # WMOAreaTable id
    struct.pack_into("<6f", mohd, 36, -10.0, -10.0, 0.0, 10.0, 10.0, 20.0)
    # Legion split this uint32 into flags (low) + numLod (high).
    struct.pack_into("<I", mohd, 60, 0x0002 | (num_lod << 16))
    cw.add("MOHD", bytes(mohd))

    momt = bytearray(64 * materials)
    for i in range(materials):
        base = i * 64
        struct.pack_into("<3I", momt, base,
                         0x0004 | 0x8000,    # unculled + a post-Wrath bit
                         16 if i else 3,     # shader 16 does not exist in 3.3.5a
                         8 if i else 2)      # blend 8 does not exist either
        struct.pack_into("<I", momt, base + 12, texture_ids[i % len(texture_ids)])
        struct.pack_into("<I", momt, base + 24, 0)
        struct.pack_into("<I", momt, base + 32, 0)
    cw.add("MOMT", bytes(momt))

    cw.add("MOGN", _pad4(b"".join(f"Group{i}".encode() + b"\0" for i in range(groups))))
    mogi = bytearray(32 * groups)
    for i in range(groups):
        struct.pack_into("<I6fi", mogi, i * 32, 0x8,
                         -10.0, -10.0, 0.0, 10.0, 10.0, 20.0, 0)
    cw.add("MOGI", bytes(mogi))

    cw.add("MOSI", struct.pack("<I", skybox_id))
    cw.add("MOLT", b"")
    mods = bytearray(32)
    mods[0:8] = b"Set_$DEF"
    struct.pack_into("<III", mods, 20, 0, doodads, 0)
    cw.add("MODS", bytes(mods))
    cw.add("MODI", struct.pack("<" + "I" * len(doodad_ids), *doodad_ids))

    modd = bytearray(40 * doodads)
    for i in range(doodads):
        # nameIndex is an index into MODI here, not a byte offset into MODN.
        struct.pack_into("<I", modd, i * 40, (i & 0xFFFFFF) | (0x01 << 24))
        struct.pack_into("<3f", modd, i * 40 + 4, float(i), 0.0, 0.0)
        struct.pack_into("<4f", modd, i * 40 + 16, 0.0, 0.0, 0.0, 1.0)
        struct.pack_into("<f", modd, i * 40 + 32, 1.0)
        struct.pack_into("<I", modd, i * 40 + 36, 0xFFFFFFFF)
    cw.add("MODD", bytes(modd))
    cw.add("MFOG", bytes(48))
    cw.add("GFID", struct.pack("<" + "I" * len(group_ids), *group_ids))
    cw.add("MOUV", bytes(8 * materials))
    cw.add("MAVG", bytes(0x30))
    return cw.getvalue()


def build_modern_wmo_group(*, vertices: int = 6, triangles: int = 2,
                           uv_layers: int = 3, colour_layers: int = 2,
                           wide_indices: bool = True, wide_polys: bool = True,
                           big_material: bool = False) -> bytes:
    """A Shadowlands-shaped group: MOVX indices and MPY2 material references."""
    inner = ChunkWriter(reverse=WMO_CHUNKS_REVERSED)

    if wide_polys:
        polys = bytearray()
        for i in range(triangles):
            material = 300 if (big_material and i == 0) else i
            polys += struct.pack("<HH", 0x20, material)
        inner.add("MPY2", bytes(polys))
    else:
        inner.add("MOPY", b"".join(bytes((0x20, i)) for i in range(triangles)))

    index_list = [i % vertices for i in range(triangles * 3)]
    if wide_indices:
        inner.add("MOVX", struct.pack("<" + "I" * len(index_list), *index_list))
    else:
        inner.add("MOVI", struct.pack("<" + "H" * len(index_list), *index_list))

    verts = b"".join(struct.pack("<3f", float(i), float(i % 3), float(i % 5))
                     for i in range(vertices))
    inner.add("MOVT", verts)
    inner.add("MONR", b"".join(struct.pack("<3f", 0.0, 0.0, 1.0) for _ in range(vertices)))
    for _ in range(uv_layers):
        inner.add("MOTV", b"".join(struct.pack("<2f", 0.0, 0.0) for _ in range(vertices)))

    moba = bytearray(24)
    struct.pack_into("<6h", moba, 0, 0, 0, 0, 0, 0, 0)   # deliberately empty box
    struct.pack_into("<IHHH", moba, 12, 0, triangles * 3, 0, vertices - 1)
    moba[22] = 0
    moba[23] = 0
    inner.add("MOBA", bytes(moba))

    for _ in range(colour_layers):
        inner.add("MOCV", bytes(4 * vertices))
    inner.add("MOBS", bytes(24))
    inner.add("MOLS", bytes(56))

    header = bytearray(68)
    struct.pack_into("<II", header, 0, 0, 0)
    # 0x08000000 is a post-Wrath bit; 0x02000000/0x01000000 claim two UV/colour
    # layers, which the converter must re-derive after dropping the extras.
    struct.pack_into("<I", header, 8, 0x8 | 0x4 | 0x02000000 | 0x08000000)
    struct.pack_into("<6f", header, 12, -10.0, -10.0, 0.0, 10.0, 10.0, 20.0)
    struct.pack_into("<6H", header, 0x24, 0, 0, 0, 1, 0, 0)
    struct.pack_into("<I", header, 0x34, 0xFFFFFFFF)
    struct.pack_into("<I", header, 0x38, 1234)
    struct.pack_into("<II", header, 0x3C, 0x1, 0xFFFFFFFF)  # flags2 + split index

    outer = ChunkWriter(reverse=WMO_CHUNKS_REVERSED)
    outer.add("MVER", struct.pack("<I", 17))
    outer.add("MOGP", bytes(header) + inner.getvalue())
    return outer.getvalue()


# ---------------------------------------------------------------------------
# ADT
# ---------------------------------------------------------------------------
def build_split_adt(*, chunks: int = 4, layers: int = 2,
                    texture_ids=(700001, 700002), doodad_refs: int = 3,
                    object_refs: int = 2, high_res_holes: bool = True
                    ) -> tuple[bytes, bytes, bytes]:
    """A Cataclysm-style split tile: (root, tex0, obj0)."""
    rev = WMO_CHUNKS_REVERSED

    # -- root -----------------------------------------------------------
    root = ChunkWriter(reverse=rev)
    root.add("MVER", struct.pack("<I", 18))
    mhdr = bytearray(64)
    struct.pack_into("<I", mhdr, 0, 0x1)   # has MFBO
    root.add("MHDR", bytes(mhdr))
    root.add("MH2O", b"")

    for i in range(chunks):
        hdr = bytearray(128)
        flags = 0x0
        if high_res_holes:
            flags |= 0x10000
            # 8x8 mask: punch the top-left 2x2 high-res quadrant only.
            hdr[0x40] = 0b00000011
            hdr[0x41] = 0b00000011
        struct.pack_into("<I", hdr, 0, flags)
        struct.pack_into("<II", hdr, 4, i % 16, i // 16)
        struct.pack_into("<I", hdr, 0x34, 1519)                  # area id
        struct.pack_into("<3f", hdr, 0x68, float(i), 0.0, 0.0)   # position
        inner = ChunkWriter(reverse=rev)
        inner.add("MCVT", b"".join(struct.pack("<f", float(n % 7)) for n in range(145)))
        inner.add("MCCV", bytes(145 * 4))
        inner.add("MCNR", bytes(448))
        inner.add("MCLV", bytes(145 * 4))     # Cataclysm-only, must be dropped
        inner.add("MCSE", bytes(28 * 2))
        root.add("MCNK", bytes(hdr) + inner.getvalue())
    root.add("MFBO", bytes(36))

    # -- tex0 -----------------------------------------------------------
    tex = ChunkWriter(reverse=rev)
    tex.add("MVER", struct.pack("<I", 18))
    tex.add("MDID", struct.pack("<" + "I" * len(texture_ids), *texture_ids))
    tex.add("MHID", struct.pack("<" + "I" * len(texture_ids), *([0] * len(texture_ids))))
    tex.add("MTXP", bytes(16 * len(texture_ids)))
    for _ in range(chunks):
        inner = ChunkWriter(reverse=rev)
        mcly = bytearray()
        for layer in range(layers):
            # textureId, flags, offsetInMCAL, effectId
            mcly += struct.pack("<IIIi", layer % len(texture_ids), 0x100 if layer else 0,
                                0 if layer == 0 else 2048 * (layer - 1), -1)
        inner.add("MCLY", bytes(mcly))
        inner.add("MCSH", bytes(512))
        inner.add("MCAL", bytes(2048 * max(0, layers - 1)))
        inner.add("MCMT", bytes(layers))   # Cataclysm-only
        tex.add("MCNK", inner.getvalue())

    # -- obj0 -----------------------------------------------------------
    obj = ChunkWriter(reverse=rev)
    obj.add("MVER", struct.pack("<I", 18))
    obj.add("MMDX", b"world\\doodad\\tree.m2\0")
    obj.add("MMID", struct.pack("<I", 0))
    obj.add("MWMO", b"world\\wmo\\house.wmo\0")
    obj.add("MWID", struct.pack("<I", 0))
    obj.add("MDDF", bytes(36))
    obj.add("MODF", bytes(64))
    for _ in range(chunks):
        inner = ChunkWriter(reverse=rev)
        inner.add("MCRD", struct.pack("<" + "I" * doodad_refs,
                                      *range(doodad_refs)))
        inner.add("MCRW", struct.pack("<" + "I" * object_refs,
                                      *range(object_refs)))
        obj.add("MCNK", inner.getvalue())

    return root.getvalue(), tex.getvalue(), obj.getvalue()


# ---------------------------------------------------------------------------
# WDT
# ---------------------------------------------------------------------------
def build_modern_wdt(*, tiles=((32, 48), (33, 48)), flags: int = 0x0201,
                     global_wmo: bool = False, with_maid: bool = True) -> bytes:
    """A BfA-shaped map index: MAID present, big-alpha flag absent."""
    cw = ChunkWriter(reverse=WMO_CHUNKS_REVERSED)
    cw.add("MVER", struct.pack("<I", 18))

    mphd = bytearray(32)
    struct.pack_into("<I", mphd, 0, flags | (1 if global_wmo else 0))
    struct.pack_into("<I", mphd, 4, 123456)   # Cata+ map texture FileDataID
    cw.add("MPHD", bytes(mphd))

    main = bytearray(64 * 64 * 8)
    for x, y in tiles:
        struct.pack_into("<I", main, (y * 64 + x) * 8, 1)
    cw.add("MAIN", bytes(main))

    if with_maid:
        cw.add("MAID", bytes(64 * 64 * 8 * 4))
    cw.add("MWMO", b"world\\wmo\\global.wmo\0" if global_wmo else b"")
    if global_wmo:
        cw.add("MODF", bytes(64))
    cw.add("MPL2", bytes(24))
    return cw.getvalue()


def build_oversized_wmo_group(*, vertices: int = 70000, strips: int = 4) -> bytes:
    """A Shadowlands group with more vertices than 16-bit indices can reach.

    The geometry is a set of disjoint triangle strips so a splitter has natural
    seams to cut along, and each strip is covered by its own render batch.
    """
    import math

    inner = ChunkWriter(reverse=WMO_CHUNKS_REVERSED)

    per_strip = vertices // strips
    verts = bytearray()
    normals = bytearray()
    uvs = bytearray()
    colours = bytearray()
    triangles: list[tuple[int, int, int]] = []
    strip_ranges: list[tuple[int, int]] = []

    for strip in range(strips):
        base = strip * per_strip
        first_triangle = len(triangles)
        for i in range(per_strip):
            angle = (i / per_strip) * math.tau
            verts += struct.pack("<3f", math.cos(angle) * (10 + strip * 20),
                                 math.sin(angle) * (10 + strip * 20),
                                 float(i % 17))
            normals += struct.pack("<3f", 0.0, 0.0, 1.0)
            uvs += struct.pack("<2f", i / per_strip, 0.0)
            colours += struct.pack("<4B", 255, 255, 255, 255)
        for i in range(per_strip - 2):
            triangles.append((base + i, base + i + 1, base + i + 2))
        strip_ranges.append((first_triangle, len(triangles)))

    index_words = []
    for tri in triangles:
        index_words.extend(tri)
    inner.add("MPY2", b"".join(struct.pack("<HH", 0x20, 1)
                               for _ in triangles))
    inner.add("MOVX", struct.pack("<" + "I" * len(index_words), *index_words))
    inner.add("MOVT", bytes(verts))
    inner.add("MONR", bytes(normals))
    inner.add("MOTV", bytes(uvs))

    moba = bytearray()
    for first, last in strip_ranges:
        record = bytearray(24)
        struct.pack_into("<6h", record, 0, -1, -1, -1, 1, 1, 1)
        struct.pack_into("<IHHH", record, 12, first * 3, (last - first) * 3,
                         0, per_strip - 1)
        record[23] = 1
        moba += record
    inner.add("MOBA", bytes(moba))
    inner.add("MOCV", bytes(colours))
    inner.add("MODR", struct.pack("<HH", 0, 1))

    header = bytearray(68)
    struct.pack_into("<I", header, 8, 0x8 | 0x4 | 0x800)
    struct.pack_into("<6f", header, 12, -100.0, -100.0, 0.0, 100.0, 100.0, 20.0)
    struct.pack_into("<I", header, 0x34, 0xFFFFFFFF)

    outer = ChunkWriter(reverse=WMO_CHUNKS_REVERSED)
    outer.add("MVER", struct.pack("<I", 17))
    outer.add("MOGP", bytes(header) + inner.getvalue())
    return outer.getvalue()


def build_model_vertices(count: int) -> bytes:
    """``count`` M2Vertex records laid out along a line."""
    out = bytearray()
    for i in range(count):
        out += struct.pack("<3f4B4B3f4f",
                           float(i % 997), float((i // 997) % 97), float(i % 13),
                           255, 0, 0, 0, 0, 0, 0, 0,
                           0.0, 0.0, 1.0,
                           (i % 64) / 64.0, 0.0, 0.0, 0.0)
    return bytes(out)


def build_skin_for(submesh_vertex_lists, *, legion: bool = False,
                   bone_count_max: int = 4) -> bytes:
    """A .skin whose submeshes reference the given model vertex indices.

    Each entry of ``submesh_vertex_lists`` is the list of model vertex indices
    one submesh draws; triangles are generated over them in order.
    """
    vertices: list[int] = []
    indices: list[int] = []
    submeshes = []
    batches = []

    for submesh_index, model_vertices in enumerate(submesh_vertex_lists):
        vertex_start = len(vertices)
        vertices.extend(model_vertices)
        index_start = len(indices)
        for i in range(len(model_vertices) - 2):
            indices.extend((vertex_start + i, vertex_start + i + 1,
                            vertex_start + i + 2))
        # Level carries the high 16 bits of indexStart.
        submeshes.append(struct.pack(
            "<10H3f3ff", submesh_index, index_start >> 16, vertex_start,
            len(model_vertices), index_start & 0xFFFF,
            len(indices) - index_start, 1, 0, 1, 0,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0))
        batches.append(struct.pack(
            "<BbHHHHHHHHHHH", 0, 0, 0, submesh_index, 0, 0, 0, 0, 1, 0, 0, 0, 0))

    header_size = 56 if legion else 48
    payload = bytearray()
    offsets = {}

    def place(name: str, blob: bytes) -> None:
        while len(payload) % 4:
            payload.append(0)
        offsets[name] = header_size + len(payload)
        payload.extend(blob)

    place("vertices", struct.pack("<" + "H" * len(vertices), *vertices))
    place("indices", struct.pack("<" + "H" * len(indices), *indices))
    place("bones", bytes(len(vertices) * 4))
    place("submeshes", b"".join(submeshes))
    place("batches", b"".join(batches))

    head = bytearray(SKIN_MAGIC.encode("latin-1"))
    head += struct.pack("<II", len(vertices), offsets["vertices"])
    head += struct.pack("<II", len(indices), offsets["indices"])
    head += struct.pack("<II", len(vertices), offsets["bones"])
    head += struct.pack("<II", len(submeshes), offsets["submeshes"])
    head += struct.pack("<II", len(batches), offsets["batches"])
    head += struct.pack("<I", bone_count_max)
    if legion:
        head += struct.pack("<II", 0, 0)
    assert len(head) == header_size
    return bytes(head) + bytes(payload)


# ---------------------------------------------------------------------------
# WDL
# ---------------------------------------------------------------------------
def build_wdl(*, tiles=((0, 0), (32, 48)), holes: bool = True,
              lod_mesh: bool = True, version: int = 18,
              wmo_tables: bool = False, mare_size: int | None = None) -> bytes:
    """A .wdl with a heightmap for `tiles`, optionally Legion's LOD mesh."""
    from wotlkconv.adt.wdl import MAHO_SIZE, MAOF_ENTRIES, MARE_SIZE

    out = bytearray()

    def add(name: str, payload: bytes) -> int:
        at = len(out)
        out.extend(name[::-1].encode("latin-1"))
        out.extend(struct.pack("<I", len(payload)))
        out.extend(payload)
        return at

    add("MVER", struct.pack("<I", version))
    if wmo_tables:
        add("MWMO", b"world/wmo/a.wmo\0")
        add("MWID", struct.pack("<I", 0))
        add("MODF", b"\0" * 64)
    if lod_mesh:
        add("MLHD", b"\0" * 16)
        add("MLVH", b"\0" * 32)

    maof_at = add("MAOF", b"\0" * (MAOF_ENTRIES * 4))
    maof_data = maof_at + 8
    size = MARE_SIZE if mare_size is None else mare_size
    for n, (x, y) in enumerate(tiles):
        index = y * 64 + x
        # A recognisable height per tile, so a mix-up in the index shows up.
        payload = struct.pack("<h", 100 + n) * (size // 2)
        struct.pack_into("<I", out, maof_data + index * 4, add("MARE", payload))
        if holes:
            add("MAHO", struct.pack("<H", n + 1) * (MAHO_SIZE // 2))

    if lod_mesh:
        add("MLND", b"\0" * 16)
        add("MLFD", b"\0" * 8)
    return bytes(out)


# ---------------------------------------------------------------------------
# Liquid volumes
# ---------------------------------------------------------------------------
def build_liquid(*, version: int = 1, liquid_type: int = 2, blocks: int = 2,
                 magic: bytes = b"LIQ*", block_size: int = 64) -> bytes:
    """A .wlw/.wlq liquid volume: a 12-byte header and opaque blocks."""
    out = bytearray(magic)
    out += struct.pack("<HHI", version, liquid_type, blocks)
    for i in range(blocks):
        out += bytes([(i + 1) & 0xFF]) * block_size
    return bytes(out)
