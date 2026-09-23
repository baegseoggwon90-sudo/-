"""ffmpeg 실행 파일 찾기와 영상 정보 조회."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts"}


class FFmpegError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    """환경변수 KBROLL_FFMPEG > PATH 의 ffmpeg > imageio-ffmpeg 내장 ffmpeg 순서로 찾는다."""
    env = os.environ.get("KBROLL_FFMPEG")
    if env:
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - 설치 환경에 따라 다름
        raise FFmpegError(
            "ffmpeg 를 찾을 수 없습니다. ffmpeg 를 설치하거나 "
            "'pip install imageio-ffmpeg' 를 실행하세요."
        ) from exc


def run(args: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_exe(), "-hide_banner", *args]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc


@dataclass
class MediaInfo:
    path: str
    duration: float | None
    width: int
    height: int
    fps: float
    has_audio: bool
    is_image: bool = False


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video: (.*)")
_SIZE_RE = re.compile(r"[ ,](\d{2,5})x(\d{2,5})[ ,\]]")
_FPS_RE = re.compile(r"([\d.]+)(k?) fps")
_TBR_RE = re.compile(r"([\d.]+)(k?) tbr")


def _parse_rate(match: re.Match | None) -> float | None:
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2) == "k":
        value *= 1000
    return value


def parse_info(path: str, stderr: str) -> MediaInfo:
    """`ffmpeg -i <file>` 의 stderr 출력에서 영상 정보를 추출한다 (ffprobe 불필요)."""
    duration = None
    m = _DURATION_RE.search(stderr)
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    vm = _VIDEO_RE.search(stderr)
    if not vm:
        raise FFmpegError(f"영상 스트림을 찾을 수 없습니다: {path}")
    vline = vm.group(1) + " "
    sm = _SIZE_RE.search(vline)
    if not sm:
        raise FFmpegError(f"해상도를 읽을 수 없습니다: {path}")
    width, height = int(sm.group(1)), int(sm.group(2))

    # 회전 메타데이터(세로 촬영 영상)가 있으면 가로/세로를 바꾼다.
    rot = re.search(r"rotat\w*\s*(?:of|:)\s*(-?\d+(?:\.\d+)?)", stderr)
    if rot and abs(round(float(rot.group(1)))) % 180 == 90:
        width, height = height, width

    fps = _parse_rate(_FPS_RE.search(vline)) or _parse_rate(_TBR_RE.search(vline)) or 30.0
    has_audio = re.search(r"Stream #\d+:\d+.*?: Audio:", stderr) is not None
    is_image = os.path.splitext(path)[1].lower() in IMAGE_EXTS
    return MediaInfo(path, duration, width, height, fps, has_audio, is_image)


@lru_cache(maxsize=256)
def probe(path: str) -> MediaInfo:
    if not os.path.exists(path):
        raise FFmpegError(f"파일이 없습니다: {path}")
    proc = run(["-i", path])
    return parse_info(path, proc.stderr or "")


def list_media(folder: str) -> list[str]:
    """폴더 안의 영상/이미지 파일을 이름순으로 반환한다."""
    exts = VIDEO_EXTS | IMAGE_EXTS
    files = [
        os.path.join(folder, name)
        for name in sorted(os.listdir(folder))
        if os.path.splitext(name)[1].lower() in exts and not name.startswith(".")
    ]
    return files
