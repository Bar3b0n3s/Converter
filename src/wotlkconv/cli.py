"""Command line interface.

    wotlkconv convert IN... -o OUT   downgrade assets into OUT
    wotlkconv inspect FILE...        report what a file is and whether 3.3.5a
                                     can load it
    wotlkconv plan IN...             list what a convert run would do
    wotlkconv listfile PATH          sanity-check a community listfile
    wotlkconv casc info|list|extract read a game install directly
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, detect, log
from .adt import inspect_adt, inspect_wdt
from .blp import inspect_blp
from .casc import CascStorage, KeyRing
from .errors import ConverterError
from .listfile import ENV_VAR, Listfile, to_posix
from .limits import BLP_SOFT_MAX_DIMENSION, TARGET_BUILD, TARGET_PATCH
from .m2 import inspect_anim, inspect_m2, inspect_skin
from .options import Options, TextureFormat, UnresolvedPolicy
from .pipeline import normalise_pattern, plan, plan_casc, run
from .wmo import inspect_group, inspect_wmo_root

_INSPECTORS = {
    detect.M2: inspect_m2,
    detect.SKIN: inspect_skin,
    detect.ANIM: inspect_anim,
    detect.BLP: inspect_blp,
    detect.WMO_ROOT: inspect_wmo_root,
    detect.WMO_GROUP: inspect_group,
    detect.ADT: inspect_adt,
    detect.WDT: inspect_wdt,
}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wotlkconv",
        description=f"Convert modern World of Warcraft assets to "
                    f"{TARGET_PATCH} (build {TARGET_BUILD}).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # one model, with its skins and animations picked up automatically
  wotlkconv convert Bear.m2 -o out/ --listfile listfile.csv

  # a whole extraction tree, four at a time
  wotlkconv convert extracted/ -o patch-4/ --listfile listfile.csv -j4

  # see what would happen, and why, without writing anything
  wotlkconv convert extracted/ -o out/ --dry-run -v

  # is this file already loadable by 3.3.5a?
  wotlkconv inspect Bear.m2 --json
""")
    parser.add_argument("--version", action="version",
                        version=f"wotlkconv {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="count", default=0,
                        help="show per-file detail; repeat for debug output")
    common.add_argument("-q", "--quiet", action="store_true",
                        help="only report errors")
    common.add_argument("--no-color", action="store_true",
                        help="disable coloured output")

    casc_opts = argparse.ArgumentParser(add_help=False)
    cg = casc_opts.add_argument_group("game install (CASC)")
    cg.add_argument("--casc", metavar="DIR",
                    help="read straight out of a game install: the folder "
                         "containing the game executable and Data/")
    cg.add_argument("--casc-product", metavar="NAME",
                    help="which product in .build.info to read (wow, "
                         "wow_classic, wowt, ...); default is the active one")
    cg.add_argument("--casc-locale", default="enus", metavar="LOCALE",
                    help="locale to prefer for localised files "
                         "(default: %(default)s)")
    cg.add_argument("--casc-keys", metavar="PATH",
                    help="TACT encryption keys (a WoW.txt of "
                         "'<keyname> <key>' lines) for encrypted files")

    sub = parser.add_subparsers(dest="command", required=True)

    # -- convert --------------------------------------------------------
    conv = sub.add_parser("convert", parents=[common, casc_opts],
                          help="downgrade assets for 3.3.5a")
    conv.add_argument("inputs", nargs="*", metavar="IN",
                      help="files or directories to convert; omit when reading "
                           "from --casc")
    conv.add_argument("-o", "--out", required=True, metavar="DIR",
                      help="destination directory")
    conv.add_argument("--no-recursive", action="store_true",
                      help="do not descend into subdirectories")

    refs = conv.add_argument_group("asset references")
    refs.add_argument("-l", "--listfile", metavar="PATH",
                      help=f"community listfile mapping FileDataIDs to paths "
                           f"(or set ${ENV_VAR})")
    refs.add_argument("-s", "--search-dir", action="append", default=[],
                      metavar="DIR",
                      help="extra directory to search for skins, skeletons and "
                           "animations; repeatable")
    refs.add_argument("--unresolved", choices=[p.value for p in UnresolvedPolicy],
                      default=UnresolvedPolicy.PLACEHOLDER.value,
                      help="what to do with a FileDataID the listfile does not "
                           "cover (default: %(default)s)")
    refs.add_argument("--path-prefix", default="", metavar="PATH",
                      help=r"prefix every rewritten in-game path, e.g. custom\mypatch")
    refs.add_argument("--keep-numeric-names", action="store_true",
                      help="keep FileDataID filenames instead of renaming "
                           "outputs to their listfile path")
    refs.add_argument("--flatten", action="store_true",
                      help="write every output into the destination root")

    sel = conv.add_argument_group("selection (with --casc)")
    sel.add_argument("--include", action="append", default=[], metavar="GLOB",
                     help=r"in-game path glob to extract, e.g. "
                          r"'creature/bear/**' or '**/*.m2'; repeatable")
    sel.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                     help="in-game path glob to leave out; repeatable")
    sel.add_argument("--include-from", metavar="PATH",
                     help="file of globs, one per line (# comments allowed)")
    sel.add_argument("--fileid", action="append", default=[], type=int,
                     metavar="ID", help="extract one FileDataID; repeatable")

    tex = conv.add_argument_group("textures")
    tex.add_argument("--texture-format", choices=[f.value for f in TextureFormat],
                     default=TextureFormat.AUTO.value,
                     help="output encoding (default: %(default)s -- DXT1 when "
                          "opaque, DXT5 when the alpha channel needs gradients)")
    tex.add_argument("--max-texture-size", type=int,
                     default=BLP_SOFT_MAX_DIMENSION, metavar="N",
                     help="downscale textures larger than N pixels; 0 disables "
                          "(default: %(default)s)")
    tex.add_argument("--allow-npot", action="store_true",
                     help="keep non-power-of-two dimensions instead of resizing")
    tex.add_argument("--dxt1-alpha-cutoff", type=int, default=128, metavar="N",
                     help="alpha below N becomes transparent in DXT1 "
                          "punch-through blocks (default: %(default)s)")

    mdl = conv.add_argument_group("models")
    mdl.add_argument("--strip-particles", action="store_true",
                     help="remove particle emitters")
    mdl.add_argument("--strip-ribbons", action="store_true",
                     help="remove ribbon emitters")
    mdl.add_argument("--strip-cameras", action="store_true",
                     help="remove cameras")
    mdl.add_argument("--strip-lights", action="store_true",
                     help="remove lights")
    mdl.add_argument("--allow-missing-skeleton", action="store_true",
                     help="convert a model whose .skel could not be found, "
                          "producing a static model with no animations")
    mdl.add_argument("--no-companions", action="store_true",
                     help="convert only the named files, not their .skin, "
                          ".anim or WMO group companions")
    mdl.add_argument("--strict", action="store_true",
                     help="treat exceeding a 3.3.5a soft limit as an error")
    mdl.add_argument("--no-merge-adt", action="store_true",
                     help="do not merge split terrain tiles")
    mdl.add_argument("--no-copy-unconverted", action="store_true",
                     help="drop formats 3.3.5a reads unchanged (sound, "
                          "interface, fonts) instead of copying them through")

    outg = conv.add_argument_group("output")
    outg.add_argument("-j", "--jobs", type=int, default=1, metavar="N",
                      help="convert N files in parallel (default: %(default)s)")
    outg.add_argument("-f", "--overwrite", action="store_true",
                      help="replace files that already exist")
    outg.add_argument("-n", "--dry-run", action="store_true",
                      help="convert but write nothing")
    outg.add_argument("--report", metavar="PATH",
                      help="write a machine-readable JSON report")

    # -- inspect --------------------------------------------------------
    insp = sub.add_parser("inspect", parents=[common],
                          help="describe files and their 3.3.5a compatibility")
    insp.add_argument("inputs", nargs="+", metavar="FILE")
    insp.add_argument("--json", action="store_true", help="emit JSON")

    # -- plan -----------------------------------------------------------
    pl = sub.add_parser("plan", parents=[common],
                        help="list the work a convert run would do")
    pl.add_argument("inputs", nargs="+", metavar="IN")
    pl.add_argument("--no-recursive", action="store_true")

    # -- casc -----------------------------------------------------------
    casc = sub.add_parser("casc", parents=[common, casc_opts],
                          help="inspect or extract from a game install")
    casc_sub = casc.add_subparsers(dest="casc_command", required=True)
    casc_sub.add_parser("info", parents=[common, casc_opts],
                        help="show what build the install holds")
    cl = casc_sub.add_parser("list", parents=[common, casc_opts],
                             help="list files matching a path glob")
    cl.add_argument("--include", action="append", default=[], metavar="GLOB")
    cl.add_argument("-l", "--listfile", metavar="PATH")
    cl.add_argument("--limit", type=int, default=100, metavar="N")
    ce = casc_sub.add_parser("extract", parents=[common, casc_opts],
                             help="extract files without converting them")
    ce.add_argument("-o", "--out", required=True, metavar="DIR")
    ce.add_argument("--include", action="append", default=[], metavar="GLOB")
    ce.add_argument("--fileid", action="append", default=[], type=int,
                    metavar="ID")
    ce.add_argument("-l", "--listfile", metavar="PATH")
    ce.add_argument("-f", "--overwrite", action="store_true")

    # -- listfile -------------------------------------------------------
    lf = sub.add_parser("listfile", parents=[common],
                        help="sanity-check a community listfile")
    lf.add_argument("path", metavar="PATH")
    lf.add_argument("--lookup", action="append", default=[], metavar="ID_OR_PATH",
                    help="resolve a FileDataID or path; repeatable")

    return parser


