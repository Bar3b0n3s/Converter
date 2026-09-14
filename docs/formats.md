# What changed, and what this tool does about it

Reference for each format the converter touches: what modern builds do
differently from 3.3.5a, and how the downgrade handles it. Field offsets are
from the start of the struct unless stated otherwise.

---

## M2 — models

### Container

3.3.5a reads a flat file beginning `MD20` followed by `uint32 version = 264`.
Legion (272) wrapped that same body in an `MD21` chunk and moved every external
reference into sibling chunks:

| Chunk | Carries | Handling |
|---|---|---|
| `MD21` | the MD20 body; offsets are relative to the chunk payload | unwrapped |
| `TXID` | one FileDataID per `M2Texture` | resolved to paths, written inline |
| `SFID` | skin profile FileDataIDs, then Legion LOD skins | first four used, rest reported |
| `AFID` | `{animId, subAnimId, fileId}` per external animation | renamed `<model><anim:04d>-<sub:02d>.anim` |
| `SKID` | the `.skel` holding bones and sequences | loaded and merged |
| `BFID` | `.bone` overrides | dropped |
| `PFID` | physics rig | dropped — 3.3.5a has no model physics |
| `PABC` `PADC` `PSBC` `PEDC` | parent-animation data | dropped |
| `LDV1` | LOD data | dropped |
| `TXAC` `EXPT` `EXP2` `PGD1` | extended particle data | dropped |
| `WFV1`–`WFV3` `DETL` `NERF` `EDGF` `DBOC` `AFRA` | shader/light extras | dropped |
| `RPID` `GPID` | recursive and geometry particle models | dropped |

Note the magics in an M2 chunk table are stored in reading order (`MD21`),
unlike ADT/WDT/WMO where they are byte-reversed (`REVM` for `MVER`). The chunk
reader detects which convention a file uses rather than assuming.

### Header

The MD20 header layout is identical between 264 and 274 — 0x130 bytes, or
0x138 when `global_flags & 0x08` adds the texture-combiner-combo array. Nothing
moves; only the contents need clamping.

### Structs that changed shape

| Struct | 264 | 272+ | Downgrade |
|---|---|---|---|
| `M2Sequence` (64 B) | `uint32 blend_time` at 0x24 | `uint16 blend_time_in`, `uint16 blend_time_out` | keeps the longer of the two |
| `M2Camera` | 100 B, `float fov` | 116 B, `M2Track<M2SplineKey<float>> fov` | first key of the track, or 0.9749 rad |
| `M2Particle` | 476 B | 492 B | fields mapped individually (below) |

Everything else — `M2CompBone` (88), `M2Texture` (16), `M2Material` (4),
`M2Color` (40), `M2TextureWeight` (20), `M2TextureTransform` (60),
`M2Attachment` (40), `M2Event` (36), `M2Light` (156), `M2Ribbon` (176) — is
byte-identical across the range, and the test suite asserts every one of those
sizes.

### Particles

Cataclysm reused the two bytes at 0x2C (`particle_type`, `head_or_tail`) for
multi-texture parameters and appended two 8-byte parameter blocks, giving 492.
The downgrade zeroes the reclaimed bytes, drops the appended blocks, and:

- clears flag `0x10000000` (multi-texture), keeping the first of the three
  5-bit texture indices packed into the `texture` field;
- masks flags to `0x0FFFFFFF`;
- maps emitter types 3 (spline, Cataclysm) and 4 (bone, Legion) to 1 (plane),
  since 3.3.5a implements only plane and sphere;
- clamps blend modes above 6 to additive.

### Feature clamping

| Field | 3.3.5a range | Above it |
|---|---|---|
| `M2Material.blending_mode` | 0–6 | 7 (`BlendAdd`) → 4 (`Add`); anything higher → 2 (`Alpha`) |
| `M2Material.flags` | `0x1F` | cleared |
| `M2CompBone.flags` | `0x3FF` | cleared (`0x400` kinematic, `0x1000` helmet-scaled are Cata+) |
| header `global_flags` | `0x0B` | cleared |
| `M2Texture.type` | ≤ 15 | treated as a hardcoded texture |
| `M2Texture.flags` | `0x3` | cleared |

### Skeletons

