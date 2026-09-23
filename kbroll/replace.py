"""지정한 구간의 화면만 한국 영상으로 교체한다.

- 원본 음성 트랙은 재인코딩 없이 그대로 복사한다 (나레이션 보존).
- 구간 밖의 화면은 원본 프레임을 그대로 사용한다.
- 화면에 박힌(하드) 자막은 원본의 자막 영역을 교체된 화면 위에 다시 얹어서 보존한다.
"""

from __future__ import annotations

import os
import random
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable

from . import ffmpeg_util
from .ffmpeg_util import FFmpegError, MediaInfo
from .segments import Segment, format_time

SUBTITLE_MODES = ("key", "band", "none")


@dataclass
class SubtitleOptions:
    """하드 자막 보존 설정.

    mode:
      key  - 자막 영역에서 밝은 글자(+테두리)만 뽑아서 얹는다 (흰/노란 글자 + 검은 테두리 자막에 적합)
      band - 자막 영역 띠 전체를 원본 그대로 얹는다 (자막 배경 박스가 있는 경우에 적합)
      none - 자막을 얹지 않는다 (자막이 화면에 박혀 있지 않은 경우)
    region: 자막 영역 (x, y, w, h) — 화면 크기에 대한 비율(0~1)
    threshold: key 모드에서 글자로 판단할 밝기(0~255)
    outline: key 모드에서 글자 주변으로 넓힐 테두리 두께(px)
    dark: key 모드에서 글자 테두리로 판단할 어두운 정도(0~255). 밝은 픽셀 중 근처에
          이만큼 어두운 픽셀(테두리)이 있는 것만 글자로 본다 → 하늘/흰 벽 같은 밝은 배경 제외.
          0 이면 이 검사를 끈다 (테두리 없는 자막).
    """

    mode: str = "key"
    region: tuple[float, float, float, float] = (0.0, 0.72, 1.0, 0.28)
    threshold: int = 200
    outline: int = 4
    dark: int = 80


@dataclass
class Replacement:
    segment: Segment
    clip: str
    clip_start: float
    info: MediaInfo


def parse_region(text: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in text.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise ValueError("자막 영역은 x,y,w,h 4개 값이어야 합니다 (예: 0,0.72,1,0.28)")
    x, y, w, h = parts
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1):
        raise ValueError("자막 영역 값은 0~1 사이의 비율이어야 합니다")
    if x + w > 1.0001 or y + h > 1.0001:
        raise ValueError("자막 영역이 화면 밖으로 나갑니다")
    return x, y, w, h


def resolve_clip_path(clip: str, clips_dir: str | None) -> str:
    if os.path.isabs(clip) or os.path.exists(clip) or not clips_dir:
        return clip
    return os.path.join(clips_dir, clip)


