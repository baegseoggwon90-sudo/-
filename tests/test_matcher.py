"""AI 자동 추천 테스트 — Claude 와 픽사베이는 가짜로 대신한다."""

import json
import os
import re
import shutil
import subprocess

import pytest

from kbroll import ffmpeg_util, matcher
from kbroll.matcher import AutoOptions, Caption, Pixabay, parse_subtitles, recommend

FFMPEG = ffmpeg_util.ffmpeg_exe()


def ff(*args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def test_parse_srt_and_vtt():
    srt = "1\n00:00:01,000 --> 00:00:03,500\n안녕하세요\n두 번째 줄\n\n2\n00:00:04,000 --> 00:00:05,000\n<i>김치</i>\n"
    caps = parse_subtitles(srt)
    assert [(c.start, c.end, c.text) for c in caps] == [(1.0, 3.5, "안녕하세요 두 번째 줄"), (4.0, 5.0, "김치")]
    vtt = "WEBVTT\n\n00:01.000 --> 00:02.000\n서울\n"
    assert parse_subtitles(vtt)[0].text == "서울"
    assert matcher.narration_between(caps, 3, 4.5) == "안녕하세요 두 번째 줄 김치"


class FakeAI:
    def __init__(self):
        self.calls = []

    def ask_json(self, system, content, schema, max_tokens=16000):
        self.calls.append((system, content, schema))
        text = "\n".join(b["text"] for b in content if b["type"] == "text")
        if schema is matcher.CLIP_SCHEMA:
            return {"description": "서울 도심 거리", "tags_en": ["seoul", "street"], "in_korea": True}
        if schema is matcher.SCENE_SCHEMA:
            scenes = []
            for i in map(int, re.findall(r"장면 (\d+):", text)):
                base = {"index": i, "narration": "", "foreign": False, "mismatch": False,
                        "replace": False, "reason": "", "wanted": "", "query_en": ""}
                if i == 1:
                    base.update(foreign=True, replace=True, reason="영어 간판", wanted="서울 거리",
                                query_en="seoul street")
                if i == 2:
                    base.update(mismatch=True, replace=True, reason="나레이션은 김치",
                                wanted="김치 담그기", query_en="korean kimchi")
                scenes.append(base)
            return {"topic": "한국 소개", "scenes": scenes}
        if schema is matcher.PICK_SCHEMA:
            if "서울 거리" in text:
                return {"choice": "L:seoul.mp4", "score": 90, "reason": "내 자료가 딱 맞음"}
            return {"choice": "P:1", "score": 80, "reason": "김치 영상"}
        raise AssertionError(schema)


@pytest.fixture
def media(tmp_path):
    src = str(tmp_path / "orig.mp4")
    ff("-f", "lavfi", "-i", "color=red:s=320x180:r=25:d=2", "-f", "lavfi", "-i", "color=blue:s=320x180:r=25:d=2",
       "-f", "lavfi", "-i", "color=white:s=320x180:r=25:d=2",
       "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v]", "-map", "[v]",
       "-c:v", "libx264", "-preset", "ultrafast", src)
    clips = tmp_path / "clips"
    clips.mkdir()
    ff("-f", "lavfi", "-i", "color=green:s=320x180:r=25:d=3", "-c:v", "libx264", "-preset", "ultrafast",
       str(clips / "seoul.mp4"))
    px_video = str(tmp_path / "px.mp4")
    ff("-f", "lavfi", "-i", "color=orange:s=640x360:r=25:d=5", "-c:v", "libx264", "-preset", "ultrafast", px_video)
    return src, str(clips), px_video, tmp_path


def fake_pixabay(tmp_path, px_video):
    requests = []

    def http_get(url):
        requests.append(url)
        if url.startswith(Pixabay.API):
            return json.dumps({"hits": [{
                "id": 123, "pageURL": "https://pixabay.com/videos/id-123/", "tags": "kimchi, korean food",
                "duration": 5, "user": "someone",
                "videos": {"medium": {"url": "https://cdn/x_medium.mp4", "width": 640, "height": 360,
                                      "thumbnail": "https://cdn/x.jpg"},
                           "large": {"url": "https://cdn/x_large.mp4", "width": 1920, "height": 1080}},
            }]}).encode()
        return b"\xff\xd8fakejpeg"

    def http_save(url, path):
        requests.append(url)
        shutil.copy(px_video, path)

    return Pixabay("KEY", str(tmp_path / "pxcache"), http_get, http_save), requests


def test_recommend_picks_library_and_pixabay(media):
    src, clips, px_video, tmp = media
    ai = FakeAI()
    px, requests = fake_pixabay(tmp, px_video)
    scenes = [{"s": 0, "e": 2}, {"s": 2, "e": 4}, {"s": 4, "e": 6}]
    captions = [Caption(0, 2, "안녕하세요"), Caption(4, 6, "김치를 담급니다")]
    result = recommend(ai, src, scenes, captions, clips, str(tmp), AutoOptions(), px, log=lambda _: None)

    recs = result["scenes"]
    assert result["topic"] == "한국 소개"
    assert recs[0]["clip"] is None and not recs[0]["replace"]
    assert recs[1]["clip"] == "seoul.mp4" and recs[1]["source"] == "library"
    assert recs[2]["clip"] == "pixabay_123.mp4" and recs[2]["credit"]["user"] == "someone"
    # 원본(320x180)보다 크지 않은 파일 중 가장 큰 것 → 180p 넘는 게 없으니 가장 작은 medium
    assert "https://cdn/x_medium.mp4" in requests
    assert os.path.exists(os.path.join(clips, "pixabay_123.mp4"))
    # 장면 판단 요청에 대본과 장면 사진이 들어갔는지
    scene_call = next(c for c in ai.calls if c[2] is matcher.SCENE_SCHEMA)
    assert "김치를 담급니다" in scene_call[0][1]["text"]
    assert sum(b["type"] == "image" for b in scene_call[1]) == 3
    # 내 자료 설명은 저장되어 다음에는 다시 묻지 않는다
    n_clip_calls = sum(c[2] is matcher.CLIP_SCHEMA for c in ai.calls)
    recommend(ai, src, scenes, captions, clips, str(tmp), AutoOptions(use_pixabay=False), None,
              log=lambda _: None)
    assert sum(c[2] is matcher.CLIP_SCHEMA for c in ai.calls) == n_clip_calls == 1


def test_low_score_is_not_replaced(media):
    src, clips, px_video, tmp = media

    class Picky(FakeAI):
        def ask_json(self, system, content, schema, max_tokens=16000):
            out = super().ask_json(system, content, schema, max_tokens)
            if schema is matcher.PICK_SCHEMA:
                out["score"] = 20
            return out

    result = recommend(Picky(), src, [{"s": 0, "e": 2}, {"s": 2, "e": 4}], [], clips, str(tmp),
                       AutoOptions(use_pixabay=False), None, log=lambda _: None)
    assert result["scenes"][1]["replace"] and result["scenes"][1]["clip"] is None


def test_pixabay_search_is_cached(tmp_path, media):
    _, _, px_video, _ = media
    px, requests = fake_pixabay(tmp_path, px_video)
    first = px.search("korean kimchi")
    second = px.search("korean kimchi")
    assert first == second and len([r for r in requests if r.startswith(Pixabay.API)]) == 1
    assert "key=KEY" in requests[0] and "q=korean+kimchi" in requests[0]