Legion moved bones, key bone lookups, attachments, global loops and the whole
sequence table out of the M2 and into a `.skel` referenced by `SKID`, so that
every variant of a race could share one rig. 3.3.5a has nowhere to put that.
The converter loads the skeleton, follows its `SKPD` parent chain (first
definition of each section wins), and folds the sections back into the model.

A rigged model whose `.skel` cannot be found is a hard failure by default,
because the alternative is a model with no bones and no animations that looks
converted. `--allow-missing-skeleton` opts into that.

### Too many vertices

A `.skin` addresses model vertices through a `uint16` array, so no profile —
modern or Wrath — reaches past vertex 65 535. A model with more is
unrenderable by *any* client, which is why the converter tries compaction
before it gives up: geometry no submesh draws is dropped and the skins are
renumbered, which is lossless and usually settles it.

A model still too large after that is split into several `.m2` files sharing
one rig, materials and textures, with model-wide effects, attachments and
collision left on the first piece. Opt-in (`--split-models`), because the extra
pieces are assets nothing references yet.

---

## SKIN — draw data

3.3.5a's header is 48 bytes; Legion appended an `M2Array<M2ShadowBatch>` for
its shadow pass, giving 56. The two are told apart by where the payload starts
and whether the candidate array is plausible.

- Shadow batches are dropped; 3.3.5a draws model shadows from a blob texture.
- `M2Batch.textureCount` (offset **0x0E**) is clamped to 2.
- `M2Batch.shader_id` (0x02) keeps its `0x8000` combiner form only when the
  model carries a combiner table; otherwise it resets to 0.
- `M2SkinSection.boneCount` above 256 and `boneInfluences` above 4 are
  reported, not silently accepted.
- `M2SkinSection.Level` is **not** a LOD number: it carries the high 16 bits of
  `indexStart`, which is how a skin addresses more than 65 536 triangle indices
  without widening the field. Any tool that rebuilds a skin has to write it, or
  large submeshes silently wrap.
- `vertexStart` has no such extension, so a skin's vertex list tops out at
  65 536 entries; `indexCount` is a plain `uint16`, capping one submesh at
  21 845 triangles.

---

## ANIM — external animations

3.3.5a reads a flat blob whose offsets come from the model's per-sequence track
sub-arrays. Legion wrapped it in `AFM2` and added `AFSA`/`AFSB` for
physics-driven bone tracks. Converting unwraps `AFM2` and drops the rest.

The track offsets are relative to the start of the animation payload, so
extracting the chunk body leaves them valid without rewriting.

---

## BLP — textures

The container is unchanged: a 1172-byte header, then up to 16 mip payloads.
What changed is the encodings in use. 3.3.5a samples palettised (compression
1), DXT1/DXT3/DXT5 (compression 2, `alpha_type` 0/1/7) and raw BGRA
(compression 3). Legion normal maps use `alpha_type = 11` (BC5), which it
cannot decode at all.

- A texture already in a supported encoding, power-of-two and within the size
  cap, is re-emitted with its block data **byte for byte identical**, so
  repeated conversions never accumulate loss.
- BC5 is decoded (reconstructing Z into blue) and re-encoded. 3.3.5a has no
  normal-mapped shader path, so the result is only useful as diffuse or
  overlay art, and the report says so.
- Non-power-of-two dimensions are resized: the old client only mipmaps
  power-of-two textures.
- `--texture-format auto` picks DXT1 when the image is opaque, DXT1 with
  punch-through when alpha is only 0 or 255, and DXT5 when it has gradients.

---

## WMO — world objects

The version stayed 17, which is why modern WMOs look deceptively compatible.

### Root

| Modern | 3.3.5a | Handling |
|---|---|---|
| `MODI` (doodad FileDataIDs) | `MODN` name table | rebuilt; `MODD.nameIndex` changes from an array index to a byte offset |
| `MOSI` (skybox FileDataID) | `MOSB` filename | rebuilt |
| `GFID` (group FileDataIDs) | `<root>_000.wmo` … | groups renamed |
| no `MOTX`, FileDataIDs in `MOMT` | `MOTX` + byte offsets | rebuilt |
| `MOHD` `uint16 flags` + `uint16 numLod` | one `uint32 flags` | flags masked to `0x0F`, LOD count reported |
| `MOUV` `MAVG` `MAVD` `MBVD` `MOLS` `MOLP` `MOP2` … | — | dropped |

`SMOMaterial.shader` above 6 resets to diffuse and `blendMode` above 6 to
alpha; flags are masked to `0x1FF`.

### Groups

| Modern | 3.3.5a | Handling |
|---|---|---|
| `MOVX` (uint32 indices) | `MOVI` (uint16) | converted; over 65 535 vertices is a hard failure |
| `MPY2` (uint16 material ids) | `MOPY` (uint8) | converted; ids above 255 become the collision-only material `0xFF` |
| 3+ `MOTV` / `MOCV` layers | 2 | extra layers dropped and the header flags corrected to match |
| more than 65 535 vertices | several groups | split, with the root updated to match |
| `MOBS` `MOLS` `MOLP` `MOC2` `MOPL` `MDAL` … | — | dropped |

Shadowlands reused the first twelve bytes of each `MOBA` batch — Wrath's
bounding box — for a wide material id. When `MPY2` is present (the same era),
the boxes are recomputed from the group's own vertices rather than left as
values the client would cull against.

`MOGP`'s `flags2` and split-group indices are zeroed; Wrath has no `flags2` and
reads the last word as padding.

### Splitting an oversized group

`MOVI` is `uint16`, so a 3.3.5a group tops out at 65 536 vertices — which is
exactly why Shadowlands introduced `MOVX`. Those groups cannot be narrowed, but
a WMO is a *collection* of groups and nothing stops it having more, so an
oversized group becomes several and the root is updated:

* `MOHD.nGroups` grows;
* each new part gets an `MOGI` entry with its own bounding box, the original
  group's flags, and a name appended to `MOGN`;
* the parts are written as `<root>_NNN.wmo` after every existing group, so the
  numbering the root already uses stays put.

Cuts are made on render-batch boundaries where possible, so in the common case
no vertex is duplicated. Each part's `MOBA` batches get corrected index ranges
and recomputed bounds, and **the collision tree is rebuilt** — `MOBN`/`MOBR`
index triangle numbers the split invalidates, and a group without a valid tree
is one players walk through. Liquid stays with the first part rather than being
rendered several times over.

---

## ADT — terrain

Cataclysm split each tile into `Zone_32_48.adt` (heights), `_tex0` (texture
layers), `_obj0` (placements) and LOD variants, and dropped `MCIN` because it
no longer needed the index. 3.3.5a reads one monolithic file with `MCIN`.

Merging walks all 256 map chunks across the three files and rebuilds each
`MCNK`:

- sub-chunk offsets are relative to the start of the chunk **including** its
  8-byte header, so all of them are recomputed;
- `MCRD` (doodad refs) and `MCRW` (WMO refs) concatenate back into one `MCRF`,
  with both counts written into the chunk header at 0x10 and 0x38;
- `sizeAlpha`, `sizeShadow` and `sizeLiquid` include the sub-chunk header, so
  a chunk with no liquid still declares 8;
- Cataclysm's 8×8 hole mask (flag `0x10000`, stored where Wrath keeps the
  low-quality texture map) folds down to the 4×4 mask at 0x3C;
- `MCLV`, `MCMT`, `MCBB` and `MCDD` are dropped.

`MHDR`'s offsets, which are relative to the start of its own payload and point
at chunk headers, are rewritten. `MDID`/`MHID` FileDataIDs become an `MTEX`
string table; `MTXP` and the blend-mesh chunks are dropped.

---

## WDT — map index

`MAID` (per-tile FileDataIDs) is dropped, along with `MANM`, `MPL2`/`MPL3` and
the rest. `MPHD.flags` is masked to `0x0F`.

One flag matters more than the others. `MPHD.flags & 0x4` ("big alpha") tells
the client `MCAL` holds 8-bit alpha maps rather than 4-bit ones. Cataclysm and
later always write the 8-bit form, so **the converter sets that bit** — without
it every terrain texture blend renders wrong. The ADT converter warns about the
same thing, because the two files are often converted separately.

---

## CASC — reading a game install

```
.build.info           active build's config hash
Data/config/xx/yy/…   build config: root and encoding file hashes
Data/data/*.idx       EKey prefix -> (archive number, offset, size)
Data/data/data.NNN    a 30-byte entry header, then a BLTE stream
```

