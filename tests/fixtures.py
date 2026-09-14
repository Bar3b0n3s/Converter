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


def build_modern_model(*, sequences: int = 2, bones: int = 3, vertices: int = 6,
                       textures: int = 2, particles: int = 1, cameras: int = 1,
                       ribbons: int = 1, lights: int = 1,
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
        s.update(id=i, variation_index=0, duration=1000 + i, movespeed=1.5,
                 flags=0x20, frequency=32767, replay_min=0, replay_max=0,
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
