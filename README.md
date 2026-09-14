# wotlkconv

Convert modern World of Warcraft assets into files the **3.3.5a (Wrath of the
Lich King, build 12340)** client can load.

Retail assets and 3.3.5a assets look superficially alike — an `.m2` is still an
`.m2`, a `.wmo` still claims version 17 — but almost everything about how they
reference each other changed. Legion wrapped models in a chunk container and
replaced every filename with a numeric FileDataID; Cataclysm split terrain
tiles into four files and dropped the chunk index Wrath needs; BfA moved WMO
texture and doodad names out of the file entirely. Copying a modern file into a
3.3.5a patch archive does not work, and usually does not fail cleanly either.

`wotlkconv` reads a game install directly, performs the structural downgrade,
resolves the numeric references back into paths, and tells you exactly what it
had to throw away.

```
wotlkconv convert --casc "C:\World of Warcraft" --include "creature/**" \
                  -o patch-4/ --listfile listfile.csv -j4
```

## What it converts

| Format | From | To | Notes |
|---|---|---|---|
| `.m2` | version 264–274, `MD21`-chunked | version 264, flat `MD20` | merges `.skel` skeletons back in, resolves `TXID` textures |
| `.skin` | Legion 56-byte header | Wrath 48-byte header | drops shadow batches, clamps batch limits |
| `.anim` | `AFM2`-chunked | flat blob | drops physics bone tracks |
| `.skel` | Legion+ | *(merged into the model)* | including `SKPD` parent chains |
| `.blp` | BLP2, any encoding incl. BC5 | DXT1/3/5, palettised or raw | already-valid block data is copied byte for byte |
| `.wmo` root | Legion/BfA/Shadowlands | 3.3.5a v17 | rebuilds `MOTX`/`MODN`/`MOSB` from `MODI`/`MOSI` |
| `.wmo` group | Shadowlands `MOVX`/`MPY2` | `MOVI`/`MOPY` | recomputes batch bounds, trims UV/colour layers |
| `.adt` | Cataclysm+ split tiles | monolithic Wrath tile | rebuilds `MCIN`, merges `_tex0` and `_obj0` |
| `.wdt` | BfA+ with `MAID` | 3.3.5a v17 | drops `MAID`, sets the big-alpha flag |
| `.wdl` | Legion+ with the `ML*` LOD mesh | 3.3.5a heightmap | keeps `MAOF`/`MARE`/`MAHO`, rewrites every offset |
| `.db2` | WDC1–WDC5 (Legion 7.3 onwards) | `.dbc` | needs a DBD definition; the layout comes from the definition too |

Companion files are found automatically and renamed into the layout the client
globs for — `Bear.m2` gets `Bear00.skin` … `Bear03.skin` and
`Bear0000-00.anim`, a WMO root gets `Root_000.wmo` … — regardless of how the
inputs were named.

## What it does with everything else

A patch archive needs more than the art. Every file is put in one of three
buckets, and the report says which:

- **Converted** — the formats in the table above.
- **Copied through, byte for byte** — formats 3.3.5a reads unchanged: `.wav`,
  `.mp3`, `.ogg`, `.avi`, `.lua`, `.xml`, `.toc`, `.ttf`, and anything with an
  unfamiliar extension (carried through and flagged rather than dropped).
  `--no-copy-unconverted` turns this off.
- **Skipped, with the actual reason** — `.tex` streamed texture payloads,
  `.phys` physics rigs, `.bone` overrides. `.skel` files are skipped because
  they are merged into the model that references them, and `.db2` databases
  because they need a definition (see below).

Nothing is silently discarded.

## Client databases

A converted model is inert until something points at it, and what points at it
is a row in `CreatureDisplayInfo` or `GameObjectDisplayInfo`. Those live in
`.db2` files whose format *and* layout both changed completely, so converting
them needs two things you supply:

```bash
git clone https://github.com/wowdev/WoWDBDefs
wotlkconv db convert CreatureDisplayInfo.db2 -o out/ \
    --dbd WoWDBDefs/definitions \
    --listfile listfile.csv \
    --template-dir /my/335a/client/dbc \
    --id-offset 200000
```

- **`--dbd`** gives the columns names — on *both* sides. A `.db2` carries none,
  and the community definitions supply them, matched on the file's own layout
  hash; the same definitions also carry a `BUILD 3.3.5.12340` layout naming
  every column that build's table had, in order, which is where the shape of
  the `.dbc` being written comes from. Without it a conversion refuses rather
  than mapping columns by position.
- **`--template-dir`** points at *your* client's `.dbc` files. Each table's own
  file verifies the layout and is merged onto, so the rows you already have
  survive and the new ones are appended.
- **`--id-offset`** moves converted ids clear of Blizzard's, so a new display id
  does not overwrite an existing creature.

Merging keeps the template's records and string block byte for byte and appends
to them, so existing string offsets stay valid and the tool never has to guess
the type of a field it is not writing.

Databases older than WDC1 (Cataclysm through Legion 7.2) are **refused rather
than read**: they lay records out differently enough that reading one as a WDC
would produce plausible wrong values instead of an error. Every build that
ships assets worth converting is WDC1 or later.

Because both sides are named, most columns map themselves: a modern
`CollisionHeight` and a Wrath `CollisionHeight` are the same field, matched by
name and type. A mapping file only has to describe what actually changed — the
FileDataIDs that used to be paths, and the handful Blizzard renamed — so a
table with no mapping at all still converts. Any 3.3.5a column left without a
source is reported by name rather than quietly written as zero.

`wotlkconv db tables` lists the built-in mappings and what each one rewrites.
They are plain JSON — `--db-mappings DIR` overrides any of them, and adding a
table means writing one file, not changing code.

A `--template` is still worth passing: it cross-checks the derived layout and
hard-fails on disagreement. It cannot *replace* the definition, though — a
`.dbc` records how many columns there are, never what belongs in them, so a
table the definitions do not cover for 3.3.5a is refused rather than filled in
by position. (A mapping can pin columns by `index` if you have to force it.)

## Meshes too big for 16-bit indices

Both formats index vertices with `uint16`, so 65 535 is a hard ceiling. Modern
assets pass it, and the tool handles that rather than refusing the file:

- **WMO groups** are split into several groups and the root is updated to
  reference them — group count, bounding boxes, names and all. Each part gets
  its own render batches and a freshly built collision tree, because a split
  renumbers the triangles the original tree indexed. This is self-contained and
  on by default; `--no-split-groups` turns it back into an error.
- **Models** are first *compacted*: geometry no submesh draws is dropped, which
  is lossless and usually enough on its own. A model still too large after that
  can be split into several `.m2` files sharing one rig with `--split-models`.
  That is opt-in, because the extra pieces are new assets nothing references
  yet — you have to place them.

## Reading a game install

Point `--casc` at the folder containing the game executable and its `Data`
directory, and select what you want by in-game path:

```bash
# Everything under one creature directory
wotlkconv convert --casc "C:\World of Warcraft" -l listfile.csv \
                  --include "creature/bear/**" -o out/

# Every model in the build, four processes
wotlkconv convert --casc /games/wow -l listfile.csv \
                  --include "**/*.m2" -o out/ -j4

# A specific file, no listfile needed
wotlkconv convert --casc /games/wow --fileid 1394967 -o out/

# What build is this, and what is in it?
wotlkconv casc info --casc /games/wow
wotlkconv casc list --casc /games/wow -l listfile.csv --include "world/wmo/**"

# Pull files out without converting them
wotlkconv casc extract --casc /games/wow -l listfile.csv \
                       --include "sound/**" -o raw/
```

Selection is deliberately explicit: `--include "**"` takes the whole build, and
a modern build is millions of files.

Only **local** storage is read. Modern installs can be partial, streaming the
rest from Blizzard's CDN on demand; files that are not on disk are reported as
unavailable rather than downloaded. Encrypted files (Blizzard ships unreleased
content that way) decode if you pass `--casc-keys` a community key ring, and
are reported per file if you do not.

`--casc-product` picks between products in a multi-product install
(`wow`, `wow_classic`, `wowt`, …) and `--casc-locale` chooses which localised
variant to prefer.

## Install

Python 3.10 or newer. There are no third-party dependencies.

