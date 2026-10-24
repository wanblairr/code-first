"""Server-side password verification for the configured demonstration workbench."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from pathlib import Path

from cafe_core import CafeError

AUTH_CONFIG = Path(__file__).resolve().with_name("auth_config.json")


def make_auth_policy(password, *, salt=None, iterations=310_000):
    """Build a JSON-safe salted verifier, for explicit configuration or tests."""
    if not isinstance(password, str) or not password or len(password) > 512:
        raise CafeError("密码须为非空且最多512字的文本。")
    encoded = password.encode("utf-8")
    if len(encoded) > 1024 or type(iterations) is not int or not 100_000 <= iterations <= 1_000_000:
        raise CafeError("密码或密码哈希参数无效。")
    salt = secrets.token_bytes(24) if salt is None else salt
    if not isinstance(salt, bytes) or not 16 <= len(salt) <= 64:
        raise CafeError("密码盐值需为16至64字节。")
    digest = hashlib.pbkdf2_hmac("sha256", encoded, salt, iterations, dklen=32)
    return {"algorithm": "pbkdf2_sha256", "iterations": iterations,
            "salt_base64": base64.b64encode(salt).decode("ascii"), "digest_hex": digest.hex()}


def load_auth_policy(path=None):
    """Read the package policy; old runtime plaintext secrets are not credentials."""
    path = Path(path) if path is not None else AUTH_CONFIG
    try:
        if path.stat().st_size > 8192:
            raise ValueError
        policy = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(policy, dict) or set(policy) != {"algorithm", "iterations", "salt_base64", "digest_hex"}:
            raise ValueError
        if policy["algorithm"] != "pbkdf2_sha256" or type(policy["iterations"]) is not int or not 100_000 <= policy["iterations"] <= 1_000_000:
            raise ValueError
        salt = base64.b64decode(policy["salt_base64"], validate=True)
        digest = bytes.fromhex(policy["digest_hex"])
        if not 16 <= len(salt) <= 64 or len(digest) != 32:
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError, binascii.Error):
        raise CafeError("门店登录配置缺失或无效，请恢复 auth_config.json 后重启服务。") from None
    return {"iterations": policy["iterations"], "salt": salt, "digest": digest}


def verify_owner_password(supplied, policy=None):
    """Compare a derived digest in constant time; never return a password."""
    if not isinstance(supplied, str) or not supplied or len(supplied) > 512:
        return False
    try:
        encoded = supplied.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if len(encoded) > 1024:
        return False
    policy = policy if policy is not None else load_auth_policy()
    derived = hashlib.pbkdf2_hmac("sha256", encoded, policy["salt"], policy["iterations"], dklen=32)
    return hmac.compare_digest(derived, policy["digest"])
