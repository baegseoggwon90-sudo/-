"""CapCut 초안(pycapcut) 만들기 테스트."""

import json
import os
import subprocess

import pytest

pytest.importorskip("pycapcut")

from kbroll import ffmpeg_util
from kbroll.capcut import cover_scale, export_draft, region_transform_y
from kbroll.matcher import Caption
from kbroll.replace import SubtitleOptions, assign_clips
from kbroll.segments import Segment

FFMPEG = ffmpeg_util.ffmpeg_exe()


def ff(*args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def test_geometry_helpers():
    assert cover_scale(1920, 1080, 1920, 1080) == 1
    assert cover_scale(1080, 1920, 1920, 1080) == pytest.approx((1920 / 1080) / (1080 / 1920))
    assert region_transform_y(0, 1080, 1080) == 0          # 화면 전체 = 가운데
    assert region_transform_y(1080 - 200, 200, 1080) < -0.8  # 아래쪽 띠 = 음수(아래)


@pytest.fixture
def media(tmp_path):
    src = str(tmp_path / "orig.mp4")
    ff("-f", "lavfi", "-i", "color=red:s=640x360:r=30:d=6", "-f", "lavfi", "-i", "sine=d=6",
       "-vf", "drawbox=x=200:y=320:w=240:h=12:color=white:t=fill", "-c:v", "libx264", "-preset", "ultrafast",
       "-c:a", "aac", "-shortest", src)
    clips = tmp_path / "clips"
    clips.mkdir()
    ff("-f", "lavfi", "-i", "color=green:s=360x640:r=30:d=1.5", "-c:v", "libx264", "-preset", "ultrafast",
       str(clips / "tall.mp4"))
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    return src, str(clips), str(drafts)


def load(draft_path):
    with open(os.path.join(draft_path, "draft_content.json"), encoding="utf-8") as f:
        data = json.load(f)
    return {t["name"]: t for t in data["tracks"]}, data


@pytest.mark.parametrize("mode", ["key", "band", "text", "none"])
def test_export_draft(media, mode):
    src, clips, drafts = media
    plan = assign_clips([Segment(2, 5)], [os.path.join(clips, "tall.mp4")])
    captions = [Caption(1.5, 3.0, "첫 문장"), Caption(3.0, 5.5, "둘째 문장")]
    result = export_draft(src, plan, drafts, f"t_{mode}", SubtitleOptions(mode=mode),
                          captions=captions, log=lambda _: None)
    tracks, data = load(result.draft_path)
    assert data["canvas_config"]["width"] == 640 and data["canvas_config"]["height"] == 360

    main = tracks["원본영상"]["segments"]
    assert len(main) == 1 and main[0]["volume"] == 1.0            # 나레이션은 원본 그대로

    korean = tracks["한국영상"]["segments"]
    # 1.5초짜리 영상으로 3초를 채우려면 두 번 반복
    assert [s["target_timerange"] for s in korean] == [
        {"start": 2_000_000, "duration": 1_500_000}, {"start": 3_500_000, "duration": 1_500_000}]
    assert all(s["volume"] == 0.0 for s in korean)                # 한국 영상 소리는 끔
    assert korean[0]["clip"]["scale"]["x"] > 3                    # 세로 영상 → 꽉 채우게 확대

    if mode in ("key", "band"):
        piece = tracks["자막보존"]["segments"]
        assert len(piece) == 1 and piece[0]["clip"]["transform"]["y"] < -0.5
        media_files = os.listdir(os.path.join(result.draft_path, "kbroll_media"))
        assert media_files == ["subtitle_000.mov" if mode == "key" else "subtitle_000.mp4"]
    else:
        assert "자막보존" not in tracks
    if mode == "text":
        texts = tracks["자막"]["segments"]
        # 교체 구간(2~5초)에 걸친 부분만 잘라서 넣는다
        assert [s["target_timerange"] for s in texts] == [
            {"start": 2_000_000, "duration": 1_000_000}, {"start": 3_000_000, "duration": 2_000_000}]


def test_text_mode_requires_captions(media):
    src, clips, drafts = media
    plan = assign_clips([Segment(2, 5)], [os.path.join(clips, "tall.mp4")])
    with pytest.raises(ValueError):
        export_draft(src, plan, drafts, "x", SubtitleOptions(mode="text"), log=lambda _: None)


def test_key_piece_is_transparent(media):
    src, clips, drafts = media
    plan = assign_clips([Segment(2, 5)], [os.path.join(clips, "tall.mp4")])
    result = export_draft(src, plan, drafts, "alpha", SubtitleOptions(mode="key", dark=0),
                          log=lambda _: None)
    piece = os.path.join(result.draft_path, "kbroll_media", "subtitle_000.mov")
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", piece], capture_output=True, text=True).stderr
    assert "yuva444p" in out


def test_pack_draft_zip_rewrites_paths(media, tmp_path):
    import zipfile
    from kbroll.capcut import pack_draft_zip

    src, clips, drafts = media
    plan = assign_clips([Segment(2, 5)], [os.path.join(clips, "tall.mp4")])
    result = export_draft(src, plan, drafts, "zipme", SubtitleOptions(mode="band"), log=lambda _: None)
    win = r"C:\Users\kim\AppData\Local\CapCut\User Data\Projects\com.lveditor.draft"
    zf = zipfile.ZipFile(pack_draft_zip(result.draft_path, str(tmp_path / "d.zip"), win))
    names = set(zf.namelist())
    assert {"zipme/draft_content.json", "zipme/kbroll_media/orig.mp4", "zipme/kbroll_media/tall.mp4",
            "zipme/kbroll_media/subtitle_000.mp4"} <= names
    paths = [m["path"] for m in json.loads(zf.read("zipme/draft_content.json"))["materials"]["videos"]]
    assert all(p.startswith(win + "\\zipme\\kbroll_media\\") for p in paths)
    mac = "/Users/kim/Movies/CapCut/User Data/Projects/com.lveditor.draft"
    zf2 = zipfile.ZipFile(pack_draft_zip(result.draft_path, str(tmp_path / "m.zip"), mac))
    paths = [m["path"] for m in json.loads(zf2.read("zipme/draft_content.json"))["materials"]["videos"]]
    assert all(p.startswith(mac + "/zipme/kbroll_media/") for p in paths)