def _configure_logging(args: argparse.Namespace) -> None:
    if args.no_color:
        log.set_colour(False)
    if args.quiet:
        log.set_level(log.ERROR)
    elif args.verbose >= 2:
        log.set_level(log.DEBUG)
    else:
        log.set_level(log.INFO)


def _options_from(args: argparse.Namespace) -> Options:
    return Options(
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        jobs=max(1, args.jobs),
        unresolved=UnresolvedPolicy(args.unresolved),
        path_prefix=args.path_prefix,
        name_from_listfile=not args.keep_numeric_names,
        flatten=args.flatten,
        texture_format=TextureFormat(args.texture_format),
        max_texture_size=max(0, args.max_texture_size),
        force_power_of_two=not args.allow_npot,
        dxt1_alpha_cutoff=args.dxt1_alpha_cutoff,
        strict_limits=args.strict,
        strip_particles=args.strip_particles,
        strip_ribbons=args.strip_ribbons,
        strip_cameras=args.strip_cameras,
        strip_lights=args.strip_lights,
        allow_missing_skeleton=args.allow_missing_skeleton,
        convert_companions=not args.no_companions,
        merge_split_adt=not args.no_merge_adt,
        copy_unconverted=not args.no_copy_unconverted,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _read_globs(path: str | None) -> list[str]:
    if not path:
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _open_casc(args: argparse.Namespace, search_dirs) -> tuple[object, dict]:
    """Open the install named by --casc and describe it for worker processes."""
    keys = KeyRing.discover(getattr(args, "casc_keys", None),
                            [Path(args.casc), *search_dirs])
    storage = CascStorage.open(args.casc,
                               product=getattr(args, "casc_product", None),
                               locale=getattr(args, "casc_locale", "enus"),
                               keys=keys)
    casc_args = {
        "path": str(args.casc),
        "product": getattr(args, "casc_product", None),
        "locale": getattr(args, "casc_locale", "enus"),
        "keys": keys.sources[0] if keys.sources else None,
    }
    return storage, casc_args


def cmd_convert(args: argparse.Namespace) -> int:
    opts = _options_from(args)
    search_dirs = [Path(d) for d in args.search_dir]
    listfile = Listfile.discover(args.listfile, search_dirs)
    if not listfile:
        log.warn("no listfile loaded: modern assets reference textures and "
                 "models by FileDataID, and without one those references "
                 "cannot be turned back into paths (see --listfile)")

    if not args.inputs and not args.casc:
        log.error("nothing to convert: give input paths, or --casc to read a "
                  "game install directly")
        return 1

    jobs = []
    skipped = []
    storage = None
    casc_args = None

    if args.casc:
        includes = list(args.include) + _read_globs(args.include_from)
        if not includes and not args.fileid:
            log.error("--casc needs a selection: --include with an in-game path "
                      "glob, --include-from with a file of them, or --fileid. "
                      "Use --include '**' to take the whole build, but expect "
                      "millions of files")
            return 1
        if includes and not listfile:
            log.error("--include matches against in-game paths, which need a "
                      "listfile; pass --listfile, or select by --fileid instead")
            return 1
        storage, casc_args = _open_casc(args, search_dirs)
        cjobs, cskipped = plan_casc(
            storage, listfile, include=includes, file_ids=args.fileid,
            exclude=args.exclude, claim_companions=not args.no_companions,
            copy_unconverted=not args.no_copy_unconverted)
        jobs += cjobs
        skipped += cskipped

    if args.inputs:
        fjobs, fskipped = plan(args.inputs, recursive=not args.no_recursive,
                               claim_companions=not args.no_companions,
                               copy_unconverted=not args.no_copy_unconverted)
        jobs += fjobs
        skipped += fskipped

    if not jobs:
        log.error("nothing to convert")
        for res in skipped[:20]:
            log.info(f"  skipped {res.source}: "
                     f"{res.notes[0].message if res.notes else 'unknown reason'}")
        return 1

    roots = [str(Path(i)) for i in args.inputs] + [str(d) for d in search_dirs]
    log.info(f"converting {len(jobs)} file(s) to {args.out}"
             + (" (dry run)" if opts.dry_run else ""))

    report = run(jobs, opts, listfile, args.out, roots=roots,
                 listfile_path=listfile.source if listfile else None,
                 skipped=skipped, storage=storage, casc_args=casc_args)
    if storage is not None:
        storage.close()

    for line in report.summary_lines(verbose=args.verbose >= 1):
        print(line)
    if args.report:
        report.write_json(args.report)
        log.info(f"wrote report to {args.report}")

    lossy = len(report.lossy)
    if lossy and args.verbose < 1:
        print(f"{lossy} file(s) converted with losses; re-run with -v (or "
              f"--report) to see what changed")
    return 1 if report.failed else 0


def cmd_inspect(args: argparse.Namespace) -> int:
    results = []
    exit_code = 0
    for raw in args.inputs:
        path = Path(raw)
        try:
            data = path.read_bytes()
        except OSError as exc:
            log.error(f"{path}: {exc}")
            exit_code = 1
            continue
        kind = detect.detect(data, str(path))
        entry: dict = {"file": str(path), "detected": kind, "bytes": len(data)}
        inspector = _INSPECTORS.get(kind)
        if inspector is None:
            entry["error"] = "unrecognised format"
            exit_code = 1
        else:
            try:
                entry.update(inspector(data, str(path)))
            except ConverterError as exc:
                entry["error"] = str(exc)
                exit_code = 1
        results.append(entry)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return exit_code

    for entry in results:
        print(f"{entry['file']}  [{entry.get('kind', entry['detected'])}]"
              f"  {entry['bytes']} bytes")
        for key, value in entry.items():
            if key in ("file", "bytes", "kind", "detected") or value is None:
                continue
            print(f"    {key}: {value}")
    return exit_code


def cmd_plan(args: argparse.Namespace) -> int:
    jobs, skipped = plan(args.inputs, recursive=not args.no_recursive)
    by_kind: dict[str, int] = {}
    for job in jobs:
        by_kind[job.kind] = by_kind.get(job.kind, 0) + 1
        print(f"  {job.kind:9} {job.describe()}")
    for res in skipped:
        reason = res.notes[0].message if res.notes else "skipped"
        print(f"  {'skip':9} {res.source} -- {reason}")
    total = ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
    print(f"{len(jobs)} job(s): {total}" if jobs else "nothing to convert")
    return 0


def cmd_casc(args: argparse.Namespace) -> int:
    import fnmatch

    search_dirs = [Path(args.casc)]
    storage, _ = _open_casc(args, search_dirs)
    try:
        if args.casc_command == "info":
            stats = storage.stats()
            print(f"install    {args.casc}")
            print(f"product    {stats.product}")
            print(f"version    {stats.version}")
            print(f"build      {stats.build}")
            print(f"locale     {stats.locale}")
            print(f"files      {stats.files}")
            print(f"encoding   {stats.encoding_entries} content keys")
            print(f"indices    {stats.index_buckets} bucket(s), "
                  f"{storage.index.total_entries()} entries")
            print(f"keys       {stats.keys} encryption key(s) loaded")
            return 0

        listfile = Listfile.discover(getattr(args, "listfile", None), search_dirs)
        patterns = [normalise_pattern(p) for p in (args.include or ["**"])]

        if args.casc_command == "list":
            if not listfile:
                log.error("listing by path needs a listfile (--listfile)")
                return 1
            shown = 0
            for file_id in sorted(storage.file_ids()):
                path = listfile.path_for(file_id)
                if path is None or not any(fnmatch.fnmatch(path, p)
                                           for p in patterns):
                    continue
                print(f"{file_id:>9}  {path}")
                shown += 1
                if shown >= args.limit:
                    print(f"... stopping at --limit {args.limit}")
                    break
            if not shown:
                print("no files matched")
            return 0

        # extract
        out_dir = Path(args.out)
        selected: dict[int, str] = {fid: listfile.path_for(fid) or
                                    f"unknown/{fid}.bin" for fid in args.fileid}
        if args.include:
            if not listfile:
                log.error("selecting by path needs a listfile (--listfile)")
                return 1
            for file_id in storage.file_ids():
                path = listfile.path_for(file_id)
                if path and any(fnmatch.fnmatch(path, p) for p in patterns):
                    selected[file_id] = path
        if not selected:
            log.error("nothing selected: pass --include or --fileid")
            return 1

        written = failed = 0
        for file_id, path in sorted(selected.items(), key=lambda kv: kv[1]):
            data, why = storage.try_read_file_id(file_id)
            if data is None:
                log.warn(f"{path}: {why}")
                failed += 1
                continue
            target = out_dir / to_posix(path)
            if target.exists() and not args.overwrite:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written += 1
        print(f"extracted {written} file(s) to {out_dir}"
              + (f", {failed} unavailable" if failed else ""))
        return 1 if failed and not written else 0
    finally:
        storage.close()


def cmd_listfile(args: argparse.Namespace) -> int:
    listfile = Listfile.load(args.path)
    print(f"{len(listfile)} entries from {listfile.source}")
    by_ext: dict[str, int] = {}
    for _fdid, path in listfile:
        ext = os.path.splitext(path)[1] or "(none)"
        by_ext[ext] = by_ext.get(ext, 0) + 1
    for ext, count in sorted(by_ext.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {ext:10} {count}")
    for query in args.lookup:
        if query.isdigit():
            hit = listfile.path_for(int(query))
            print(f"  {query} -> {hit or '(not found)'}")
        else:
            hit = listfile.id_for(query)
            print(f"  {query} -> {hit if hit is not None else '(not found)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args)

    handlers = {
        "convert": cmd_convert,
        "inspect": cmd_inspect,
        "plan": cmd_plan,
        "listfile": cmd_listfile,
        "casc": cmd_casc,
    }
    try:
        return handlers[args.command](args)
    except ConverterError as exc:
        log.error(str(exc))
        return 1
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
