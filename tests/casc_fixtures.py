"""Builds a synthetic CASC install so the reader can be tested end to end.

Every layer is written from the format description rather than by round-tripping
the reader's own output, so an offset mistake in either direction shows up as a
failing test rather than cancelling out.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

ENTRY_HEADER = 30
KEY_PREFIX = 9


def md5(data: bytes) -> bytes:
    return hashlib.md5(data, usedforsecurity=False).digest()


# ---------------------------------------------------------------------------
# BLTE
# ---------------------------------------------------------------------------
def make_blte(payload: bytes, *, chunk_size: int = 0, mode: bytes = b"Z") -> bytes:
    """Encode a payload as a BLTE stream, optionally split into chunks."""
    if chunk_size <= 0:
        chunks = [payload]
    else:
        chunks = [payload[i:i + chunk_size]
                  for i in range(0, len(payload), chunk_size)] or [b""]

    encoded = []
    for chunk in chunks:
        if mode == b"Z":
            encoded.append(b"Z" + zlib.compress(chunk, 6))
        elif mode == b"N":
            encoded.append(b"N" + chunk)
        else:
            raise ValueError(f"fixture cannot emit mode {mode!r}")

    header_size = 12 + 24 * len(encoded)
    out = bytearray(b"BLTE")
    out += struct.pack(">I", header_size)
    out += bytes([0x0F])
    out += len(encoded).to_bytes(3, "big")
    for raw, enc in zip(chunks, encoded):
        out += struct.pack(">II", len(enc), len(raw))
        out += md5(enc)
    for enc in encoded:
        out += enc
    return bytes(out)


def make_blte_single(payload: bytes) -> bytes:
    """The headerless single-chunk form used for small files."""
    return b"BLTE" + struct.pack(">I", 0) + b"N" + payload


# ---------------------------------------------------------------------------
# Encoding table
# ---------------------------------------------------------------------------
def make_encoding(entries: dict[bytes, tuple[bytes, int]], *,
                  page_kib: int = 4, espec: bytes = b"z\0") -> bytes:
    """``{ckey: (ekey, size)}`` -> an encoding table."""
    page_size = page_kib * 1024
    entry_size = 1 + 5 + 16 + 16

    pages: list[bytearray] = []
    current = bytearray()
    first_keys: list[bytes] = []
    for ckey, (ekey, size) in sorted(entries.items()):
        if not current or len(current) + entry_size > page_size:
            if current:
                pages.append(current)
            current = bytearray()
            first_keys.append(ckey)
        current += bytes([1])
        current += size.to_bytes(5, "big")
        current += ckey
        current += ekey
    if current:
        pages.append(current)

    out = bytearray(b"EN")
    out += struct.pack(">BBBHHIIBI", 1, 16, 16, page_kib, page_kib,
                       len(pages), 0, 0, len(espec))
    out += espec
    for first in first_keys:
        out += first
        out += bytes(16)          # page checksum, unchecked by the reader
    for page in pages:
        out += bytes(page).ljust(page_size, b"\0")
    return bytes(out)


# ---------------------------------------------------------------------------
# Root table
# ---------------------------------------------------------------------------
def make_root(entries: dict[int, bytes], *, mfst: bool = True,
              locale_flags: int = 0xFFFFFFFF, content_flags: int = 0x10000000,
              version2: bool = False, with_names: bool = False) -> bytes:
    """``{file_id: ckey}`` -> a root table."""
    ids = sorted(entries)
    out = bytearray()
    if mfst:
        out += b"TSFM"
        if version2:
            out += struct.pack("<IIIII", 0x18, 2, len(ids), 0, 0)
        else:
            out += struct.pack("<II", len(ids), 0)

    flags = content_flags if not with_names else (content_flags & ~0x10000000)
    out += struct.pack("<III", len(ids), flags, locale_flags)
    previous = -1
    for file_id in ids:
        out += struct.pack("<i", file_id - previous - 1)
        previous = file_id
    for file_id in ids:
        out += entries[file_id]
    if with_names:
        out += bytes(8 * len(ids))
    return bytes(out)


# ---------------------------------------------------------------------------
# Local index
# ---------------------------------------------------------------------------
def bucket_for(ekey: bytes) -> int:
    i = 0
    for byte in ekey[:KEY_PREFIX]:
        i ^= byte
    return (i & 0xF) ^ (i >> 4)


def make_index(bucket: int, entries: dict[bytes, tuple[int, int, int]]) -> bytes:
    """``{ekey: (archive, offset, size)}`` -> one ``.idx`` file."""
    out = bytearray()
    out += struct.pack("<II", 0x10, 0)                       # header size, hash
    out += struct.pack("<HBBBBBBQ", 7, bucket, 0, 4, 5, 9, 30, 0)
    out += b"\0" * (32 - len(out))                           # pad to 16-byte bound

    body = bytearray()
    for ekey, (archive, offset, size) in sorted(entries.items()):
        body += ekey[:KEY_PREFIX]
        body += ((archive << 30) | offset).to_bytes(5, "big")
        body += struct.pack("<I", size)
    out += struct.pack("<II", len(body), 0)                  # entries size, hash
    out += body
    return bytes(out)


# ---------------------------------------------------------------------------
# Whole install
# ---------------------------------------------------------------------------
def build_install(root_dir: Path, files: dict[int, bytes], *,
                  product: str = "wow", version: str = "11.0.5.57212",
                  mfst: bool = True, locale_flags: int = 0xFFFFFFFF,
                  chunk_size: int = 0) -> Path:
    """Write a complete, readable CASC install under ``root_dir``."""
    data_dir = root_dir / "Data" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    archive = bytearray()
    index_entries: dict[bytes, tuple[int, int, int]] = {}
    encoding_map: dict[bytes, tuple[bytes, int]] = {}

    def store(payload: bytes) -> bytes:
        """Append a BLTE-encoded payload to data.000 and index it."""
        stream = make_blte(payload, chunk_size=chunk_size)
        ekey = md5(stream)
        offset = len(archive)
        size = ENTRY_HEADER + len(stream)
        header = bytearray(ENTRY_HEADER)
        header[0:16] = ekey[::-1]
        struct.pack_into("<I", header, 16, size)
        archive.extend(header)
        archive.extend(stream)
        index_entries[ekey] = (0, offset, size)
        return ekey

    root_entries: dict[int, bytes] = {}
    for file_id, payload in sorted(files.items()):
        ckey = md5(payload)
        ekey = store(payload)
        encoding_map[ckey] = (ekey, len(payload))
        root_entries[file_id] = ckey

    root_raw = make_root(root_entries, mfst=mfst, locale_flags=locale_flags)
    root_ckey = md5(root_raw)
    encoding_map[root_ckey] = (store(root_raw), len(root_raw))

    encoding_raw = make_encoding(encoding_map)
    encoding_ckey = md5(encoding_raw)
    encoding_ekey = store(encoding_raw)

    (data_dir / "data.000").write_bytes(bytes(archive))

    by_bucket: dict[int, dict[bytes, tuple[int, int, int]]] = {}
    for ekey, location in index_entries.items():
        by_bucket.setdefault(bucket_for(ekey), {})[ekey] = location
    for bucket, entries in by_bucket.items():
        (data_dir / f"{bucket:02x}00000001.idx").write_bytes(
            make_index(bucket, entries))

    build_key = md5(b"build" + version.encode()).hex()
    cdn_key = md5(b"cdn" + version.encode()).hex()
    config_dir = root_dir / "Data" / "config" / build_key[0:2] / build_key[2:4]
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / build_key).write_text(
        f"root = {root_ckey.hex()}\n"
        f"encoding = {encoding_ckey.hex()} {encoding_ekey.hex()}\n"
        f"install = {md5(b'install').hex()}\n"
        f"build-name = WOW-{version.split('.')[-1]}patch{version}\n",
        encoding="utf-8")

    (root_dir / ".build.info").write_text(
        "Branch!STRING:0|Active!DEC:1|Build Key!HEX:16|CDN Key!HEX:16|"
        "Version!STRING:0|Product!STRING:0\n"
        f"us|1|{build_key}|{cdn_key}|{version}|{product}\n",
        encoding="utf-8")
    return root_dir
