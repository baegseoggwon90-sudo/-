import pytest

from kbroll.ffmpeg_util import MediaInfo, parse_info
from kbroll.replace import SubtitleOptions, assign_clips, build_filtergraph, parse_region
from kbroll.segments import Segment


def fake_probe(durations):
    def probe(path):
        return MediaInfo(path, durations.get(path), 1920, 1080, 30.0, False,
                         is_image=path.endswith(".png"))
    return probe


def test_assign_clips_cycles_and_continues_offsets():
    segs = [Segment(0, 4), Segment(10, 13), Segment(20, 24)]
    plan = assign_clips(segs, ["a.mp4", "b.png"], probe=fake_probe({"a.mp4": 10}))
    assert [(r.clip, r.clip_start) for r in plan] == [
        ("a.mp4", 0.0), ("b.png", 0.0), ("a.mp4", 4.0)]


def test_assign_clips_restarts_when_remaining_too_short():
    segs = [Segment(0, 8), Segment(10, 15)]
    plan = assign_clips(segs, ["a.mp4"], probe=fake_probe({"a.mp4": 10}))
    assert plan[1].clip_start == 0.0


def test_assign_clips_requires_clip_source():
    with pytest.raises(ValueError):
        assign_clips([Segment(0, 1)], [], probe=fake_probe({}))


def test_filtergraph_contains_replacement_and_subtitle_overlay():
    plan = assign_clips([Segment(4, 7), Segment(9, 10)], ["a.mp4"],
                        probe=fake_probe({"a.mp4": 100}))
    graph = build_filtergraph(1920, 1080, 30, plan, SubtitleOptions())
    assert "[1:v]fps=30," in graph and "[2:v]fps=30," in graph
    assert "setpts=PTS-STARTPTS+4/TB" in graph
    assert "enable='between(t,4,7)+between(t,9,10)'" in graph
    assert "alphamerge" in graph
    assert graph.strip().endswith("[vout]")


def test_filtergraph_without_subtitles():
    plan = assign_clips([Segment(1, 2)], ["a.mp4"], probe=fake_probe({"a.mp4": 5}))
    graph = build_filtergraph(1280, 720, 25, plan, SubtitleOptions(mode="none"))
    assert "split" not in graph and "alphamerge" not in graph


def test_parse_region_validation():
    assert parse_region("0, 0.8, 1, 0.2") == (0, 0.8, 1, 0.2)
    with pytest.raises(ValueError):
        parse_region("0,0.9,1,0.2")


def test_parse_info_from_ffmpeg_output():
    stderr = """
  Duration: 00:01:02.50, start: 0.000000, bitrate: 317 kb/s
  Stream #0:0[0x1](und): Video: h264 (High), yuv420p(progressive), 1920x1080 [SAR 1:1 DAR 16:9], 29.97 fps, 29.97 tbr
  Stream #0:1[0x2](und): Audio: aac (LC), 44100 Hz, stereo
"""
    info = parse_info("x.mp4", stderr)
    assert (info.duration, info.width, info.height, info.fps, info.has_audio) == (
        62.5, 1920, 1080, 29.97, True)
