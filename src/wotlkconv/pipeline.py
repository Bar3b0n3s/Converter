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
from .adt import AdtParts, convert_adt
from .blp import convert_blp
from .errors import ConverterError
from .listfile import Listfile, to_posix
from .m2 import convert_anim, convert_m2, convert_skin
from .options import Options
from .report import FileResult, Report, Status
from .resolve import AssetSource
from .wmo import convert_group, convert_wmo_root

#: Extensions worth opening when walking a directory.
INPUT_EXTENSIONS = {".m2", ".skin", ".anim", ".skel", ".blp", ".wmo", ".adt",
                    ".wdt", ".unknown", ""}


@dataclasses.dataclass(slots=True)
class Job:
    """One unit of conversion work."""

    kind: str
    source: Path
    relpath: str
    extra: dict[str, Path] = dataclasses.field(default_factory=dict)

    def describe(self) -> str:
        if self.extra:
            return f"{self.relpath} (+{', '.join(sorted(self.extra))})"
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
                    if not fp.is_file():
                        continue
                    if fp.suffix.lower() in INPUT_EXTENSIONS or fp.stem.isdigit():
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


def plan(inputs: Sequence[str | os.PathLike[str]], recursive: bool = True,
         claim_companions: bool = True) -> tuple[list[Job], list[FileResult]]:
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

        if kind == detect.SKEL:
            res = FileResult(source=rel, kind="skel", status=Status.SKIPPED)
            res.info("skel.companion",
                     "skeletons are merged into the model that references them, "
                     "not converted on their own")
            skipped.append(res)
            continue

        if kind == detect.UNKNOWN:
            res = FileResult(source=rel, kind="unknown", status=Status.SKIPPED)
            res.info("detect.unknown", "unrecognised file format")
            skipped.append(res)
            continue

        pending.append((root, path, kind, rel))

    for _root, path, kind, rel in pending:
        if claim_companions and kind in (detect.SKIN, detect.ANIM, detect.WMO_GROUP):
            if path.name.lower() in claimed_names:
                log.debug(f"skipping {rel}: converted as a companion")
                continue
            if path.stem.isdigit() and int(path.stem) in claimed_ids:
                log.debug(f"skipping {rel}: converted as a companion "
                          f"(FileDataID {path.stem})")
                continue
        jobs.append(Job(kind=kind, source=path, relpath=rel))

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

    return jobs, skipped


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
class Converter:
    """Runs jobs and decides where their outputs land."""

    def __init__(self, opts: Options, listfile: Listfile,
                 out_dir: Path, source: AssetSource):
        self.opts = opts
        self.listfile = listfile
        self.out_dir = Path(out_dir)
        self.source = source

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
    def convert(self, job: Job) -> list[Output]:
        started = time.time()
        target = self.output_path(job)
        data = job.source.read_bytes()
        result = FileResult(source=job.relpath, kind=job.kind,
                            target=str(target), bytes_in=len(data))
        self.source.add_root(job.source.parent)

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

        if kind == detect.ADT:
            parts = AdtParts(root=data)
            if opts.merge_split_adt:
                if "tex0" in job.extra:
                    parts.tex0 = job.extra["tex0"].read_bytes()
                if "obj0" in job.extra:
                    parts.obj0 = job.extra["obj0"].read_bytes()
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


def _worker_init(opts: Options, listfile_path: str | None,
                 roots: list[str], out_dir: str) -> None:  # pragma: no cover
    lf = Listfile.load(listfile_path) if listfile_path else Listfile()
    _WORKER["converter"] = Converter(opts, lf, Path(out_dir),
                                     AssetSource(lf, roots=roots))


def _worker_run(job: Job) -> list[Output]:  # pragma: no cover
    converter: Converter = _WORKER["converter"]  # type: ignore[assignment]
    outputs = converter.convert(job)
    converter.write(outputs)
    # Payloads stay in the worker; only the results travel back.
    return [Output(o.path, b"", o.result) for o in outputs]


def run(jobs: Sequence[Job], opts: Options, listfile: Listfile,
        out_dir: str | os.PathLike[str], roots: Sequence[str] = (),
        listfile_path: str | None = None,
        skipped: Sequence[FileResult] = ()) -> Report:
    """Convert every job, in parallel when asked, and collect the results."""
    report = Report()
    report.extend(skipped)
    out_dir = Path(out_dir)

    if opts.jobs > 1 and len(jobs) > 1:
        try:
            with ProcessPoolExecutor(
                    max_workers=opts.jobs, initializer=_worker_init,
                    initargs=(opts, listfile_path, list(roots), str(out_dir))
            ) as pool:
                for outputs in pool.map(_worker_run, jobs):
                    for out in outputs:
                        report.add(out.result)
            report.close()
            return report
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail
            log.warn(f"parallel execution unavailable ({exc}); running serially")

    source = AssetSource(listfile, roots=list(roots))
    converter = Converter(opts, listfile, out_dir, source)
    for job in jobs:
        log.debug(f"converting {job.describe()}")
        outputs = converter.convert(job)
        converter.write(outputs)
        for out in outputs:
            report.add(out.result)
    report.close()
    return report
