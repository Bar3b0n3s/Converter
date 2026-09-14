"""Top-level M2 conversion: the model plus everything that travels with it.

A single ``.m2`` is never self-contained.  3.3.5a expects to find
``<model>00.skin`` .. ``<model>03.skin`` and ``<model><anim>-<sub>.anim`` beside
it, whereas a modern extraction names those files by FileDataID (or does not
include them at all).  This module converts the model, then goes looking for
its companions and renames them into the layout the old client globs for.
"""

from __future__ import annotations

import dataclasses
import os
import time

from ..limits import M2_MAX_SKIN_PROFILES, M2_VERSION
from ..listfile import Listfile
from ..options import Options
from ..report import FileResult, Status
from ..resolve import AssetSource
from .anim import convert_anim
from .downgrade import anim_filename, downgrade_model
from .model import M2Model, parse_m2
from .skin import convert_skin
from .write import write_md20


@dataclasses.dataclass(slots=True)
class ConvertedAsset:
    """One output file produced alongside the model."""

    filename: str
    data: bytes
    result: FileResult


def _model_stem(source_name: str, model: M2Model,
                output_stem: str | None = None) -> str:
    """Basename the client will glob companions by.

    3.3.5a derives ``<model>00.skin`` and ``<model><anim>-<sub>.anim`` from the
    model's *filename*, so the companions have to match whatever the model is
    written out as -- not the source filename, and not the name stored inside
    the model.
    """
    if output_stem:
        return output_stem
    stem = os.path.splitext(os.path.basename(source_name))[0]
    if stem.isdigit() and model.name:
        # Input named by FileDataID and no output name given: the model's own
        # name is a better guess than the numeric one.
        stem = os.path.splitext(os.path.basename(model.name.replace("\\", "/")))[0]
    return stem


def _collect_skins(model: M2Model, stem: str, source: AssetSource | None,
                   source_name: str, opts: Options,
                   result: FileResult) -> list[ConvertedAsset]:
    out: list[ConvertedAsset] = []
    if source is None:
        return out

    wanted = min(max(model.num_skin_profiles, len(model.skin_file_ids)),
                 M2_MAX_SKIN_PROFILES)
    found = 0
    for index in range(wanted):
        raw = None
        if index < len(model.skin_file_ids):
            raw = source.by_file_id(model.skin_file_ids[index], ".skin")
        if raw is None:
            src_stem = os.path.splitext(os.path.basename(source_name))[0]
            raw = source.by_path(f"{src_stem}{index:02d}.skin")
        if raw is None:
            continue
        name = f"{stem}{index:02d}.skin"
        sub = FileResult(source=name, kind="skin")
        try:
            data, sub = convert_skin(raw, name, opts, model.uses_combiner_combos, sub)
        except Exception as exc:  # noqa: BLE001 - reported per file
            sub.fail("skin.error", f"{type(exc).__name__}: {exc}")
            out.append(ConvertedAsset(name, b"", sub))
            continue
        if sub.ok and data:
            out.append(ConvertedAsset(name, data, sub))
            found += 1

    if found == 0 and wanted:
        result.warn("m2.skin.missing",
                    f"no .skin profiles found next to the model; 3.3.5a will not "
                    f"render it without {stem}00.skin",
                    expected=wanted)
    elif found < wanted:
        result.info("m2.skin.partial",
                    f"found {found} of {wanted} skin profile(s)",
                    found=found, expected=wanted)
    return out


def _collect_anims(model: M2Model, stem: str, source: AssetSource | None,
                   opts: Options, result: FileResult) -> list[ConvertedAsset]:
    out: list[ConvertedAsset] = []
    if source is None or not model.anim_file_ids:
        return out

    missing = 0
    for ref in model.anim_file_ids:
        raw = source.by_file_id(ref.file_id, ".anim")
        if raw is None:
            missing += 1
            continue
        name = anim_filename(stem + ".m2", ref.anim_id, ref.sub_anim_id)
        sub = FileResult(source=name, kind="anim")
        try:
            data, sub = convert_anim(raw, name, opts, model, sub)
        except Exception as exc:  # noqa: BLE001 - reported per file
            sub.fail("anim.error", f"{type(exc).__name__}: {exc}")
            out.append(ConvertedAsset(name, b"", sub))
            continue
        out.append(ConvertedAsset(name, data, sub))

    if missing:
        result.warn("m2.anim.missing",
                    f"{missing} of {len(model.anim_file_ids)} external .anim "
                    f"file(s) were not found; those sequences will not play",
                    missing=missing, total=len(model.anim_file_ids))
    return out


def convert_m2(data: bytes, source_name: str, opts: Options,
               listfile: Listfile | None = None,
               source: AssetSource | None = None,
               result: FileResult | None = None,
               output_stem: str | None = None
               ) -> tuple[bytes, FileResult, list[ConvertedAsset]]:
    """Convert one model. Returns (MD20 bytes, result, companion files).

    ``output_stem`` is the basename the model will be written as; companions
    are named to match it so the client finds them.
    """
    started = time.time()
    res = result or FileResult(source=source_name, kind="m2")
    res.kind = "m2"
    res.bytes_in = len(data)
    listfile = listfile or Listfile()

    model = parse_m2(data, source_name)
    already_wotlk = (model.version == M2_VERSION and not model.chunked)

    model = downgrade_model(model, opts, listfile, source, res)
    if not res.ok:
        res.elapsed = time.time() - started
        return b"", res, []

    out = write_md20(model)
    res.bytes_out = len(out)
    res.extra.update({
        "name": model.name,
        "vertices": model.vertex_count,
        "bones": len(model.bones),
        "sequences": len(model.sequences),
        "textures": len(model.textures),
        "particles": len(model.particles),
        "skin_profiles": model.num_skin_profiles,
    })

    companions: list[ConvertedAsset] = []
    if opts.convert_companions:
        stem = _model_stem(source_name, model, output_stem)
        companions += _collect_skins(model, stem, source, source_name, opts, res)
        companions += _collect_anims(model, stem, source, opts, res)

    if already_wotlk and res.status is Status.OK:
        res.status = Status.PASSTHROUGH
        res.info("m2.passthrough", "already a version 264 model; rewritten as-is")

    res.elapsed = time.time() - started
    return out, res, companions


def inspect_m2(data: bytes, source_name: str) -> dict:
    model = parse_m2(data, source_name)
    return {
        "kind": "m2",
        "version": model.version,
        "chunked": model.chunked,
        "name": model.name,
        "vertices": model.vertex_count,
        "bones": len(model.bones),
        "sequences": len(model.sequences),
        "textures": len(model.textures),
        "materials": len(model.materials),
        "particles": len(model.particles),
        "ribbons": len(model.ribbons),
        "cameras": len(model.cameras),
        "lights": len(model.lights),
        "skin_profiles": model.num_skin_profiles,
        "texture_file_ids": model.texture_file_ids or None,
        "skin_file_ids": model.skin_file_ids or None,
        "skeleton_file_id": model.skeleton_file_id or None,
        "phys_file_id": model.phys_file_id or None,
        "anim_files": len(model.anim_file_ids) or None,
        "extra_chunks": model.extra_chunks or None,
        "wotlk_compatible": model.version == M2_VERSION and not model.chunked,
    }
