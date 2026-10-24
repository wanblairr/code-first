"""Portable ZIP metadata for the Windows-authored competition deliverables.

Archive entries use Unix creator metadata and POSIX modes. Python's ZIP writer
encodes non-ASCII names as UTF-8 in both local and central headers. The caller
selects files, including any runtime/secret exclusions, before invoking this API.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import time
from typing import Iterable
import zipfile


def archive_name(value: str) -> str:
    """Return a relative forward-slash name; reject ambiguous/traversal paths."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("ZIP entry needs a non-empty relative name.")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if normalized.startswith("/") or any(part in ("", ".", "..") for part in normalized.rstrip("/").split("/")) or ":" in path.parts[0]:
        raise ValueError("ZIP entry names must be relative, without traversal or drive letters.")
    return str(path)


def portable_zip_info(source: Path, name: str) -> zipfile.ZipInfo:
    """Create checked Unix metadata for one ordinary file or directory."""
    source = Path(source)
    if source.is_symlink():
        raise ValueError("ZIP deliverables do not include symbolic links.")
    is_directory = source.is_dir()
    if not is_directory and not source.is_file():
        raise ValueError("ZIP source must be a regular file or directory.")
    name = archive_name(name).rstrip("/") + ("/" if is_directory else "")
    modified = time.localtime(source.stat().st_mtime)[:6]
    if not 1980 <= modified[0] <= 2107:
        modified = (1980, 1, 1, 0, 0, 0)
    info = zipfile.ZipInfo(name, modified)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = (stat.S_IFDIR | 0o755) if is_directory else (stat.S_IFREG | 0o644)
    info.external_attr = (mode << 16) | (0x10 if is_directory else 0)
    # The writer recalculates UTF-8 flags from filename encoding when serializing;
    # set it here as well so the metadata object accurately describes its name.
    if not name.isascii():
        info.flag_bits |= 0x800
    return info


def write_portable_zip(entries: Iterable[tuple[Path, str]], destination: Path) -> Path:
    """Write ``(source_path, archive_name)`` entries, then atomically publish ZIP.

    Source selection is explicit. This helper does not recurse or choose which
    project files may be distributed. Invalid/duplicate entries leave an
    existing destination untouched.
    """
    prepared = []
    seen = set()
    for source, name in entries:
        source = Path(source)
        info = portable_zip_info(source, name)
        canonical = info.filename.rstrip("/")
        if canonical in seen:
            raise ValueError("Duplicate ZIP entry: " + info.filename)
        seen.add(canonical)
        prepared.append((source, info))
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + destination.name + "-", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for source, info in prepared:
                archive.writestr(info, b"" if source.is_dir() else source.read_bytes())
        with zipfile.ZipFile(temporary) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise OSError("ZIP CRC check failed: " + bad)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination
