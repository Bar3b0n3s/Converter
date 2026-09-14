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

<a name="wdl"></a>
## WDL — low-resolution heightmaps

What the client draws on the horizon before real terrain streams in. One 17x17
grid of 16-bit heights, plus the 16x16 grid between those points, stands in for
each of the map's 64x64 tiles.

| Chunk | Holds | Converted to |
|---|---|---|
| `MAOF` | 64x64 absolute file offsets, one per tile | rewritten for the new layout |
| `MARE` | the 17x17 and 16x16 height grids (1090 bytes) | kept verbatim |
| `MAHO` | one 16-bit hole mask per inner row | kept verbatim |
| `MWMO`/`MWID`/`MODF` | low-detail WMO placements | kept, or emitted empty |
| `ML*` | Legion's LOD mesh: vertices, indices, skirts, liquid, placements | dropped |

The heightmap itself never changed. What Legion added is a separate thing
sharing the file — a real LOD mesh, with its own copies of the doodad and WMO
placements, for a renderer 3.3.5a does not have.

The catch is `MAOF`: its 4096 entries are absolute file offsets, so dropping
anything ahead of the `MARE` blocks moves every one of them and they all have
to be rewritten. Wrath also expects the low-detail WMO tables that Legion
stopped writing, so empty ones are emitted to keep the file the shape the old
client reads. An offset that does not lead to a `MARE` of the right size is
reported (`wdl.tiles.unreadable`) and the tile left empty, rather than followed
into whatever happens to be there.

---

<a name="liquid"></a>
## WLW / WLQ / WLM — liquid volumes

These sit beside a map and describe the lakes, rivers and lava the terrain's
own liquid grid cannot: volumes with real shape, the ones the client tests
against when deciding whether you are swimming. 3.3.5a reads all three, so
unlike the LOD and lighting sidecars they belong in a patch.

| Offset | Field | Notes |
|---|---|---|
| 0x00 | `magic` | `LIQ*`; accepted in either byte order |
| 0x04 | `version` | 3.3.5a reads 0 and 1 |
| 0x06 | `liquidType` | water, lava, slime … |
| 0x08 | `blockCount` | volume blocks that follow |

What this tool does is deliberately narrower than a conversion. The header is
well understood; the block layout after it is not documented well enough to
rewrite safely, and a liquid volume rewritten wrongly puts swimmable water
where there is none. So the version is checked and the body passed through
untouched: a version Wrath reads is already the file the old client wants, and
a newer one is refused by name rather than copied, because a client that
cannot parse the body is worse off with the file than without it.

If a build turns out to ship a version this refuses, that is the point at
which the block layout has to be worked out — and the report says so, instead
of leaving a misparsed file to be discovered in-game.

---

<a name="sidecars"></a>
## Map sidecars — what is deliberately left out

A modern map folder holds more than the tile. These share an extension with
the map files they sit beside, so each is recognised by a chunk only it has:

| File | Marker chunks | Holds |
|---|---|---|
| `_lgt.wdt` | `MPLT` `MPL2` `MPL3` `MLTA` | per-tile light definitions and animations |
| `_occ.wdt` | `MAOI` `MAOH` | terrain occlusion hulls and heightmap |
| `_fogs.wdt` | `MVFX` `VFOG` | volumetric fog |
| `_mpv.wdt` | `MPVD` | particulate volumes |
| `_lod.adt` | `MLHD` `MLVH` `MLLL` | the LOD terrain mesh |
| `_tex1.adt` `_obj1.adt` | — | high-detail texture and object variants |

3.3.5a keeps none of this and never looks for the files, so they are skipped
with what they held rather than copied into the patch. The check runs *after*
the map formats identify themselves, because a real `.wdl` carries the same
LOD chunks a `_lod.adt` does and is told apart by the `MAOF` that makes it a
heightmap.

`_tex0` and `_obj0` are the exception: they are merged into the tile, and the
report records them as carried by it rather than leaving them unmentioned.

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