def assign_clips(
    segments: list[Segment],
    clip_files: list[str],
    *,
    clips_dir: str | None = None,
    shuffle: bool = False,
    seed: int | None = None,
    probe: Callable[[str], MediaInfo] = ffmpeg_util.probe,
) -> list[Replacement]:
    """각 구간에 사용할 한국 영상과 시작 지점을 정한다.

    같은 영상을 여러 번 쓸 때는 이전에 쓴 부분 다음부터 이어서 써서 같은 장면이 반복되지 않게 한다.
    """
    pool = list(clip_files)
    if shuffle:
        random.Random(seed).shuffle(pool)
    used: dict[str, float] = {}
    plan: list[Replacement] = []
    auto_index = 0
    for seg in segments:
        if seg.clip:
            clip = resolve_clip_path(seg.clip, clips_dir)
        else:
            if not pool:
                raise ValueError(
                    f"{format_time(seg.start)} 구간에 쓸 한국 영상이 없습니다. "
                    "--clips 폴더를 지정하거나 구간 파일에 영상을 적어주세요."
                )
            clip = pool[auto_index % len(pool)]
            auto_index += 1
        info = probe(clip)
        if seg.clip_start is not None:
            start = seg.clip_start
        elif info.is_image or not info.duration:
            start = 0.0
        else:
            start = used.get(clip, 0.0) % info.duration
            if info.duration - start < seg.duration:
                start = 0.0
        used[clip] = start + seg.duration
        plan.append(Replacement(seg, clip, start, info))
    return plan


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def _num(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def build_filtergraph(
    width: int,
    height: int,
    fps: float,
    plan: list[Replacement],
    subs: SubtitleOptions,
) -> str:
    """ffmpeg -filter_complex 문자열을 만든다. 입력 0 = 원본, 입력 i+1 = plan[i] 의 영상."""
    chains: list[str] = []
    use_subs = subs.mode != "none" and plan
    base = "[0:v]"
    if use_subs:
        chains.append("[0:v]split=2[base0][subsrc]")
        base = "[base0]"

    for i, rep in enumerate(plan, 1):
        s, d = rep.segment.start, rep.segment.duration
        chains.append(
            f"[{i}:v]fps={_num(fps)},"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,format=yuv420p,"
            f"trim=duration={_num(d)},setpts=PTS-STARTPTS+{_num(s)}/TB[r{i}]"
        )
        out = f"[v{i}]"
        chains.append(
            f"{base}[r{i}]overlay=0:0:eof_action=pass:"
            f"enable='between(t,{_num(s)},{_num(s + d)})'{out}"
        )
        base = out

    if use_subs:
        fx, fy, fw, fh = subs.region
        x, y = _even(fx * width) if fx > 0 else 0, _even(fy * height) if fy > 0 else 0
        w = min(_even(fw * width), width - x)
        h = min(_even(fh * height), height - y)
        crop = f"[subsrc]crop={w}:{h}:{x}:{y}"
        if subs.mode == "key":
            dil = ",dilation" * max(0, subs.outline)
            bright = f"lut=y='if(gte(val,{int(subs.threshold)}),255,0)'"
            chains.append(f"{crop},format=yuv420p,split=2[sc][sm]")
            if subs.dark > 0:
                # 밝은 글자 AND (어두운 테두리 근처) — 글자 획 두께를 덮도록 화면 높이의 1% 만큼 넓힌다
                reach = ",dilation" * max(2, round(height / 100))
                chains.append("[sm]format=gray,split=2[sb][sd]")
                chains.append(f"[sb]{bright}[bright]")
                chains.append(f"[sd]lut=y='if(lte(val,{int(subs.dark)}),255,0)'{reach}[near]")
                chains.append(f"[bright][near]blend=all_mode=multiply{dil},"
                              "boxblur=luma_radius=1:luma_power=1[mask]")
            else:
                chains.append(f"[sm]format=gray,{bright}{dil},"
                              "boxblur=luma_radius=1:luma_power=1[mask]")
            chains.append("[sc][mask]alphamerge[subs]")
        else:
            chains.append(f"{crop}[subs]")
        enable = "+".join(
            f"between(t,{_num(r.segment.start)},{_num(r.segment.end)})" for r in plan
        )
        chains.append(f"{base}[subs]overlay={x}:{y}:enable='{enable}'[vsub]")
        base = "[vsub]"

    chains.append(f"{base}format=yuv420p[vout]")
    return ";\n".join(chains)


def _clip_input(rep: Replacement, fps: float) -> list[str]:
    if rep.info.is_image:
        return ["-loop", "1", "-framerate", _num(fps), "-i", rep.clip]
    args = ["-stream_loop", "-1"]
    if rep.clip_start > 0:
        args += ["-ss", _num(rep.clip_start)]
    return args + ["-i", rep.clip]


def build_command(
    source: str,
    output: str,
    plan: list[Replacement],
    info: MediaInfo,
    subs: SubtitleOptions,
    *,
    crf: int = 18,
    preset: str = "medium",
    graph_file: str,
) -> list[str]:
    args: list[str] = ["-y", "-i", source]
    for rep in plan:
        args += _clip_input(rep, info.fps)
    args += ["-filter_complex_script", graph_file, "-map", "[vout]"]
    if info.has_audio:
        args += ["-map", "0:a", "-c:a", "copy"]
    # 자막 스트림(소프트 자막)은 컨테이너가 같을 때만 그대로 복사
    if os.path.splitext(source)[1].lower() == os.path.splitext(output)[1].lower():
        args += ["-map", "0:s?", "-c:s", "copy"]
    args += [
        "-map_metadata", "0",
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
        "-fps_mode", "passthrough",
    ]
    if output.lower().endswith((".mp4", ".mov", ".m4v")):
        args += ["-movflags", "+faststart"]
    args.append(output)
    return args


def replace_segments(
    source: str,
    segments: list[Segment],
    output: str,
    *,
    clips_dir: str | None = None,
    subs: SubtitleOptions | None = None,
    shuffle: bool = False,
    seed: int | None = None,
    crf: int = 18,
    preset: str = "medium",
    progress: Callable[[float], None] | None = None,
    log: Callable[[str], None] = print,
) -> list[Replacement]:
    subs = subs or SubtitleOptions()
    if subs.mode not in SUBTITLE_MODES:
        raise ValueError(f"자막 모드는 {SUBTITLE_MODES} 중 하나여야 합니다")
    if os.path.abspath(source) == os.path.abspath(output):
        raise ValueError("출력 파일은 원본과 다른 이름이어야 합니다")
    info = ffmpeg_util.probe(source)
    if info.duration:
        segments = [
            Segment(s.start, min(s.end, info.duration), s.clip, s.clip_start)
            for s in segments
            if s.start < info.duration
        ]
    if not segments:
        raise ValueError("교체할 구간이 없습니다")

    clip_files = ffmpeg_util.list_media(clips_dir) if clips_dir else []
    plan = assign_clips(segments, clip_files, clips_dir=clips_dir, shuffle=shuffle, seed=seed)

    log(f"원본: {source} ({info.width}x{info.height}, {info.fps:g}fps)")
    for rep in plan:
        s = rep.segment
        log(
            f"  {format_time(s.start)} ~ {format_time(s.end)}  ->  "
            f"{os.path.basename(rep.clip)} ({rep.clip_start:g}초부터)"
        )

    graph = build_filtergraph(info.width, info.height, info.fps, plan, subs)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as gf:
        gf.write(graph)
        graph_file = gf.name
    try:
        args = build_command(
            source, output, plan, info, subs, crf=crf, preset=preset, graph_file=graph_file
        )
        _run_with_progress(args, info.duration, progress)
    finally:
        os.unlink(graph_file)
    log(f"완료: {output}")
    return plan


def _run_with_progress(
    args: list[str], duration: float | None, progress: Callable[[float], None] | None
) -> None:
    cmd = [ffmpeg_util.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
           "-nostats", "-progress", "pipe:1", *args]
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=err, text=True,
            encoding="utf-8", errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and duration and progress and value.isdigit():
                progress(min(1.0, int(value) / 1e6 / duration))
        code = proc.wait()
        if code != 0:
            err.seek(0)
            raise FFmpegError(f"ffmpeg 실패 (코드 {code}):\n{err.read()[-3000:]}")
    if progress:
        progress(1.0)


def preview_frame(
    source: str,
    at: float,
    clip: str,
    out_image: str,
    subs: SubtitleOptions | None = None,
    clip_start: float = 0.0,
) -> str:
    """원본 at 초 지점을 clip 으로 교체했을 때의 화면 한 장을 이미지로 저장한다 (자막 설정 확인용)."""
    subs = subs or SubtitleOptions()
    info = ffmpeg_util.probe(source)
    plan = [Replacement(Segment(0.0, 1.0), clip, clip_start, ffmpeg_util.probe(clip))]
    graph = build_filtergraph(info.width, info.height, info.fps, plan, subs)
    args = ["-y", "-ss", _num(max(0.0, at)), "-t", "1", "-i", source,
            *_clip_input(plan[0], info.fps),
            "-filter_complex", graph, "-map", "[vout]", "-frames:v", "1", out_image]
    proc = ffmpeg_util.run(args)
    if proc.returncode != 0 or not os.path.exists(out_image):
        raise FFmpegError(f"미리보기 실패:\n{(proc.stderr or '')[-2000:]}")
    return out_image
