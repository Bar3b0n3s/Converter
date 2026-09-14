# 3.3.5a limits the converter enforces

Everything here lives in `src/wotlkconv/limits.py`. "Hard" means the client
refuses to load or crashes; "soft" means it loads but renders wrongly, or only
works because live Blizzard data never went that far.

## Models

| Limit | Value | Kind | On breach |
|---|---|---|---|
| M2 version | 264 | hard | always written |
| Vertices per skin profile | 65 535 | hard | **fails** — `uint16` indices |
| Skin profiles (LOD levels) | 4 | hard | clamped, extras reported |
| Bones per model | 256 | soft | reported |
| Bones per draw call | 256 | soft | reported |
| Bone influences per vertex | 4 | soft | reported |
| Texture units per batch | 2 | soft | clamped |
| Blend modes | 0–6 | hard | 7 → 4, higher → 2 |
| Material flags | `0x1F` | hard | masked |
| Bone flags | `0x3FF` | hard | masked |
| Header global flags | `0x0B` | hard | masked |
| Texture types | ≤ 15 | hard | treated as hardcoded |
| Particle emitter shapes | plane, sphere | hard | spline/bone → plane |
| Particle flags | `0x0FFFFFFF` | hard | masked |

`--strict` turns the soft limits into failures.

## Textures

| Limit | Value | Kind |
|---|---|---|
| Encodings | palettised, DXT1/3/5, raw BGRA | hard |
| BC5 | not supported | hard — transcoded |
| Mip levels | 16 | hard |
| Dimensions | power of two | hard for mipmapped textures |
| Size | 1024 by default (`--max-texture-size`) | soft — the 32-bit client's texture budget |

## World objects

| Limit | Value | Kind |
|---|---|---|
| WMO version | 17 | hard |
| Vertices per group | 65 535 | hard — **fails** |
| Material shader | 0–6 | hard — higher resets to diffuse |
| Material blend mode | 0–6 | hard — higher resets to alpha |
| Material flags | `0x1FF` | hard |
| `MOHD` flags | `0x0F` | hard |
| `MOGP` flags | `0x07FFFFFF` | hard |
| UV layers per group | 2 | hard |
| Vertex colour layers | 2 | hard |
| Groups per WMO | 512 | soft |

## Terrain

| Limit | Value | Kind |
|---|---|---|
| ADT version | 18, monolithic with `MCIN` | hard |
| Map chunks per tile | 256 | hard |
| Texture layers per chunk | 4 | hard |
| Holes | 4×4 per chunk | hard — 8×8 folded down |
| `MPHD` flags | `0x0F` | hard |
