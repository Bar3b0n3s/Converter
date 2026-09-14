"""External animation files.

3.3.5a reads a ``.anim`` as one flat blob: the M2's per-sequence track
sub-arrays carry offsets that point straight into it.  Legion wrapped that same
blob in an ``AFM2`` chunk and added ``AFSA``/``AFSB`` for its physics-driven
bone tracks, which the old client has no use for.

Converting is therefore an unwrap: the ``AFM2`` payload is written out on its
own.  The track offsets baked into the M2 are relative to the start of that
payload, not to the start of the file, so extracting the chunk body keeps them
valid without any rewriting.
"""

from __future__ import annotations

from ..chunks import ChunkReader
from ..errors import MalformedFileError
from ..options import Options
from ..report import FileResult, Status
from .model import M2Model

KNOWN_ANIM_CHUNKS = {"AFM2", "AFSA", "AFSB", "AFSK"}

#: Chunks that carry data 3.3.5a cannot use, with the reason.
DROPPED_ANIM_CHUNKS = {
    "AFSA": "physics-driven bone tracks (attachment sim)",
    "AFSB": "physics-driven bone tracks (bone sim)",
    "AFSK": "skeleton animation overrides",
}


def convert_anim(data: bytes, source_name: str, opts: Options,
                 model: M2Model | None = None,
                 result: FileResult | None = None) -> tuple[bytes, FileResult]:
    """Unwrap a Legion ``.anim`` into the flat blob 3.3.5a expects."""
    res = result or FileResult(source=source_name, kind="anim")
    res.kind = "anim"
    res.bytes_in = len(data)

    if len(data) < 8:
        raise MalformedFileError(f"{source_name}: file is only {len(data)} bytes")

    # A flat .anim has no chunk structure; anything that is not a recognised
    # chunk magic is treated as already-converted payload.
    reader = ChunkReader.auto(data, KNOWN_ANIM_CHUNKS, name=source_name)
    chunks = {}
    try:
        for chunk in reader:
            chunks.setdefault(chunk.name, []).append(chunk)
    except MalformedFileError:
        chunks = {}

    if "AFM2" not in chunks:
        res.status = Status.PASSTHROUGH
        res.source_version = "anim (flat)"
        res.target_version = "anim (flat)"
        res.bytes_out = len(data)
        res.info("anim.passthrough", "already a flat 3.3.5a animation blob")
        return data, res

    afm2 = chunks["AFM2"][0]
    payload = afm2.data
    res.source_version = f"anim (chunked, {len(chunks)} chunk kinds)"

    dropped = [f"{n} ({DROPPED_ANIM_CHUNKS[n]})"
               for n in chunks if n in DROPPED_ANIM_CHUNKS]
    if dropped:
        res.lossy("anim.chunks.dropped",
                  "dropped animation chunks with no 3.3.5a equivalent: "
                  + ", ".join(dropped), chunks=sorted(chunks))

    res.target_version = "anim (flat)"
    res.bytes_out = len(payload)
    res.info("anim.unwrapped",
             f"unwrapped AFM2 payload ({len(payload)} bytes) into a flat blob")
    return payload, res


def inspect_anim(data: bytes, source_name: str) -> dict:
    reader = ChunkReader.auto(data, KNOWN_ANIM_CHUNKS, name=source_name)
    try:
        chunks = {c.name: len(c.data) for c in reader}
    except MalformedFileError:
        chunks = {}
    return {
        "kind": "anim",
        "chunked": "AFM2" in chunks,
        "chunks": chunks or None,
        "bytes": len(data),
    }
