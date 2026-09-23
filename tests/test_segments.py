import pytest

from kbroll.segments import Segment, dump_segments, normalize, parse_segments, parse_time


def test_parse_time_formats():
    assert parse_time("83.5") == 83.5
    assert parse_time("1:23.5") == 83.5
    assert parse_time("00:01:23,5") == 83.5
    with pytest.raises(ValueError):
        parse_time("")


def test_parse_segments_with_header_bom_and_comments():
    text = "start,end,clip,clip_start\n# 주석\n1:10,1:18,,\n5,8,서울.mp4,12\n"
    segs = parse_segments(text)
    assert [(s.start, s.end, s.clip, s.clip_start) for s in segs] == [
        (5, 8, "서울.mp4", 12.0),
        (70, 78, None, None),
    ]


def test_parse_segments_rejects_reversed():
    with pytest.raises(ValueError):
        parse_segments("10,5")


def test_normalize_merges_adjacent_auto_segments():
    segs = normalize([Segment(4, 7), Segment(7, 9), Segment(20, 21)])
    assert [(s.start, s.end) for s in segs] == [(4, 9), (20, 21)]


def test_normalize_trims_overlap_with_different_clip():
    segs = normalize([Segment(4, 8, "a.mp4"), Segment(6, 10, "b.mp4")])
    assert [(s.start, s.end, s.clip) for s in segs] == [(4, 8, "a.mp4"), (8, 10, "b.mp4")]


def test_dump_roundtrip():
    segs = [Segment(1.5, 3.25, "x, y.mp4", 2)]
    assert parse_segments(dump_segments(segs)) == segs
