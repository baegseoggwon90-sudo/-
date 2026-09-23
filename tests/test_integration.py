"""ffmpeg 로 합성 영상을 만들어 실제 교체를 해 보는 테스트."""

import os
import subprocess

import pytest

from kbroll import ffmpeg_util
from kbroll.replace import replace_segments
from kbroll.scan import scan
from kbroll.segments import Segment

try:
    FFMPEG = ffmpeg_util.ffmpeg_exe()
except ffmpeg_util.FFmpegError:  # pragma: no cover
    FFMPEG = None

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg 없음")


def ff(*args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def frame_rgb(path, t, x, y):
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", path,
         "-frames:v", "1", "-vf", f"crop=2:2:{x}:{y},scale=1:1", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, check=True).stdout
    return tuple(out[:3])


def audio_md5(path):
    out = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path,
                          "-map", "0:a", "-c", "copy", "-f", "md5", "-"],
                         stdout=subprocess.PIPE, check=True, text=True).stdout
    return out.strip()


@pytest.fixture
def media(tmp_path):
    src = str(tmp_path / "orig.mp4")
    ff("-f", "lavfi", "-i", "color=red:s=320x180:r=25:d=2",
       "-f", "lavfi", "-i", "color=blue:s=320x180:r=25:d=2",
       "-f", "lavfi", "-i", "sine=f=440:d=4",
       "-filter_complex",
       "[0][1]concat=n=2:v=1:a=0,drawbox=x=100:y=150:w=120:h=6:color=white:t=fill,"
       "drawbox=x=97:y=147:w=126:h=12:color=black:t=3[v]",
       "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast",
       "-c:a", "aac", "-shortest", src)
    clips = tmp_path / "clips"
    clips.mkdir()
    ff("-f", "lavfi", "-i", "color=green:s=640x480:r=30:d=1", "-c:v", "libx264",
       "-preset", "ultrafast", str(clips / "k.mp4"))
    return src, str(clips), tmp_path


def test_replace_keeps_audio_and_subtitle(media):
    src, clips, tmp = media
    out = str(tmp / "out.mp4")
    replace_segments(src, [Segment(2, 4)], out, clips_dir=clips, preset="ultrafast",
                     log=lambda _: None)
    # 교체 전 구간은 원본(빨강), 교체 구간은 한국 영상(초록)
    r, g, b = frame_rgb(out, 1.0, 10, 10)
    assert r > 200 and g < 60
    r, g, b = frame_rgb(out, 3.0, 10, 10)
    assert g > 100 and r < 60 and b < 60
    # 자막(흰 막대)은 교체 구간에서도 남아 있어야 한다
    assert min(frame_rgb(out, 3.0, 150, 151)) > 200
    # 음성은 원본과 비트 단위로 동일
    assert audio_md5(out) == audio_md5(src)


def test_scan_writes_review_page(media):
    src, clips, tmp = media
    page = str(tmp / "review.html")
    scenes = scan(src, page, clips_dir=clips, log=lambda _: None)
    assert len(scenes) == 2
    assert abs(scenes[1].start - 2.0) < 0.1
    html = open(page, encoding="utf-8").read()
    assert "k.mp4" in html and "data:image/jpeg;base64," in html
    assert os.path.getsize(page) > 1000
