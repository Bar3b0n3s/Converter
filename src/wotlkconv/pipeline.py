"""Batch conversion: discovery, output naming and execution.

The pipeline turns a pile of paths into a list of jobs and runs them.  Two
things make that less trivial than it sounds:

* **Terrain arrives in pieces.**  ``Zone_32_48.adt``, ``_tex0`` and ``_obj0``
  are one logical tile and have to be converted together, so they are grouped
  by base name before any work starts.
* **Extractions are named inconsistently.**  A file dumped by FileDataID has
  no path at all, so with a listfile the output is renamed to the in-game path
  the client will look for.
"""

from __future__ import annotations

import dataclasses
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Iterable, Sequence

from . import detect, log
from .adt import AdtParts, convert_adt, convert_wdl, convert_wdt
from .blp import convert_blp
from .db import DbdIndex, MappingLibrary, convert_db2, find_template
from .errors import ConverterError
from .liquid import convert_liquid
from .listfile import Listfile, to_posix
from .m2 import convert_anim, convert_m2, convert_skin
from .options import Options
from .report import FileResult, Report, Status
from .resolve import AssetSource
from .wmo import convert_group, convert_wmo_root

#: Directory walking opens everything: a patch build needs the sound and
#: interface files as much as the converted art, and files extracted by
#: FileDataID have no meaningful extension to filter on.


@dataclasses.dataclass(slots=True)
class Job:
    """One unit of work: a file to convert, or one to copy through."""

    kind: str
    relpath: str
    source: Path | None = None
    #: Set instead of ``source`` when the bytes come out of a CASC install.
    file_id: int | None = None
    action: str = detect.CONVERT
    #: Extra inputs for a split terrain tile, by piece name ("tex0", "obj0").
    extra: dict[str, Path] = dataclasses.field(default_factory=dict)
    extra_ids: dict[str, int] = dataclasses.field(default_factory=dict)

    def describe(self) -> str:
        pieces = sorted({*self.extra, *self.extra_ids})
        if pieces:
            return f"{self.relpath} (+{', '.join(pieces)})"
        return self.relpath


@dataclasses.dataclass(slots=True)
class Output:
    """One file the pipeline wants to write."""

    path: Path
    data: bytes
    result: FileResult


def iter_input_files(inputs: Sequence[str | os.PathLike[str]],
                     recursive: bool = True) -> list[tuple[Path, Path]]:
    """Expand paths into (root, file) pairs, where root anchors relative names."""
    out: list[tuple[Path, Path]] = []
    for entry in inputs:
        p = Path(entry)
        if p.is_file():
            out.append((p.parent, p))
        elif p.is_dir():
            walker = os.walk(p) if recursive else [(str(p), [], os.listdir(p))]
            for dirpath, _dirs, files in walker:
                for fn in sorted(files):
                    fp = Path(dirpath) / fn
                    if fp.is_file():
                        out.append((p, fp))
        else:
            log.warn(f"input not found, skipping: {p}")
    return out


#: Chunks that name a file belonging to another asset in the same run.
_COMPANION_CHUNKS = {"SFID", "AFID", "BFID", "SKID", "GFID"}