```bash
git clone https://github.com/Bar3b0n3s/Converter
cd Converter
pip install -e .          # or: PYTHONPATH=src python -m wotlkconv
```

## You will want a listfile

Modern files name their textures, models and skeletons by FileDataID — a bare
integer. Nothing inside the file says what `1234567` is, and the 3.3.5a client
has no FileDataID table at all: it opens paths out of MPQ archives. Turning
those numbers back into paths needs the community listfile:

```bash
curl -LO https://github.com/wowdev/wow-listfile/releases/latest/download/community-listfile.csv
wotlkconv convert ... --listfile community-listfile.csv
```

Set `WOTLKCONV_LISTFILE` instead of passing the flag every time, or drop a
`listfile.csv` in the working directory and it is found automatically.

Without one, textures and doodads are pointed at `unresolved\blp\1234567.blp`
placeholder paths: the files still load, but you have to repoint the references
yourself. `--unresolved fail` turns that into an error instead, and
`--unresolved strip` empties the reference.

## Usage

```
wotlkconv convert IN... -o OUT   downgrade assets (from disk, or --casc)
wotlkconv inspect FILE...        what is this file, and will 3.3.5a load it?
wotlkconv plan IN...             what would a convert run do?
wotlkconv casc info|list|extract read a game install
wotlkconv db convert|tables      turn .db2 databases into 3.3.5a .dbc
wotlkconv listfile PATH          sanity-check a listfile
```

### Everyday runs

```bash
# One model. Its skins, animations and skeleton are picked up from the same
# directory, whether they are named by path or by FileDataID.
wotlkconv convert Bear.m2 -o out/ -l listfile.csv

# A whole extraction, four processes.
wotlkconv convert extracted/ -o patch-4/ -l listfile.csv -j4

# See what it would do, and why, before it writes anything.
wotlkconv convert extracted/ -o out/ --dry-run -v

# Machine-readable results for a build script.
wotlkconv convert extracted/ -o out/ --report report.json
```

### Inspecting

```console
$ wotlkconv inspect Bear.m2
Bear.m2  [m2]  184320 bytes
    version: 272
    chunked: True
    bones: 148
    textures: 3
    texture_file_ids: [1394967, 1394969, 1394971]
    skeleton_file_id: 1394961
    wotlk_compatible: False
```

`--json` gives the same thing as JSON.

### Useful flags

| Flag | Effect |
|---|---|
| `--casc DIR` | read a game install directly |
| `--include GLOB` / `--fileid N` | what to take out of it (repeatable) |
| `--casc-keys PATH` | TACT key ring for encrypted files |
| `-l, --listfile PATH` | FileDataID → path mapping |
| `-s, --search-dir DIR` | extra place to look for skins, skeletons and animations |
| `--path-prefix PATH` | prefix every rewritten path, e.g. `custom\mypatch` |
| `--texture-format` | `auto` (default), `dxt1`, `dxt3`, `dxt5`, `pal`, `raw`, `keep` |
| `--max-texture-size N` | downscale above N pixels (default 1024, `0` disables) |
| `--allow-npot` | keep non-power-of-two dimensions instead of resizing |
| `--strip-particles` etc. | drop particles, ribbons, cameras or lights |
| `--allow-missing-skeleton` | convert a rigged model without its `.skel`, producing a static one |
| `--keep-numeric-names` | keep `1234567.m2` instead of renaming to the listfile path |
| `--strict` | treat exceeding a 3.3.5a soft limit as an error |
| `-j N` | convert N files in parallel |
| `-n, --dry-run` | convert but write nothing |
| `--no-copy-unconverted` | drop sound/interface files instead of copying them |
| `--dbd DIR` | DBD definitions, which also lets `.db2` join a convert run |
| `--template-dir DIR` | your client's `.dbc` files, to verify and merge onto |
| `--id-offset N` | shift converted database ids clear of existing ones |
| `--no-split-groups` | fail on an oversized WMO group instead of splitting it |
| `--split-models` | split an oversized model into several `.m2` files |

## Downgrading is lossy, and it says so

Every file gets a status — `ok`, `passthrough`, `lossy`, `skipped` or `failed`
— and every discarded feature is recorded with a stable code you can grep for:

