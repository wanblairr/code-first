"""Verify a fresh extracted v3.7 package; no network/provider calls."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import re
import stat
import sys

ROOT = Path(__file__).resolve().parent
OLD = b"codex" + b"gemini" + b"Runpod" + b"001"
SECRET = re.compile(rb"ya29\.[A-Za-z0-9_-]{20,}|GOCSPX-[A-Za-z0-9_-]{12,}|\brpa_[A-Za-z0-9_-]{20,}|\bEAA[A-Za-z0-9]{40,}|\b(?:nfp|nfu)_[A-Za-z0-9_-]{20,}|\bsk-[A-Za-z0-9_-]{24,}|1//[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
FIXTURES = {"local_app/tests/test_gmail_auth_manager_v33.py":"3826947d1502d82bf5aa7118ea3d6759ef60a55963204991afab8eb2daeba6bb",
            "local_app/tests/test_integration_config_v33.py":"f39e93859261e3bbdfa8e0480b6faef035e9e0671fbe02777820566bc7f714ec"}

def digest(data):
    return hashlib.sha256(data).hexdigest()

def main():
    manifest = json.loads((ROOT / "FINAL_FILES.json").read_text(encoding="utf-8"))
    entries = manifest["files"]
    seen = set()
    for entry in entries:
        name = entry["path"]
        parts = name.split("/")
        if (not name or "\\" in name or any(part in ("", ".", "..") or ":" in part for part in parts)
                or PurePosixPath(name).is_absolute() or name in seen):
            raise ValueError("Unsafe or duplicate manifest path.")
        if any(part.casefold() in {"node_modules", ".netlify", ".git", "private", "credentials", "secrets", "__pycache__", "reports"} or part.casefold().startswith(("runtime", ".env", "auth_config", "private_api_credentials", "client_secret", "owner_secret")) for part in parts):
            raise ValueError("Private/runtime manifest path refused.")
        seen.add(name)
        source = ROOT.joinpath(*parts)
        flags = getattr(source.lstat(), "st_file_attributes", 0)
        if source.is_symlink() or flags & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("Linked file refused.")
        if not source.resolve().is_relative_to(ROOT) or not source.is_file():
            raise ValueError("Manifest path escaped package.")
        data = source.read_bytes()
        if len(data) != entry["bytes"] or digest(data) != entry["sha256"]:
            raise ValueError("Hash/size mismatch: " + name)
        if OLD in data:
            raise ValueError("Historical password marker refused.")
        matches = SECRET.findall(data)
        fixture = "tests" in parts and source.suffix in (".py", ".mjs", ".js")
        if matches and (not fixture or
                FIXTURES.get(name) != digest(data) and any(not re.search(rb"test|fake|dummy|mock|example|fixture", marker, re.I) for marker in matches)):
            raise ValueError("Credential-looking content refused: " + name)
        if source.suffix.casefold() == ".cmd" and (data.startswith(b"\xef\xbb\xbf") or b"\n" in data.replace(b"\r\n", b"")):
            raise ValueError("CMD must use BOM-free CRLF.")
    # Verification precedes first startup. Runtime files/auth verifiers created
    # by the owner later are deliberately outside the distributable manifest.
    actual = set()
    for source in ROOT.rglob("*"):
        if source.is_symlink():
            raise ValueError("Symlink in package.")
        if source.is_file():
            relative = source.relative_to(ROOT).as_posix()
            if relative != "FINAL_FILES.json":
                actual.add(relative)
    if actual != seen:
        raise ValueError("Unexpected or missing files; verify a fresh extraction before first startup.")
    print(json.dumps({"success":True,"files":len(entries),"sha256_manifest_verified":True,
                      "private_credentials_included":False,"network_calls":0}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