Magics read: **WDC1** (Legion 7.3) through **WDC5** (The War Within). Earlier
ones — `WDB2` to `WDB6`, Cataclysm through Legion 7.2 — are refused. They have
no field-storage table and measure string offsets from the string block rather
than from the field, so reading one as a WDC yields plausible wrong values
instead of an error.

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

A **sparse** table has no fixed-size record block: an offset map gives each
present row a `(offset, size)` into a region of packed records whose strings are
inline. Where the map sits differs by era — WDC2 points at it from the section
header, WDC3 and later place it after the id list and copy table. Because that
layout is easy to get wrong and a wrong read yields nonsense rather than an
error, everything is checked: offsets must land inside the record region, the
number of present rows must match the section header, and walking a record's
columns must consume exactly the bytes the map allotted it. Any mismatch raises.

A file whose header this reader has misjudged is caught the same way — the
record block is checked against the file's actual length before any row is
decoded.

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
## Versions, and what happens outside them

Every converter applies one layout. These are the headers that say whether it
applies, and what each converter does when a file disagrees:

| Format | Understood | Older | Newer |
|---|---|---|---|
| M2 | 264–274 | refused — this tool converts down, not up | read with the newest schemas, and flagged |
| WMO | 17 | refused — v14 is a different layout wearing the same magic | read as v17, and flagged |
| ADT / WDT / WDL | `MVER` 18 | flagged | flagged |
| BLP | BLP2 v1 | BLP1 refused | unknown compression refused |
| liquid | 0–1 | — | refused, by version number |

Nothing downstream branches on the ADT, WDT or WDL version, which is exactly
why reading it is worth doing: a file declaring something else is being parsed
on an assumption nobody stated.

A chunk in neither the Wrath set nor the known-modern set — one Blizzard adds
after this was written, or one nobody documented — is dropped, because there is
no other option, but it is named in the report and makes the file lossy. That
covers M2, WMO roots and groups, ADT (top level and inside each `MCNK`), WDT
and WDL.

---

<a name="assumptions"></a>
## Where the format is ambiguous, and how it is settled

Three things the published documentation leaves open used to be chosen for
internal consistency and left at that. None of them is a standing assumption
any more: each is either decided from data the file itself carries, or checkable
against something you already have.

**`.anim` track offsets: the `AFM2` payload, or the whole file?** Being wrong
by eight bytes does not crash, it animates wrongly, which is the worst kind of
wrong. It is no longer guessed. A model names, for each sequence it keeps
outside itself, the exact `(offset, length)` of every keyframe array the
`.anim` is expected to hold; those spans have to fit the file, and an `.anim`
exists to hold them and nothing else, so under the right reading the last one
also ends exactly where the file does. Both readings are tested against the
spans per file, and the payload is emitted to match whichever one they support
— `anim.offsets.payload` or `anim.offsets.file` in the report says which, and
`anim.offsets.unmeasured` says when the model named nothing to measure.
Written in `src/wotlkconv/m2/anim.py`.

**`MCIN[i].size`: the payload, or the payload plus the chunk header?** The
offset is unambiguous — it points at the `MCNK` magic — so a reader that seeks
there and then trusts the chunk's own size field, as the client does, cannot be
misled either way. Only a tool that takes `MCIN`'s size as the extent of the
chunk can be, and for that reader the header-inclusive value is the safe one:
it spans the whole chunk, where the payload-only value stops eight bytes short
and cuts the end off the last sub-chunk. That is the default, and
`--reference-adt` reads the convention straight off any genuine 3.3.5a tile by
comparing each entry's size against the size the chunk itself declares.
Written in `src/wotlkconv/adt/convert.py`.

**The 3.3.5a DBC layouts.** These used to be the least certain thing in the
project — hand-written field counts and column indices that came from
documentation rather than from a client. They are no longer written down at
all. DBDefs carries a `BUILD 3.3.5.12340` layout for every table that existed
in Wrath, naming each column in order with its type and array size, and that is
where the layout now comes from; a `--template` `.dbc` cross-checks the width
and hard-fails on disagreement. A table the definitions do not cover for that
build is refused rather than guessed at, because a `.dbc` records how many
columns there are and never what belongs in them. Written in
`src/wotlkconv/db/target.py`.