```console
$ wotlkconv convert Bear.m2 -o out/ -l listfile.csv -v
  LOSSY Bear.m2
      [info] merged external skeleton: 148 bones, 61 sequences, 27 attachments
      [info] resolved 3 texture FileDataID(s) to paths
      [lossy] 2 emitter(s) used Cataclysm multi-texture particles; only the
              first texture survives
      [lossy] 1 material(s) used a blend mode newer than 3.3.5a; mapped to the
              nearest supported mode
      [lossy] dropped chunks with no 3.3.5a equivalent: PFID (physics rig),
              LDV1 (level-of-detail data)
```

`passthrough` means the input was already valid for 3.3.5a and was copied
without re-encoding — BLP block data in particular is preserved bit for bit, so
running a file through twice never degrades it.

Some things genuinely cannot be carried across, and the converter fails loudly
rather than writing something that crashes the client:

- **A rigged Legion+ model whose `.skel` is missing.** Its bones and animations
  are in that file. Extract it alongside the model, or accept a static model
  with `--allow-missing-skeleton`.
- **A model still past 65 535 vertices after unused geometry is dropped.**
  Pass `--split-models`, or split it in a model editor.
- **A database whose mapping disagrees with your client's table.** Fix the
  mapping rather than shipping a misaligned `.dbc`.

Others are reported as warnings because the file loads but may look wrong: more
than 256 bones, more than 256 bones in one draw call, normal maps (3.3.5a has
no normal-mapped shader path at all, so a transcoded BC5 map is only useful as
a diffuse or overlay).

## Documentation

- [`docs/formats.md`](docs/formats.md) — what changed in each format, field by
  field, and the assumptions this tool makes where the format is ambiguous
- [`docs/workflow.md`](docs/workflow.md) — extracting from retail, converting,
  and packaging a 3.3.5a patch archive
- [`docs/limits.md`](docs/limits.md) — the 3.3.5a limits being enforced

## Using it as a library

```python
from wotlkconv.listfile import Listfile
from wotlkconv.m2 import convert_m2
from wotlkconv.options import Options

listfile = Listfile.load("listfile.csv")
data, result, companions = convert_m2(
    open("Bear.m2", "rb").read(), "Bear.m2", Options(), listfile)

print(result.status, [n.code for n in result.notes])
for c in companions:
    print(c.filename, len(c.data))
```

Each format module exposes a matching `convert_*`/`inspect_*` pair, and every
converter is a pure function of bytes, so they parallelise without shared state.

## Development

```bash
python -m pytest tests/ -q      # 404 tests, no network or game data needed
python -m ruff check src tests
```

The tests build synthetic assets that match the modern wire formats byte for
byte (`tests/fixtures.py`), a complete synthetic CASC install — archives,
indices, encoding and root tables and all (`tests/casc_fixtures.py`) — and
synthetic `.db2` tables covering every field storage type
(`tests/db_fixtures.py`), then push them through the real converters. The M2 fixtures serialise through the
production writer with version-272 schemas, so the parser and writer cannot
drift apart without a test failing. The ciphers are checked against published
test vectors.

**What that does and does not prove.** The test suite verifies structure:
struct sizes against the documented wire formats, offsets that resolve to the
chunks they claim, references that survive a round trip, and features that get
clamped to the documented 3.3.5a ranges. It does not run a 3.3.5a client, and
the CASC reader has not been run against a real retail install — only against
fixtures built from the format description. Before shipping converted assets,
load them in a client. If something renders wrong, the `--report` JSON tells
you which transformation touched it. The same caveat applies twice over to the
built-in database mappings, which is why the tool presses for a template.

A few places where the published format documentation is ambiguous are called
out explicitly in [`docs/formats.md`](docs/formats.md#assumptions) — those are
the first things to check if a converted file misbehaves.

## Legal

This tool converts files you already have. It ships no Blizzard data, and you
need a legitimate copy of the game to extract anything to convert. World of
Warcraft is a trademark of Blizzard Entertainment; this project is not
affiliated with or endorsed by Blizzard.

MIT licensed — see [LICENSE](LICENSE).
