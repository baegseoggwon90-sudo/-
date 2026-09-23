"""내 컴퓨터를 인터넷 주소로 열어 주는 Cloudflare 임시 터널 (cloudflared).

`cloudflared tunnel --url http://127.0.0.1:포트` 를 실행하면 Cloudflare 가
https://<임의이름>.trycloudflare.com 주소를 만들어 준다. 계정·도메인이 없어도 되고 무료다.
주소는 실행할 때마다 바뀐다.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import urllib.request
from typing import Callable

from .userconfig import config_dir

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
RELEASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"


class TunnelError(RuntimeError):
    pass


def _bin_dir() -> str:
    return os.path.join(config_dir(), "bin")


def _download_name() -> tuple[str, str]:
    """(내려받을 파일 이름, 저장할 실행 파일 이름)"""
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    if sys.platform == "win32":
        return f"cloudflared-windows-{arch}.exe", "cloudflared.exe"
    if sys.platform == "darwin":
        return f"cloudflared-darwin-{arch}.tgz", "cloudflared"
    return f"cloudflared-linux-{arch}", "cloudflared"


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    local = os.path.join(_bin_dir(), _download_name()[1])
    return local if os.path.isfile(local) else None


def download_cloudflared(log: Callable[[str], None] = print) -> str:
    remote, exe = _download_name()
    os.makedirs(_bin_dir(), exist_ok=True)
    target = os.path.join(_bin_dir(), exe)
    tmp = os.path.join(_bin_dir(), remote + ".part")
    log(f"cloudflared 내려받는 중... ({remote})")
    try:
        with urllib.request.urlopen(RELEASE + remote, timeout=120) as res, open(tmp, "wb") as f:
            shutil.copyfileobj(res, f)
    except OSError as exc:
        raise TunnelError(f"cloudflared 를 내려받지 못했습니다: {exc}\n"
                          "https://github.com/cloudflare/cloudflared/releases 에서 직접 받아 설치해 주세요.") from exc
    if remote.endswith(".tgz"):
        with tarfile.open(tmp) as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("cloudflared"))
            member.name = exe
            tar.extract(member, _bin_dir())
        os.unlink(tmp)
    else:
        os.replace(tmp, target)
    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


class Tunnel:
    def __init__(self, port: int, exe: str | None = None) -> None:
        self.port = port
        self.exe = exe
        self.url: str | None = None
        self.proc: subprocess.Popen | None = None
        self._found = threading.Event()

    def start(self, log: Callable[[str], None] = print, timeout: float = 40) -> str:
        exe = self.exe or find_cloudflared() or download_cloudflared(log)
        log("인터넷 주소 만드는 중 (Cloudflare 터널)...")
        self.proc = subprocess.Popen(
            [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace")
        threading.Thread(target=self._read, daemon=True).start()
        if not self._found.wait(timeout) or not self.url:
            self.stop()
            raise TunnelError("Cloudflare 터널 주소를 받지 못했습니다. 인터넷 연결을 확인하세요.")
        return self.url

    def _read(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            m = URL_RE.search(line)
            if m and not self.url:
                self.url = m.group(0)
                self._found.set()
        self._found.set()  # 프로세스가 끝나면 기다리기를 멈춘다

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
