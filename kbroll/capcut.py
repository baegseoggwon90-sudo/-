"""pyCapCut 으로 CapCut 편집 프로젝트(초안)를 만든다.

ffmpeg 로 완성본을 바로 뽑는 대신, CapCut 에서 열어서 계속 손볼 수 있는 초안을 만든다.

트랙 구성 (아래 → 위)
  원본영상   : 원본 영상 전체. 나레이션 음성도 이 트랙 그대로 (재인코딩 없음)
  한국영상   : 교체할 구간에만 한국 영상 (음소거, 화면을 꽉 채우게 확대)
  자막보존   : 교체 구간에서 원본의 자막 부분만 떼어 낸 조각 (음소거)
               - key  : 글자만 남긴 투명 배경 영상 (ProRes 4444 .mov)
               - band : 자막 띠 전체
  자막(텍스트): (text 모드) 자막 파일의 문장을 CapCut 텍스트로 넣음 — CapCut 에서 글꼴·색 수정 가능

생성된 초안은 Windows / Mac 용 CapCut 에서 열어서 확인하고 내보내기(Export) 하면 된다.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from . import ffmpeg_util
from .ffmpeg_util import FFmpegError
from .replace import Replacement, SubtitleOptions, subtitle_chains, subtitle_region_px

CAPCUT_SUB_MODES = ("key", "band", "text", "none")

Log = Callable[[str], None]


class CapCutError(RuntimeError):
    pass


def _pycapcut():
    try:
        import pycapcut
    except ImportError as exc:
        raise CapCutError("CapCut 초안을 만들려면 'pip install pycapcut' 을 실행하세요.") from exc
    return pycapcut


def capcut_available() -> bool:
    try:
        import pycapcut  # noqa: F401
        return True
    except Exception:
        return False


def default_draft_folder() -> str | None:
    """이 PC 에서 CapCut 초안(Drafts) 폴더를 찾는다. (CapCut 설정 > 초안 위치 에서도 확인 가능)"""
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "CapCut", "User Data", "Projects", "com.lveditor.draft"))
    home = os.path.expanduser("~")
    candidates += [
        os.path.join(home, "Movies", "CapCut", "User Data", "Projects", "com.lveditor.draft"),
        os.path.join(home, "AppData", "Local", "CapCut", "User Data", "Projects", "com.lveditor.draft"),
    ]
    return next((c for c in candidates if os.path.isdir(c)), None)


def safe_draft_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:80] or "kbroll"


def _us(seconds: float) -> int:
    return int(round(seconds * 1_000_000))


def cover_scale(mat_w: int, mat_h: int, width: int, height: int) -> float:
    """CapCut 은 기본으로 소재를 화면 안에 '맞춤'으로 넣는다. 화면을 꽉 채우려면 얼마나 키워야 하는지."""
    if not mat_w or not mat_h:
        return 1.0
    fit = min(width / mat_w, height / mat_h)
    cover = max(width / mat_w, height / mat_h)
    return cover / fit


def region_transform_y(y: int, h: int, height: int) -> float:
    """세로 위치(px)를 CapCut 위치값으로. CapCut 은 화면 가운데가 0, 위쪽이 +1, 아래쪽이 -1."""
    center = y + h / 2
    return (height / 2 - center) / (height / 2)


def render_subtitle_piece(source: str, start: float, duration: float, subs: SubtitleOptions,
                          width: int, height: int, out_dir: str, index: int) -> tuple[str, tuple]:
    """원본 영상의 [start, start+duration] 구간에서 자막 영역만 떼어 낸 영상을 만든다."""
    chains, label, box = subtitle_chains("[0:v]", subs, width, height)
    if subs.mode == "key":
        out = os.path.join(out_dir, f"subtitle_{index:03d}.mov")
        chains.append(f"{label}format=yuva444p10le[out]")
        codec = ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"]
    else:
        out = os.path.join(out_dir, f"subtitle_{index:03d}.mp4")
        chains.append(f"{label}format=yuv420p[out]")
        codec = ["-c:v", "libx264", "-crf", "16", "-preset", "fast", "-pix_fmt", "yuv420p"]
    proc = ffmpeg_util.run(["-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", source,
                            "-filter_complex", ";".join(chains), "-map", "[out]", "-an", *codec, out])
    if proc.returncode != 0 or not os.path.exists(out):
        raise FFmpegError(f"자막 조각 만들기 실패:\n{(proc.stderr or '')[-1500:]}")
    return out, box


@dataclass
class CapCutResult:
    draft_path: str
    draft_name: str
    replaced: int


def export_draft(
    source: str,
    plan: list[Replacement],
    draft_folder: str,
    draft_name: str,
    subs: SubtitleOptions,
    *,
    captions: list | None = None,
    replace_existing: bool = True,
    log: Log = print,
    progress: Callable[[float], None] | None = None,
) -> CapCutResult:
    """교체 계획(plan)대로 CapCut 초안을 만든다. subs.mode 는 key / band / text / none."""
    cc = _pycapcut()
    if subs.mode not in CAPCUT_SUB_MODES:
        raise ValueError(f"CapCut 자막 방식은 {CAPCUT_SUB_MODES} 중 하나여야 합니다")
    if subs.mode == "text" and not captions:
        raise ValueError("텍스트 자막으로 넣으려면 자막 파일(SRT/VTT)을 먼저 올려주세요")
    if not os.path.isdir(draft_folder):
        raise CapCutError(f"CapCut 초안 폴더가 없습니다: {draft_folder}\n"
                          "CapCut > 설정 > 초안 위치 에서 폴더 경로를 확인해 주세요.")

    info = ffmpeg_util.probe(source)
    width, height = info.width, info.height
    fps = max(1, int(round(info.fps)))
    folder = cc.DraftFolder(draft_folder)
    draft_name = safe_draft_name(draft_name)
    try:
        script = folder.create_draft(draft_name, width, height, fps, allow_replace=replace_existing)
    except FileExistsError as exc:
        raise CapCutError(f"같은 이름의 초안이 이미 있습니다: {draft_name}") from exc
    draft_path = os.path.join(draft_folder, draft_name)
    media_dir = os.path.join(draft_path, "kbroll_media")
    os.makedirs(media_dir, exist_ok=True)

    # 1) 원본 (음성 포함)
    script.add_track(cc.TrackType.video, "원본영상")
    main = cc.VideoMaterial(os.path.abspath(source))
    script.add_segment(cc.VideoSegment(main, cc.Timerange(0, main.duration)), "원본영상")
    log(f"원본 영상 추가: {os.path.basename(source)} ({width}x{height}, {fps}fps)")

    # 2) 한국 영상 (음소거, 꽉 채우기, 짧으면 반복)
    script.add_track(cc.TrackType.video, "한국영상", relative_index=1)
    materials: dict[str, object] = {}
    for rep in plan:
        mat = materials.get(rep.clip)
        if mat is None:
            mat = materials[rep.clip] = cc.VideoMaterial(os.path.abspath(rep.clip))
        scale = cover_scale(mat.width, mat.height, width, height)
        settings = cc.ClipSettings(scale_x=scale, scale_y=scale)
        seg_start, seg_end = _us(rep.segment.start), _us(rep.segment.end)
        cursor = seg_start
        src = _us(rep.clip_start) if mat.material_type == "video" else 0
        while cursor < seg_end:
            if mat.material_type == "video":
                if src >= mat.duration - 40_000:  # 남은 길이가 너무 짧으면 처음부터
                    src = 0
                take = min(seg_end - cursor, mat.duration - src)
            else:
                take = seg_end - cursor
            script.add_segment(cc.VideoSegment(
                mat, cc.Timerange(cursor, take), source_timerange=cc.Timerange(src, take),
                volume=0.0, clip_settings=settings), "한국영상")
            cursor += take
            src = 0
        log(f"  {rep.segment.start:7.2f}s ~ {rep.segment.end:7.2f}s  →  {os.path.basename(rep.clip)}")
    if progress:
        progress(0.3)

    # 3) 원본 자막 지키기
    if subs.mode in ("key", "band"):
        script.add_track(cc.TrackType.video, "자막보존", relative_index=2)
        for n, rep in enumerate(plan):
            s, d = rep.segment.start, rep.segment.duration
            piece, (x, y, w, h) = render_subtitle_piece(source, s, d, subs, width, height, media_dir, n)
            mat = cc.VideoMaterial(piece)
            take = min(_us(d), mat.duration)
            # 자막 조각은 원래 크기 그대로(맞춤 배율을 되돌림), 원래 자리에 놓는다
            fit = min(width / mat.width, height / mat.height)
            settings = cc.ClipSettings(
                scale_x=1 / fit, scale_y=1 / fit,
                transform_x=((x + w / 2) - width / 2) / (width / 2),
                transform_y=region_transform_y(y, h, height))
            script.add_segment(cc.VideoSegment(mat, cc.Timerange(_us(s), take),
                                               source_timerange=cc.Timerange(0, take),
                                               volume=0.0, clip_settings=settings), "자막보존")
            if progress:
                progress(0.3 + 0.6 * (n + 1) / len(plan))
        log(f"원본 자막 조각 {len(plan)}개 추가 ({'글자만' if subs.mode == 'key' else '자막 띠'})")
    elif subs.mode == "text":
        script.add_track(cc.TrackType.text, "자막")
        style = cc.TextStyle(size=6.0, align=1, auto_wrapping=True)
        border = cc.TextBorder(width=40.0)
        count = 0
        for rep in plan:
            for c in captions or []:
                a, b = max(c.start, rep.segment.start), min(c.end, rep.segment.end)
                if b - a < 0.05:
                    continue
                try:
                    script.add_segment(cc.TextSegment(
                        c.text, cc.Timerange(_us(a), _us(b) - _us(a)), style=style, border=border,
                        clip_settings=cc.ClipSettings(transform_y=region_transform_y(
                            *subtitle_region_px(subs.region, width, height)[1::2], height))), "자막")
                    count += 1
                except Exception as exc:  # 자막끼리 시간이 겹치면 건너뛴다
                    if type(exc).__name__ != "SegmentOverlap":
                        raise
        log(f"교체 구간에 텍스트 자막 {count}개 추가")

    script.save()
    if progress:
        progress(1.0)
    log(f"CapCut 초안 저장: {draft_path}")
    return CapCutResult(draft_path, draft_name, len(plan))


def open_folder(path: str) -> None:  # pragma: no cover - OS 마다 다름
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


def _local_join(base: str, *parts: str) -> str:
    """사용자 PC 의 경로 규칙(Windows 는 \\, Mac 은 /)에 맞춰 경로를 잇는다."""
    windows = "\\" in base or re.match(r"^[A-Za-z]:", base) is not None
    sep = "\\" if windows else "/"
    return sep.join([base.rstrip("\\/"), *parts])


def pack_draft_zip(draft_path: str, zip_path: str, local_drafts_folder: str) -> str:
    """서버에서 만든 초안을 사용자 PC 로 옮길 수 있는 ZIP 으로 묶는다.

    초안이 쓰는 모든 영상(원본·한국 영상·자막 조각)을 ZIP 안의 <초안이름>/kbroll_media/ 에 넣고,
    초안 속 파일 경로를 '사용자 PC 의 CapCut 초안 폴더/<초안이름>/kbroll_media/파일' 로 바꾼다.
    사용자는 ZIP 을 CapCut 초안 폴더에 풀기만 하면 된다.
    """
    import json
    import zipfile

    if not local_drafts_folder.strip():
        raise ValueError("PC 의 CapCut 초안 폴더 경로가 필요합니다 (CapCut > 설정 > 초안 위치).")
    draft_name = os.path.basename(os.path.normpath(draft_path))
    content_file = os.path.join(draft_path, "draft_content.json")
    with open(content_file, encoding="utf-8") as f:
        content = json.load(f)

    media: dict[str, str] = {}  # 서버 경로 → ZIP 안 파일 이름
    used_names: set[str] = set()
    for kind in ("videos", "audios"):
        for mat in content.get("materials", {}).get(kind, []):
            path = mat.get("path")
            if not path or not os.path.isfile(path):
                continue
            if path not in media:
                base, ext = os.path.splitext(os.path.basename(path))
                name, n = base + ext, 1
                while name in used_names:
                    n += 1
                    name = f"{base}_{n}{ext}"
                used_names.add(name)
                media[path] = name
            mat["path"] = _local_join(local_drafts_folder, draft_name, "kbroll_media", media[path])

    os.makedirs(os.path.dirname(zip_path) or ".", exist_ok=True)
    tmp = zip_path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{draft_name}/draft_content.json", json.dumps(content, ensure_ascii=False))
        for name in os.listdir(draft_path):
            full = os.path.join(draft_path, name)
            if name != "draft_content.json" and os.path.isfile(full):
                zf.write(full, f"{draft_name}/{name}")
        for path, name in media.items():  # 영상은 이미 압축되어 있으므로 그대로 저장
            zf.write(path, f"{draft_name}/kbroll_media/{name}", compress_type=zipfile.ZIP_STORED)
    os.replace(tmp, zip_path)
    return zip_path
