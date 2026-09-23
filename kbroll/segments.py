"""교체 구간(segment) 파일 읽기/쓰기.

CSV 형식 (첫 줄 머리글은 선택, '#' 으로 시작하는 줄은 주석):

    start,end,clip,clip_start
    00:01:10.5,00:01:18,,
    83.2,90,한국영상/seoul.mp4,12

- start/end : 초(83.2) 또는 시:분:초(00:01:23.2) 또는 분:초(1:23.2)
- clip      : 이 구간에 쓸 한국 영상 (비우면 --clips 폴더에서 자동 선택)
- clip_start: 한국 영상의 몇 초 지점부터 쓸지 (비우면 자동)
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass


@dataclass
class Segment:
    start: float
    end: float
    clip: str | None = None
    clip_start: float | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start


def parse_time(text: str) -> float:
    text = text.strip().replace(",", ".")
    if not text:
        raise ValueError("시간 값이 비어 있습니다")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"잘못된 시간 형식: {text}")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    if total < 0:
        raise ValueError(f"음수 시간: {text}")
    return total


def format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _looks_like_header(row: list[str]) -> bool:
    return bool(row) and row[0].strip().lower() in {"start", "시작"}


def parse_segments(text: str) -> list[Segment]:
    segments: list[Segment] = []
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for lineno, row in enumerate(csv.reader(io.StringIO("\n".join(lines))), 1):
        if _looks_like_header(row):
            continue
        row = [c.strip() for c in row]
        if len(row) < 2:
            raise ValueError(f"{lineno}번째 줄: 시작,끝 시간이 필요합니다 -> {row}")
        start, end = parse_time(row[0]), parse_time(row[1])
        if end <= start:
            raise ValueError(f"{lineno}번째 줄: 끝 시간이 시작 시간보다 커야 합니다 -> {row}")
        clip = row[2] if len(row) > 2 and row[2] else None
        clip_start = parse_time(row[3]) if len(row) > 3 and row[3] else None
        segments.append(Segment(start, end, clip, clip_start))
    return normalize(segments)


def load_segments(path: str) -> list[Segment]:
    with open(path, encoding="utf-8-sig") as f:
        return parse_segments(f.read())


def normalize(segments: list[Segment], gap: float = 0.05) -> list[Segment]:
    """시간순 정렬 후, 붙어 있거나 겹치는 구간 중 같은 영상 지정인 것은 하나로 합친다.

    겹치지만 서로 다른 영상을 지정한 구간은 뒤 구간의 시작을 앞 구간 끝으로 맞춘다.
    """
    result: list[Segment] = []
    for seg in sorted(segments, key=lambda s: (s.start, s.end)):
        seg = Segment(seg.start, seg.end, seg.clip, seg.clip_start)
        if result:
            prev = result[-1]
            touching = seg.start <= prev.end + gap
            if touching and seg.clip == prev.clip and seg.clip_start is None:
                prev.end = max(prev.end, seg.end)
                continue
            if seg.start < prev.end:
                seg.start = prev.end
                if seg.end <= seg.start:
                    continue
        result.append(seg)
    return result


def dump_segments(segments: list[Segment]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["start", "end", "clip", "clip_start"])
    for s in segments:
        writer.writerow(
            [
                format_time(s.start),
                format_time(s.end),
                s.clip or "",
                "" if s.clip_start is None else f"{s.clip_start:g}",
            ]
        )
    return out.getvalue()
