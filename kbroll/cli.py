"""명령줄 실행.

    python -m kbroll scan 원본.mp4 --clips 한국영상폴더
    python -m kbroll replace 원본.mp4 segments.csv --clips 한국영상폴더 -o 결과.mp4
    python -m kbroll web      (기본: 브라우저 화면)
"""

from __future__ import annotations

import argparse
import os
import sys

from .ffmpeg_util import FFmpegError


def _progress_printer():
    last = [-1]

    def show(p: float) -> None:
        pct = int(p * 100)
        if pct != last[0]:
            last[0] = pct
            bar = "#" * (pct // 4)
            sys.stdout.write(f"\r  [{bar:<25}] {pct:3d}%")
            sys.stdout.flush()
            if pct >= 100:
                sys.stdout.write("\n")

    return show


def cmd_scan(args: argparse.Namespace) -> int:
    from .scan import scan

    out = args.output or os.path.join(
        os.path.dirname(os.path.abspath(args.video)), "kbroll_work", "review.html"
    )
    scan(args.video, out, clips_dir=args.clips, threshold=args.threshold,
         min_length=args.min_length, progress=_progress_printer())
    if args.open:
        import webbrowser

        webbrowser.open("file://" + os.path.abspath(out))
    return 0


def cmd_replace(args: argparse.Namespace) -> int:
    from .replace import SubtitleOptions, parse_region, replace_segments
    from .segments import load_segments, parse_segments

    if os.path.exists(args.segments):
        segments = load_segments(args.segments)
    else:  # "1:10-1:18;2:00-2:05" 같은 직접 입력도 허용
        text = args.segments.replace(";", "\n").replace("-", ",")
        segments = parse_segments(text)

    output = args.output
    if not output:
        stem, ext = os.path.splitext(args.video)
        output = f"{stem}_한국영상교체{ext or '.mp4'}"
    subs = SubtitleOptions(
        mode=args.subtitle,
        region=parse_region(args.sub_region),
        threshold=args.sub_threshold,
        outline=args.sub_outline,
        dark=args.sub_dark,
    )
    replace_segments(
        args.video, segments, output,
        clips_dir=args.clips, subs=subs, shuffle=args.shuffle, seed=args.seed,
        crf=args.crf, preset=args.preset, progress=_progress_printer(),
    )
    return 0


def cmd_capcut(args: argparse.Namespace) -> int:
    from .capcut import default_draft_folder, export_draft
    from .matcher import load_subtitles
    from .replace import SubtitleOptions, assign_clips, parse_region
    from .ffmpeg_util import list_media
    from .segments import load_segments, parse_segments

    if os.path.exists(args.segments):
        segments = load_segments(args.segments)
    else:
        segments = parse_segments(args.segments.replace(";", "\n").replace("-", ","))
    folder = args.drafts or default_draft_folder()
    if not folder:
        raise ValueError("CapCut 초안 폴더를 찾지 못했습니다. --drafts 로 지정하세요 "
                         "(CapCut > 설정 > 초안 위치).")
    subs = SubtitleOptions(mode=args.subtitle, region=parse_region(args.sub_region),
                           threshold=args.sub_threshold, outline=args.sub_outline, dark=args.sub_dark)
    plan = assign_clips(segments, list_media(args.clips) if args.clips else [], clips_dir=args.clips,
                        shuffle=args.shuffle, seed=args.seed)
    captions = load_subtitles(args.srt) if args.srt else None
    name = args.name or os.path.splitext(os.path.basename(args.video))[0] + "_한국영상"
    result = export_draft(args.video, plan, folder, name, subs, captions=captions)
    print(f"CapCut 을 열면 초안 '{result.draft_name}' 이(가) 있습니다.")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .web import serve

    workdir = getattr(args, "workdir", None) or os.path.join(os.path.expanduser("~"), "kbroll_작업")
    serve(workdir, host=getattr(args, "host", "127.0.0.1"), port=getattr(args, "port", 8765),
          open_browser=getattr(args, "open", True))
    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    from .gui import main as gui_main

    gui_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kbroll",
        description="나레이션 구간의 외국 영상을 한국 영상으로 교체 (음성·자막은 원본 유지)",
    )
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("scan", help="장면을 나누고 교체할 장면을 고르는 검토 페이지(HTML) 생성")
    s.add_argument("video", help="원본 영상")
    s.add_argument("--clips", help="한국 영상 폴더 (검토 페이지에서 영상을 고를 수 있게 됨)")
    s.add_argument("-o", "--output", help="검토 페이지 경로 (기본: 원본폴더/kbroll_work/review.html)")
    s.add_argument("--threshold", type=float, default=0.3,
                   help="장면 전환 민감도 0~1 (작을수록 더 잘게 나눔, 기본 0.3)")
    s.add_argument("--min-length", type=float, default=0.6, help="최소 장면 길이(초)")
    s.add_argument("--no-open", dest="open", action="store_false", help="브라우저로 열지 않기")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("replace", help="지정 구간의 화면을 한국 영상으로 교체")
    r.add_argument("video", help="원본 영상")
    r.add_argument("segments", help="구간 파일(segments.csv) 또는 '1:10-1:18;2:00-2:05' 형식")
    r.add_argument("--clips", help="한국 영상(또는 사진) 폴더")
    r.add_argument("-o", "--output", help="결과 파일 (기본: 원본이름_한국영상교체.mp4)")
    r.add_argument("--subtitle", choices=["key", "band", "none"], default="key",
                   help="하드 자막 보존 방식: key=글자만(기본), band=자막 띠 전체, none=안 함")
    r.add_argument("--sub-region", default="0,0.72,1,0.28",
                   help="자막 영역 x,y,w,h (화면 비율, 기본: 아래쪽 28%%)")
    r.add_argument("--sub-threshold", type=int, default=200,
                   help="key 모드: 글자로 볼 밝기 0~255 (기본 200)")
    r.add_argument("--sub-outline", type=int, default=4,
                   help="key 모드: 글자 테두리 두께 px (기본 4)")
    r.add_argument("--sub-dark", type=int, default=80,
                   help="key 모드: 글자 테두리로 볼 어두움 0~255, 0=테두리 검사 끔 (기본 80)")
    r.add_argument("--shuffle", action="store_true", help="한국 영상을 무작위 순서로 사용")
    r.add_argument("--seed", type=int, help="무작위 순서 고정용 숫자")
    r.add_argument("--crf", type=int, default=18, help="화질 (낮을수록 고화질, 기본 18)")
    r.add_argument("--preset", default="medium", help="인코딩 속도 (ultrafast~veryslow)")
    r.set_defaults(func=cmd_replace)

    c = sub.add_parser("capcut", help="교체 결과를 CapCut 초안(편집 프로젝트)으로 만들기 (pycapcut)")
    c.add_argument("video", help="원본 영상")
    c.add_argument("segments", help="구간 파일(segments.csv) 또는 '1:10-1:18;2:00-2:05' 형식")
    c.add_argument("--clips", help="한국 영상(또는 사진) 폴더")
    c.add_argument("--drafts", help="CapCut 초안 폴더 (기본: 자동으로 찾음)")
    c.add_argument("--name", help="초안 이름 (기본: 원본이름_한국영상)")
    c.add_argument("--subtitle", choices=["key", "band", "text", "none"], default="key",
                   help="자막 보존: key=글자만(투명 영상), band=자막 띠, text=CapCut 텍스트(--srt 필요), none")
    c.add_argument("--srt", help="나레이션 자막 파일 (text 방식에 사용)")
    c.add_argument("--sub-region", default="0,0.72,1,0.28", help="자막 영역 x,y,w,h (화면 비율)")
    c.add_argument("--sub-threshold", type=int, default=200)
    c.add_argument("--sub-outline", type=int, default=4)
    c.add_argument("--sub-dark", type=int, default=80)
    c.add_argument("--shuffle", action="store_true")
    c.add_argument("--seed", type=int)
    c.set_defaults(func=cmd_capcut)

    w = sub.add_parser("web", help="브라우저에서 쓰는 편집 화면 실행 (기본)")
    w.add_argument("--workdir", help="작업 폴더 (기본: 홈폴더/kbroll_작업)")
    w.add_argument("--port", type=int, default=8765, help="포트 번호 (기본 8765)")
    w.add_argument("--host", default="127.0.0.1",
                   help="접속 허용 주소. 같은 공유기의 다른 기기에서 쓰려면 0.0.0.0")
    w.add_argument("--no-open", dest="open", action="store_false", help="브라우저 자동으로 열지 않기")
    w.set_defaults(func=cmd_web)

    g = sub.add_parser("gui", help="간단한 창 프로그램 실행")
    g.set_defaults(func=cmd_gui)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):  # 명령 없이 실행하면 웹 화면
        args.func = cmd_web
    try:
        return args.func(args)
    except (FFmpegError, ValueError, OSError, RuntimeError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
