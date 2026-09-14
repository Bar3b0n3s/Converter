"""Structured per-file conversion results.

Downgrading is lossy by nature, and the interesting output of a batch run is
not "did it crash" but "what did it have to throw away".  Every converter
records those decisions here so the CLI can print a summary and emit JSON for
scripted pipelines.
"""

from __future__ import annotations

import dataclasses
import json
import time
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


class Status(str, Enum):
    OK = "ok"
    #: Converted, but something the 3.3.5a client cannot express was dropped.
    LOSSY = "lossy"
    #: Already a valid 3.3.5a asset; copied through unchanged.
    PASSTHROUGH = "passthrough"
    SKIPPED = "skipped"
    FAILED = "failed"


#: Severity ordering used when folding note levels into a file status.
_RANK = {Status.OK: 0, Status.PASSTHROUGH: 0, Status.SKIPPED: 1,
         Status.LOSSY: 2, Status.FAILED: 3}


@dataclasses.dataclass(slots=True)
class Note:
    """One thing worth telling the user about a single file."""

    level: str  # "info" | "lossy" | "warn" | "error"
    code: str   # stable machine-readable identifier, e.g. "m2.particle.multitexture"
    message: str
    detail: dict[str, Any] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = {"level": self.level, "code": self.code, "message": self.message}
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclasses.dataclass(slots=True)
class FileResult:
    source: str
    target: str | None = None
    kind: str = "unknown"
    status: Status = Status.OK
    source_version: str | None = None
    target_version: str | None = None
    bytes_in: int = 0
    bytes_out: int = 0
    elapsed: float = 0.0
    notes: list[Note] = dataclasses.field(default_factory=list)
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    # -- note helpers ---------------------------------------------------
    def _add(self, level: str, code: str, message: str, **detail: Any) -> None:
        self.notes.append(Note(level, code, message, detail))

    def info(self, code: str, message: str, **detail: Any) -> None:
        self._add("info", code, message, **detail)

    def lossy(self, code: str, message: str, **detail: Any) -> None:
        """Record data that could not survive the downgrade."""
        self._add("lossy", code, message, **detail)
        self.bump(Status.LOSSY)

    def warn(self, code: str, message: str, **detail: Any) -> None:
        self._add("warn", code, message, **detail)

    def fail(self, code: str, message: str, **detail: Any) -> None:
        self._add("error", code, message, **detail)
        self.bump(Status.FAILED)

    def bump(self, status: Status) -> None:
        """Raise the status to ``status`` if that is more severe than current."""
        if _RANK[status] > _RANK[self.status]:
            self.status = status

    @property
    def ok(self) -> bool:
        return self.status is not Status.FAILED

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "status": self.status.value,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "elapsed_ms": round(self.elapsed * 1000, 2),
        }
        if self.source_version:
            d["source_version"] = self.source_version
        if self.target_version:
            d["target_version"] = self.target_version
        if self.notes:
            d["notes"] = [n.as_dict() for n in self.notes]
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclasses.dataclass(slots=True)
class Report:
    """Aggregate of a whole run."""

    files: list[FileResult] = dataclasses.field(default_factory=list)
    started: float = dataclasses.field(default_factory=time.time)
    finished: float | None = None

    def add(self, result: FileResult) -> FileResult:
        self.files.append(result)
        return result

    def extend(self, results: Iterable[FileResult]) -> None:
        self.files.extend(results)

    def close(self) -> None:
        self.finished = time.time()

    # -- aggregates -----------------------------------------------------
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.files:
            out[f.status.value] = out.get(f.status.value, 0) + 1
        return out

    @property
    def failed(self) -> list[FileResult]:
        return [f for f in self.files if f.status is Status.FAILED]

    @property
    def lossy(self) -> list[FileResult]:
        return [f for f in self.files if f.status is Status.LOSSY]

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": "wotlkconv",
            "target": {"patch": "3.3.5a", "build": 12340},
            "started": self.started,
            "finished": self.finished,
            "elapsed_s": round((self.finished or time.time()) - self.started, 3),
            "counts": self.counts(),
            "files": [f.as_dict() for f in self.files],
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.as_dict(), indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )

    def summary_lines(self, verbose: bool = False) -> list[str]:
        counts = self.counts()
        total = len(self.files)
        parts = [f"{counts.get(s.value, 0)} {s.value}" for s in Status
                 if counts.get(s.value)]
        lines = [f"{total} file(s): " + ", ".join(parts) if parts
                 else f"{total} file(s)"]
        for f in self.files:
            if f.status is Status.FAILED:
                for n in f.notes:
                    if n.level == "error":
                        lines.append(f"  FAILED {f.source}: {n.message}")
            elif verbose and f.notes:
                lines.append(f"  {f.status.value.upper()} {f.source}")
                for n in f.notes:
                    lines.append(f"      [{n.level}] {n.message}")
        return lines