def _companion_references(kind: str, data: bytes) -> set[int]:
    """FileDataIDs this asset pulls in, so they are not converted twice."""
    if kind not in (detect.M2, detect.WMO_ROOT):
        return set()
    ids: set[int] = set()
    from .chunks import ChunkReader as _CR

    known = {"MD21", "SFID", "AFID", "BFID", "SKID", "TXID", "PFID", "MOHD", "MVER"}
    try:
        for chunk in _CR.auto(data, known):
            if chunk.name not in _COMPANION_CHUNKS:
                continue
            if chunk.name == "AFID":
                for i in range(len(chunk.data) // 8):
                    ids.add(int.from_bytes(chunk.data[i * 8 + 4: i * 8 + 8], "little"))
            else:
                for i in range(len(chunk.data) // 4):
                    ids.add(int.from_bytes(chunk.data[i * 4: i * 4 + 4], "little"))
    except Exception:  # noqa: BLE001 - planning must never raise
        return set()
    ids.discard(0)
    return ids


def _companion_names(kind: str, path: Path) -> set[str]:
    """Sibling filenames the client would glob for this asset."""
    stem = path.stem
    if kind == detect.M2:
        names = {f"{stem}{i:02d}.skin".lower() for i in range(4)}
        return names
    if kind == detect.WMO_ROOT:
        return {f"{stem}_{i:03d}.wmo".lower() for i in range(512)}
    return set()


def _merged(source: str, kind: str, why: str,
            file_id: int | None = None) -> FileResult:
    """Record a file another output already carries, so it is still counted."""
    res = FileResult(source=source, kind=kind, status=Status.MERGED)
    if file_id is None:
        res.info("plan.merged", why)
    else:
        res.info("plan.merged", why, file_id=file_id)
    return res


def plan(inputs: Sequence[str | os.PathLike[str]], recursive: bool = True,
         claim_companions: bool = True, copy_unconverted: bool = True,
         convert_databases: bool = False
         ) -> tuple[list[Job], list[FileResult]]:
    """Classify inputs into jobs, grouping split terrain tiles.

    With ``claim_companions``, files that another job will pull in and rename
    (a model's .skin profiles, a WMO's groups) are dropped from the job list so
    they are not also converted under their original names.
    """
    jobs: list[Job] = []
    skipped: list[FileResult] = []
    adt_groups: dict[tuple[str, str], dict[str, Path]] = {}
    adt_roots: dict[tuple[str, str], Path] = {}

    claimed_ids: set[int] = set()
    claimed_names: set[str] = set()
    pending: list[tuple[Path, Path, str, bytes]] = []

    for root, path in iter_input_files(inputs, recursive):
        try:
            with path.open("rb") as fh:
                head = fh.read(4096)
                rest = fh.read() if claim_companions else b""
        except OSError as exc:
            res = FileResult(source=str(path))
            res.fail("io.read", f"cannot read file: {exc}")
            skipped.append(res)
            continue

        kind = detect.detect(head, str(path))
        rel = os.path.relpath(path, root)
        if claim_companions and kind in (detect.M2, detect.WMO_ROOT):
            claimed_ids |= _companion_references(kind, head + rest)
            claimed_names |= _companion_names(kind, path)

        if kind == detect.ADT:
            base = detect.adt_base_name(str(path))
            key = (str(path.parent), base.lower())
            stem = path.stem.lower()
            part = "root"
            for suffix in detect.SPLIT_ADT_SUFFIXES:
                if stem.endswith(suffix):
                    part = suffix.lstrip("_")
                    break
            adt_groups.setdefault(key, {})[part] = path
            if part == "root":
                adt_roots[key] = Path(root)
            continue

        action, reason = detect.classify(kind, str(path), convert_databases)
        if action == detect.COPY and not copy_unconverted:
            action, reason = detect.SKIP, (
                "3.3.5a reads this format unchanged, but --no-copy-unconverted "
                "was given")
        if action == detect.SKIP:
            res = FileResult(source=rel, kind=kind, status=Status.SKIPPED)
            res.info("detect.skipped", reason)
            skipped.append(res)
            continue

        pending.append((root, path, kind, rel, action, reason))

    for _root, path, kind, rel, action, reason in pending:
        if claim_companions and kind in (detect.SKIN, detect.ANIM, detect.WMO_GROUP):
            # A claimed companion is not dropped: the asset that references
            # it converts it, under the name the client globs for, and emits
            # its own result. Recording it here as well would count it twice.
            if path.name.lower() in claimed_names:
                log.debug(f"{rel}: converted by the asset that references it")
                continue
            if path.stem.isdigit() and int(path.stem) in claimed_ids:
                log.debug(f"{rel}: converted by the asset that references it "
                          f"(FileDataID {path.stem})")
                continue
        jobs.append(Job(kind=kind, source=path, relpath=rel, action=action))

    for key, parts in sorted(adt_groups.items()):
        root_path = parts.get("root")
        if root_path is None:
            names = ", ".join(p.name for p in parts.values())
            res = FileResult(source=names, kind="adt", status=Status.SKIPPED)
            res.info("adt.no_root",
                     "split terrain pieces with no matching terrain file; "
                     "convert the tile's base .adt alongside them")
            skipped.append(res)
            continue
        anchor = adt_roots.get(key, root_path.parent)
        extra = {k: v for k, v in parts.items() if k in ("tex0", "obj0")}
        jobs.append(Job(kind=detect.ADT, source=root_path,
                        relpath=os.path.relpath(root_path, anchor), extra=extra))
        for part, path in sorted(extra.items()):
            skipped.append(_merged(os.path.relpath(path, anchor), detect.ADT,
                                   f"merged into {root_path.name} as the "
                                   f"tile's _{part} piece"))
        # Pieces the merged tile has no room for still have to be accounted
        # for, or they would leave the run without ever being mentioned.
        for part, path in sorted(parts.items()):
            if part in ("root", "tex0", "obj0"):
                continue
            res = FileResult(source=os.path.relpath(path, anchor),
                             kind=detect.ADT, status=Status.SKIPPED)
            res.info("adt.piece_unused",
                     detect.UNUSED_ADT_PIECES.get(
                         part, f"the _{part} piece has no 3.3.5a equivalent"))
            skipped.append(res)

    return jobs, skipped


def plan_casc(storage, listfile: Listfile, *, include: Sequence[str] = (),
              file_ids: Sequence[int] = (),
              exclude: Sequence[str] = (),
              claim_companions: bool = True, copy_unconverted: bool = True,
              convert_databases: bool = False
              ) -> tuple[list[Job], list[FileResult]]:
    """Choose files to pull out of a CASC install.

    Selection is by in-game path glob, which needs the listfile, or by explicit
    FileDataID, which does not. Files whose path is unknown are reported rather
    than silently missed, because "the listfile is too old" is by far the most
    common reason an extraction comes up short.
    """
    import fnmatch

    jobs: list[Job] = []
    skipped: list[FileResult] = []
    patterns = [normalise_pattern(p) for p in include]
    excludes = [normalise_pattern(p) for p in exclude]

    selected: dict[int, str] = {}
    for file_id in file_ids:
        path = listfile.path_for(file_id) or f"{file_id}.unknown"
        selected[file_id] = path

    if patterns:
        unknown = 0
        for file_id in storage.file_ids():
            path = listfile.path_for(file_id)
            if path is None:
                unknown += 1
                continue
            if not any(fnmatch.fnmatch(path, p) for p in patterns):
                continue
            if any(fnmatch.fnmatch(path, p) for p in excludes):
                continue
            selected[file_id] = path
        if unknown:
            log.info(f"{unknown} file(s) in this build have no listfile entry "
                     f"and cannot be matched by path; select them by "
                     f"--fileid if you need them")

    claimed_ids: set[int] = set()
    if claim_companions:
        for file_id, path in selected.items():
            if path.endswith((".m2", ".wmo")):
                data, why = storage.try_read_file_id(file_id)
                if data is not None:
                    claimed_ids |= _companion_references(
                        detect.detect(data, path), data)

    # A tile's terrain, texture and object files are one job, as on disk.
    adt_pieces: dict[str, dict[str, tuple[int, str]]] = {}

    for file_id, path in sorted(selected.items(), key=lambda kv: kv[1]):
        if claim_companions and file_id in claimed_ids:
            log.debug(f"skipping {path}: converted as a companion")
            continue
        data, why = storage.try_read_file_id(file_id)
        if data is None:
            res = FileResult(source=path, kind="unknown", status=Status.SKIPPED)
            res.info("casc.unavailable", why, file_id=file_id)
            skipped.append(res)
            continue
        kind = detect.detect(data[:4096], path)
        if path.endswith(".unknown"):
            # No listfile entry: name it by FileDataID and detected type so it
            # still lands somewhere predictable.
            path = f"unknown\\{file_id}{detect.EXTENSIONS.get(kind, '.bin')}"
        action, reason = detect.classify(kind, path, convert_databases)
        if action == detect.COPY and not copy_unconverted:
            action, reason = detect.SKIP, (
                "3.3.5a reads this format unchanged, but --no-copy-unconverted "
                "was given")
        if action == detect.SKIP:
            res = FileResult(source=path, kind=kind, status=Status.SKIPPED)
            res.info("detect.skipped", reason, file_id=file_id)
            skipped.append(res)
            continue

        if kind == detect.ADT:
            base = detect.adt_base_name(path)
            stem = os.path.splitext(os.path.basename(path))[0].lower()
            piece = "root"
            for suffix in detect.SPLIT_ADT_SUFFIXES:
                if stem.endswith(suffix):
                    piece = suffix.lstrip("_")
                    break
            adt_pieces.setdefault(base.lower(), {})[piece] = (file_id, path)
            continue

        jobs.append(Job(kind=kind, relpath=to_posix(path), file_id=file_id,
                        action=action))

    for base, pieces in sorted(adt_pieces.items()):
        root_piece = pieces.get("root")
        if root_piece is None:
            names = ", ".join(p for _id, p in pieces.values())
            res = FileResult(source=names, kind="adt", status=Status.SKIPPED)
            res.info("adt.no_root",
                     "split terrain pieces with no matching terrain file; "
                     "widen the --include glob to take the tile's base .adt")
            skipped.append(res)
            continue
        file_id, path = root_piece
        merged_ids = {k: v[0] for k, v in pieces.items()
                      if k in ("tex0", "obj0")}
        jobs.append(Job(kind=detect.ADT, relpath=to_posix(path), file_id=file_id,
                        extra_ids=merged_ids))
        for part in sorted(merged_ids):
            skipped.append(_merged(pieces[part][1], detect.ADT,
                                   f"merged into {path} as the tile's "
                                   f"_{part} piece", pieces[part][0]))
        for part, (piece_id, piece_path) in sorted(pieces.items()):
            if part in ("root", "tex0", "obj0"):
                continue
            res = FileResult(source=piece_path, kind=detect.ADT,
                             status=Status.SKIPPED)
            res.info("adt.piece_unused",
                     detect.UNUSED_ADT_PIECES.get(
                         part, f"the _{part} piece has no 3.3.5a equivalent"),
                     file_id=piece_id)
            skipped.append(res)
    return jobs, skipped


def normalise_pattern(pattern: str) -> str:
    """Match the way listfile paths are stored: lowercase, backslashes."""
    return pattern.replace("/", "\\").lower()


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
class Converter:
    """Runs jobs and decides where their outputs land."""

    def __init__(self, opts: Options, listfile: Listfile,
                 out_dir: Path, source: AssetSource, storage=None,
                 definitions: DbdIndex | None = None,
                 mappings: MappingLibrary | None = None):
        self.opts = opts
        self.listfile = listfile
        self.out_dir = Path(out_dir)
        self.source = source
        self.storage = storage
        self.definitions = definitions or DbdIndex(None)
        self.mappings = mappings or MappingLibrary(opts.db_mappings or None)

    # -- naming ---------------------------------------------------------
    def output_path(self, job: Job) -> Path:
        rel = job.relpath
        stem = Path(rel).stem
        if self.opts.name_from_listfile and stem.isdigit() and self.listfile:
            game_path = self.listfile.path_for(int(stem))
            if game_path:
                rel = to_posix(game_path)
        if self.opts.flatten:
            rel = os.path.basename(rel)
        return self.out_dir / rel

    # -- work -----------------------------------------------------------
    def read(self, job: Job) -> bytes:
        if job.file_id is not None:
            if self.storage is None:
                raise ConverterError(
                    f"{job.relpath} comes from a CASC install but no storage "
                    f"is open")
            return self.storage.read_file_id(job.file_id)
        assert job.source is not None
        return job.source.read_bytes()

    def convert(self, job: Job) -> list[Output]:
        started = time.time()
        target = self.output_path(job)
        result = FileResult(source=job.relpath, kind=job.kind,
                            target=str(target))
        try:
            data = self.read(job)
        except ConverterError as exc:
            result.status = Status.SKIPPED
            result.info("io.unavailable", str(exc))
            result.elapsed = time.time() - started
            return [Output(target, b"", result)]
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop a run
            result.fail("io.read", f"{type(exc).__name__}: {exc}")
            result.elapsed = time.time() - started
            return [Output(target, b"", result)]

        result.bytes_in = len(data)
        if job.source is not None:
            self.source.add_root(job.source.parent)

        if job.action == detect.COPY:
            result.status = Status.PASSTHROUGH
            result.kind = job.kind if job.kind != detect.UNKNOWN else "data"
            result.info("copy.verbatim",
                        "3.3.5a reads this format unchanged; copied into the "
                        "output as-is")
            result.elapsed = time.time() - started
            return [Output(target, data, result)]

        try:
            outputs = self._dispatch(job, data, target, result)
        except ConverterError as exc:
            result.fail(f"{job.kind}.error", str(exc))
            result.elapsed = time.time() - started
            return [Output(target, b"", result)]
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop a run
            result.fail(f"{job.kind}.error", f"{type(exc).__name__}: {exc}")
            result.elapsed = time.time() - started
            return [Output(target, b"", result)]

        if result.elapsed == 0.0:
            result.elapsed = time.time() - started
        return outputs

    def _dispatch(self, job: Job, data: bytes, target: Path,
                  result: FileResult) -> list[Output]:
        kind = job.kind
        opts = self.opts

        if kind == detect.BLP:
            out, result = convert_blp(data, job.relpath, opts, result)
            return [Output(target, out, result)]

        if kind == detect.SKIN:
            out, result = convert_skin(data, job.relpath, opts, False, result)
            return [Output(target, out, result)]

        if kind == detect.ANIM:
            out, result = convert_anim(data, job.relpath, opts, None, result)
            return [Output(target, out, result)]

        if kind == detect.M2:
            out, result, companions = convert_m2(data, job.relpath, opts,
                                                 self.listfile, self.source, result,
                                                 output_stem=target.stem)
            outputs = [Output(target, out, result)]
            for c in companions:
                outputs.append(Output(target.parent / c.filename, c.data, c.result))
            return outputs

        if kind == detect.WMO_ROOT:
            out, result, companions = convert_wmo_root(data, job.relpath, opts,
                                                       self.listfile, self.source,
                                                       result,
                                                       output_stem=target.stem)
            outputs = [Output(target, out, result)]
            for c in companions:
                outputs.append(Output(target.parent / c.filename, c.data, c.result))
            return outputs

        if kind == detect.WMO_GROUP:
            out, result = convert_group(data, job.relpath, opts, result)
            return [Output(target, out, result)]

        if kind == detect.DB2:
            from .db.convert import table_name_for

            table = table_name_for(job.relpath)
            template = find_template(opts.db_templates, table)
            out, result = convert_db2(data, job.relpath, opts, self.listfile,
                                      self.definitions, self.mappings,
                                      template, table, result)
            # 3.3.5a reads .dbc, so the output changes extension.
            return [Output(target.with_suffix(".dbc"), out, result)]

        if kind == detect.WDT:
            out, result = convert_wdt(data, job.relpath, opts, result)
            return [Output(target, out, result)]

        if kind == detect.WDL:
            out, result = convert_wdl(data, job.relpath, opts, result)
            return [Output(target, out, result)]

        if kind == detect.LIQUID:
            out, result = convert_liquid(data, job.relpath, opts, result)
            return [Output(target, out, result)]

        if kind == detect.ADT:
            parts = AdtParts(root=data)
            if opts.merge_split_adt:
                for piece in ("tex0", "obj0"):
                    if piece in job.extra:
                        setattr(parts, piece, job.extra[piece].read_bytes())
                    elif piece in job.extra_ids and self.storage is not None:
                        raw, why = self.storage.try_read_file_id(
                            job.extra_ids[piece])
                        if raw is None:
                            result.warn("adt.piece_unavailable",
                                        f"{piece} piece unavailable: {why}")
                        else:
                            setattr(parts, piece, raw)
            out, result = convert_adt(parts, job.relpath, opts, self.listfile, result)
            return [Output(target, out, result)]

        result.status = Status.SKIPPED
        result.info("detect.unsupported", f"no converter for {kind!r}")
        return [Output(target, b"", result)]

    # -- output ---------------------------------------------------------
    def write(self, outputs: Iterable[Output]) -> None:
        for out in outputs:
            if not out.data or out.result.status is Status.FAILED:
                continue
            out.result.target = str(out.path)
            out.result.bytes_out = len(out.data)
            if self.opts.dry_run:
                continue
            if out.path.exists() and not self.opts.overwrite:
                out.result.warn("io.exists",
                                f"{out.path} already exists; pass --overwrite "
                                f"to replace it")
                continue
            out.path.parent.mkdir(parents=True, exist_ok=True)
            out.path.write_bytes(out.data)


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------
_WORKER: dict[str, object] = {}


def _worker_init(opts: Options, listfile_path: str | None, roots: list[str],
                 out_dir: str, casc: dict | None,
                 dbd_dir: str | None = None) -> None:  # pragma: no cover
    lf = Listfile.load(listfile_path) if listfile_path else Listfile()
    storage = None
    if casc:
        # Each worker opens its own handles; CascStorage is not picklable.
        from .casc import CascStorage, KeyRing
        keys = KeyRing.load(casc["keys"]) if casc.get("keys") else KeyRing()
        storage = CascStorage.open(casc["path"], product=casc.get("product"),
                                   locale=casc.get("locale", "enus"), keys=keys)
    source = AssetSource(lf, roots=roots, casc=storage)
    _WORKER["converter"] = Converter(opts, lf, Path(out_dir), source, storage,
                                     DbdIndex(dbd_dir) if dbd_dir else None)


def _worker_run(job: Job) -> list[Output]:  # pragma: no cover
    converter: Converter = _WORKER["converter"]  # type: ignore[assignment]
    outputs = converter.convert(job)
    converter.write(outputs)
    # Payloads stay in the worker; only the results travel back.
    return [Output(o.path, b"", o.result) for o in outputs]


def run(jobs: Sequence[Job], opts: Options, listfile: Listfile,
        out_dir: str | os.PathLike[str], roots: Sequence[str] = (),
        listfile_path: str | None = None,
        skipped: Sequence[FileResult] = (),
        storage=None, casc_args: dict | None = None,
        definitions: DbdIndex | None = None) -> Report:
    """Convert every job, in parallel when asked, and collect the results."""
    report = Report()
    report.extend(skipped)
    out_dir = Path(out_dir)

    if opts.jobs > 1 and len(jobs) > 1:
        try:
            with ProcessPoolExecutor(
                    max_workers=opts.jobs, initializer=_worker_init,
                    initargs=(opts, listfile_path, list(roots), str(out_dir),
                              casc_args,
                              str(definitions.directory)
                              if definitions and definitions.directory else None)
            ) as pool:
                for outputs in pool.map(_worker_run, jobs):
                    for out in outputs:
                        report.add(out.result)
            report.close()
            return report
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail
            log.warn(f"parallel execution unavailable ({exc}); running serially")

    source = AssetSource(listfile, roots=list(roots), casc=storage)
    converter = Converter(opts, listfile, out_dir, source, storage, definitions)
    for job in jobs:
        log.debug(f"converting {job.describe()}")
        outputs = converter.convert(job)
        converter.write(outputs)
        for out in outputs:
            report.add(out.result)
    report.close()
    return report
