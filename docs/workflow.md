# From a retail install to a working 3.3.5a patch

An end-to-end walkthrough. Nothing here needs game data to read — but you will
need a legitimate install to actually run it.

## 1. Get a listfile

Modern files reference each other by FileDataID, a bare integer. Turning those
back into the paths a 3.3.5a client opens needs the community listfile:

```bash
curl -LO https://github.com/wowdev/wow-listfile/releases/latest/download/community-listfile.csv
export WOTLKCONV_LISTFILE=$PWD/community-listfile.csv
```

Check it loaded and covers what you need:

```console
$ wotlkconv listfile community-listfile.csv --lookup 1394967
1483920 entries from community-listfile.csv
  .blp       612043
  .m2        210338
  .skin      198877
  ...
  1394967 -> creature/bear/bear.blp
```

## 2. See what the install holds

```console
$ wotlkconv casc info --casc "/games/World of Warcraft"
install    /games/World of Warcraft
product    wow
version    11.0.5.57212
build      WOW-57212patch11.0.5
locale     enus
files      1583921
encoding   1621004 content keys
indices    16 bucket(s), 1204553 entries
keys       0 encryption key(s) loaded
```

`files` counts what the build defines; `indices` counts what is actually on
this machine. A large gap means a partial install streaming from the CDN — the
converter will report those files as unavailable rather than fetching them.

If you need encrypted content, pass a community key ring:

```bash
wotlkconv casc info --casc /games/wow --casc-keys WoW.txt
```

## 3. Decide what to take

Browse by path glob before committing to a conversion:

```console
$ wotlkconv casc list --casc /games/wow --include "creature/bear/**" --limit 20
  1394961  creature/bear/bear.m2
  1394962  creature/bear/bear00.skin
  ...
```

Then dry-run the conversion to see what it would do, and what it would lose:

```bash
wotlkconv convert --casc /games/wow --include "creature/bear/**" \
                  -o out/ --dry-run -v
```

## 4. Convert

```bash
wotlkconv convert --casc /games/wow \
                  --include "creature/bear/**" \
                  -o patch-work/ -j4 --report report.json
```

Companions come along automatically: the model's `.skin` profiles, its external
`.anim` files and its `.skel` skeleton are found by FileDataID, converted, and
renamed into the layout the client globs for.

For a larger slice, put the globs in a file:

```
# bears.txt
creature/bear/**
creature/bearmount/**
world/wmo/azeroth/buildings/**
```

```bash
wotlkconv convert --casc /games/wow --include-from bears.txt -o patch-work/ -j8
```

## 5. Read the report

```console
$ wotlkconv convert ... -v
34 file(s): 21 lossy, 9 passthrough, 3 skipped
  LOSSY creature/bear/bear.m2
      [info] merged external skeleton: 112 bones, 48 sequences
      [lossy] 2 emitter(s) used Cataclysm multi-texture particles; only the
              first texture survives
      [lossy] dropped chunks with no 3.3.5a equivalent: PFID (physics rig)
```

`report.json` carries the same thing with stable `code` fields, so a build
script can fail on the categories it cares about:

```bash
jq -r '.files[] | select(.status=="failed") | .source' report.json
jq -r '.files[].notes[]? | select(.code=="m2.limit.bones") | .message' report.json
```

Two failures are worth acting on rather than working around:

- **`m2.limit.vertices` / `skin.limit.vertices` / `wmo.group.indices`** — the
  mesh needs splitting in a model editor. 16-bit indices are a format limit,
  not a policy.
- **`m2.skeleton.missing`** — the `.skel` was not available. When converting
  from `--casc` this should not happen; from a manual extraction it means the
  skeleton was not extracted.

## 6. Fix up references

Converted files keep their retail paths, which will collide with anything the
3.3.5a client already has at those paths. Two options:

```bash
# Namespace everything under your own directory
wotlkconv convert ... --path-prefix "custom\\mypatch"

# Or keep retail paths deliberately, to replace the original art
wotlkconv convert ...
```

`--path-prefix` rewrites every reference *inside* the files too — model texture
paths, WMO texture and doodad tables, terrain texture lists — so the set stays
self-consistent.

## 7. Build the patch archive

3.3.5a loads `Data/patch-4.MPQ` … `patch-9.MPQ` (and locale variants under
`Data/enUS/`). Pack the output tree with an MPQ tool — [MPQEditor], `mpqtool`,
or [StormLib] — preserving the directory structure:

```
patch-4.MPQ
└── creature/
    └── bear/
        ├── bear.m2
        ├── bear00.skin
        ├── bear0000-00.anim
        └── bear.blp
```

Paths inside an MPQ are backslash-separated and case-insensitive; most tools
handle the conversion from a directory tree for you.

## 8. Test in a client

This is the step nothing here substitutes for. Load the patch, look at the
assets, and if something renders wrong, find it in `report.json` — the note
codes say which transformation touched that file.

---

## Terrain is a bigger job

Porting a whole zone needs more than the ADTs:

```bash
wotlkconv convert --casc /games/wow \
                  --include "world/maps/azeroth/**" -o patch-work/
```

That converts the `.wdt` and every tile, merging each tile's `_tex0` and
`_obj0` pieces back together. **Convert the `.wdt` in the same run** — it
carries the big-alpha flag without which every terrain blend renders wrong.

What it does not do:

- **Map and area records live in DBCs.** `Map.dbc`, `AreaTable.dbc`,
  `WMOAreaTable.dbc` and friends have to be edited separately; this tool does
  not convert `.db2` to `.dbc`.
- **Server-side data** — spawns, navmeshes, area triggers — is outside its
  scope entirely.
- **Coordinates and scale** are unchanged between expansions, but a zone built
  for modern draw distances will still look wrong at 3.3.5a's.

## Converting from a manual extraction

If you already have files on disk (from `wow.export`, CASCExplorer, or an older
extraction), point the converter at the directory instead of `--casc`:

```bash
wotlkconv convert extracted/ -o patch-work/ -l listfile.csv -j4
```

It handles all three common layouts — files named by FileDataID, files mirroring
the in-game path tree, and everything flat in one directory — and finds
companions in any of them. Add `-s DIR` for extra places to look.

[MPQEditor]: http://www.zezula.net/en/mpq/download.html
[StormLib]: https://github.com/ladislav-zezula/StormLib
