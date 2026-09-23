"""이 컴퓨터 사용자별 설정 (~/.kbroll/config.json).

작업 폴더(저장 위치)처럼 작업 폴더 바깥에 기억해 둬야 하는 값을 저장한다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets


def config_dir() -> str:
    return os.environ.get("KBROLL_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".kbroll")


def _path() -> str:
    return os.path.join(config_dir(), "config.json")


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(**values) -> None:
    data = load()
    data.update(values)
    os.makedirs(config_dir(), exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())
    try:
        os.chmod(_path(), 0o600)
    except OSError:
        pass


def default_workdir() -> str:
    return load().get("workdir") or os.path.join(os.path.expanduser("~"), "kbroll_작업")


# ---- 비밀번호 (서버 모드) : 원문 대신 해시만 저장 ----
_ITERATIONS = 200_000


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS)
    return f"pbkdf2${salt}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _algo, salt, _digest = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)
