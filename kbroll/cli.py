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


def _server_password(reset: bool) -> tuple[str | None, str | None]:
    """서버 모드 비밀번호: 환경변수 > 저장된 해시 > 처음 한 번 물어보고 해시로 저장."""
    from . import userconfig

    env = os.environ.get("KBROLL_PASSWORD")
    if env:
        return env, None
    saved = userconfig.load().get("password_hash")
    if saved and not reset:
        return None, saved
    if not sys.stdin or not sys.stdin.isatty():
        raise ValueError("서버 모드 비밀번호가 없습니다. 환경변수 KBROLL_PASSWORD 에 넣어주세요.")
    import getpass

    print("밖에서 접속할 때 쓸 비밀번호를 정해 주세요 (8자 이상, 한 번만 물어봅니다).")
    while True:
        first = getpass.getpass("비밀번호: ")
        if len(first) < 8:
            print("8자 이상으로 정해 주세요.")
            continue
        if getpass.getpass("한 번 더: ") != first:
            print("두 번 입력한 비밀번호가 다릅니다.")
            continue
        break
    hashed = userconfig.hash_password(first)
    userconfig.save(password_hash=hashed)
    print("비밀번호를 저장했습니다. (바꾸려면 --reset-password)")
    return None, hashed


def cmd_web(args: argparse.Namespace) -> int:
    from . import userconfig
    from .web import serve

    tunnel = getattr(args, "tunnel", False)
    public = tunnel or getattr(args, "public", False) or os.environ.get("KBROLL_PUBLIC") == "1"
    workdir = (getattr(args, "workdir", None) or os.environ.get("KBROLL_WORKDIR")
               or userconfig.default_workdir())
    # 터널은 이 컴퓨터 안에서 연결하므로 127.0.0.1 만 열어도 된다 (같은 와이파이에 노출되지 않음)
    host = getattr(args, "host", None) or ("0.0.0.0" if public and not tunnel else "127.0.0.1")
    port = getattr(args, "port", None) or int(os.environ.get("PORT", 8765))
    password, password_hash = (_server_password(getattr(args, "reset_password", False))
                               if public else (None, None))
    serve(workdir, host=host, port=port, open_browser=getattr(args, "open", True) and (tunnel or not public),
          public=public, password=password, password_hash=password_hash, tunnel=tunnel)
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
    w.add_argument("--workdir", help="작업 폴더 (기본: 화면에서 고른 폴더, 없으면 홈폴더/kbroll_작업)")
    w.add_argument("--port", type=int, help="포트 번호 (기본: 환경변수 PORT 또는 8765)")
    w.add_argument("--host", help="접속 허용 주소 (기본 127.0.0.1, --public 이면 0.0.0.0)")
    w.add_argument("--public", action="store_true",
                   help="인터넷 서버로 운영: 비밀번호(환경변수 KBROLL_PASSWORD) 로그인, CapCut 초안은 ZIP 으로")
    w.add_argument("--tunnel", action="store_true",
                   help="내 컴퓨터를 서버로: Cloudflare 임시 인터넷 주소를 만들고 비밀번호 로그인을 켭니다")
    w.add_argument("--reset-password", action="store_true", help="서버 모드 비밀번호 다시 정하기")
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