Resolving one FileDataID: root gives a **content** key, encoding turns that
into an **encoding** key, the index says where that lives, and BLTE decodes it.

- BLTE header sizes and chunk sizes are **big-endian**, unlike every other
  Blizzard format. Chunk modes: `N` stored, `Z` zlib, `4` LZ4, `F` nested,
  `E` encrypted (Salsa20 or ARC4, with the IV mixed with the chunk index).
- Index entries are 18 bytes: a 9-byte key prefix, a 40-bit big-endian position
  whose top 10 bits are the archive number, and a little-endian size.
- Root blocks store FileDataIDs as deltas (`id = previous + delta + 1`).
  `contentFlags`, `localeFlags` and the record count are **unsigned** —
  `localeFlags` is `0xFFFFFFFF` for "every locale".

Only local storage is read. Partial installs stream the rest from Blizzard's
CDN; missing files are reported, never downloaded.

---

## DB2 — client databases

Cataclysm renamed `.dbc` to `.db2`; Legion stopped storing records as structs.
A WDC-family table packs each column to the narrowest width its values need,
hoists constant columns into a side table, replaces repeated values with
indices into a palette, and scatters records across sections that may be
encrypted.

Field storage types, all of which the reader handles:

| Type | Meaning |
|---|---|
| 0 `none` | plain bits in the record, one run per array element |
| 1 `bitpacked` | a narrow field at a bit offset |
| 2 `common_data` | a side table keyed by **record id**, with a constant default |
| 3 `bitpacked_indexed` | the record holds an index into a palette |
| 4 `bitpacked_indexed_array` | as above, but each slot holds a whole array |
| 5 `bitpacked_signed` | as 1, sign-extended |

Also handled: id lists (the id lives outside the record), copy tables (a row
cloned under a new id), relationship columns, and sparse offset maps.

Note `contentFlags`, `localeFlags` and the record count in a root-style block
are **unsigned** — reading `localeFlags` as signed makes `0xFFFFFFFF` ("every
locale") come out as -1 and match nothing.

### Getting to a .dbc

Three inputs have to line up:

1. **The data**, from the `.db2`.
2. **Column names**, from a DBD definition matched on the file's own layout
   hash. A `.db2` carries no names, and mapping by position across fifteen
   years of column churn is not safe, so a conversion without a definition
   refuses rather than guessing.
3. **The target layout**, from a mapping file and ideally from the user's own
   client `.dbc` used as a template.

Merging onto a template keeps its records and string block **byte for byte** and
appends to them. Existing string offsets stay valid, so the merge needs to know
the types only of the fields it writes — getting a field type wrong elsewhere in
the row cannot corrupt anything. A template whose field count disagrees with the
mapping is a hard failure, which is the check that catches a mapping written for
a different build.

Two spellings the client insists on, handled by transforms:

* model paths in a DBC end in **`.mdx`**, not `.m2`; the client swaps the
  extension when it opens the file, so a `.m2` path simply does not load;
* texture variation columns hold a **bare filename** with no directory and no
  extension, resolved against the model's own folder.

---

<a name="assumptions"></a>
## Assumptions where the format is ambiguous

Two values are not pinned down by the published documentation, and were chosen
for internal consistency. If converted terrain misbehaves in a client, check
these first — they are isolated and easy to flip.

**`MCIN[i].size` includes the 8-byte chunk header.** `MCIN.offset` points at
the `MCNK` magic, so `offset + size` lands exactly on the next chunk. The
documentation only says "the size of the MCNK chunk". Written in
`src/wotlkconv/adt/convert.py`.

**`.anim` track offsets are relative to the `AFM2` payload, not the file.**
Extracting the chunk body therefore leaves them valid. This is the only
self-consistent reading — a whole-file base would break as soon as the chunk
moved — but it has not been checked against a client. Written in
`src/wotlkconv/m2/anim.py`.

**The built-in DBC field counts and column indices.** These are the least
certain thing in the project: the 3.3.5a layouts are not in any file the tool
can read, and they came from documentation rather than from a client. Every
built-in mapping is marked `"verified": false`, the tool warns when it uses one
without a template, and it hard-fails when a template disagrees. Pass
`--template-dir` pointing at your own client's `dbc` folder and the guesswork
disappears. Written in `src/wotlkconv/db/builtin/*.json`.
