"""내 PC 에서 실행하는 웹 화면.

    python -m kbroll web          # http://127.0.0.1:8765 이 브라우저로 열림

영상 올리기 → 장면 고르기 → 자막 설정/미리보기 → 영상 만들기/다운로드 를 브라우저에서 한다.
외부에서 접속할 수 없도록 기본적으로 127.0.0.1 에만 연결한다.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import ffmpeg_util
from .ffmpeg_util import IMAGE_EXTS, VIDEO_EXTS
from .replace import SUBTITLE_MODES, SubtitleOptions, preview_frame, replace_segments
from .scan import analyze
from .segments import Segment, normalize

CHUNK = 1024 * 1024


def safe_name(name: str) -> str:
    """업로드 파일 이름에서 경로와 위험한 문자를 제거한다."""
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.lstrip(".") or "file"
    return name[:180]


class Project:
    """작업 폴더 하나 = 작업 하나. 상태는 state.json 에 저장된다."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        for sub in ("source", "clips", "thumbs", "preview", "output"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)
        self.lock = threading.Lock()
        self.state = self._load()
        self.job: dict = {"kind": None, "status": "idle", "progress": 0.0, "log": [], "error": None}

    # ---- 저장 ----
    def _state_path(self) -> str:
        return os.path.join(self.root, "state.json")

    def _load(self) -> dict:
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        state.setdefault("source", None)
        state.setdefault("scenes", [])
        state.setdefault("saved", {})
        if state["source"] and not os.path.exists(self.path("source", state["source"])):
            state["source"], state["scenes"] = None, []
        return state

    def save(self) -> None:
        tmp = self._state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._state_path())

    # ---- 경로 ----
    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def url(self, *parts: str) -> str:
        return "/files/" + "/".join(quote(p) for p in parts)

    def resolve_file(self, rel: str) -> str | None:
        full = os.path.abspath(os.path.join(self.root, unquote(rel)))
        if not full.startswith(self.root + os.sep) or not os.path.isfile(full):
            return None
        return full

    def source_path(self) -> str | None:
        return self.path("source", self.state["source"]) if self.state["source"] else None

    # ---- 조회 ----
    def media_info(self, path: str) -> dict:
        try:
            info = ffmpeg_util.probe(path)
            return {"duration": info.duration, "width": info.width, "height": info.height,
                    "fps": info.fps, "is_image": info.is_image, "has_audio": info.has_audio}
        except ffmpeg_util.FFmpegError as exc:
            return {"error": str(exc)}

    def snapshot(self) -> dict:
        src = self.state["source"]
        source = None
        if src:
            source = {"name": src, "url": self.url("source", src),
                      **self.media_info(self.path("source", src))}
        clips = [
            {"name": os.path.basename(p), "url": self.url("clips", os.path.basename(p)),
             **self.media_info(p)}
            for p in ffmpeg_util.list_media(self.path("clips"))
        ]
        outputs = []
        for name in sorted(os.listdir(self.path("output")),
                           key=lambda n: os.path.getmtime(self.path("output", n)), reverse=True):
            p = self.path("output", name)
            outputs.append({"name": name, "url": self.url("output", name),
                            "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
        return {"source": source, "clips": clips, "scenes": self.state["scenes"],
                "saved": self.state["saved"], "outputs": outputs, "job": self.job_view(),
                "workdir": self.root}

    def job_view(self) -> dict:
        return {k: self.job.get(k) for k in ("kind", "status", "progress", "log", "error", "output")}

    # ---- 작업 실행 ----
    def start_job(self, kind: str, work, **extra) -> bool:
        with self.lock:
            if self.job["status"] == "running":
                return False
            self.job = {"kind": kind, "status": "running", "progress": 0.0, "log": [],
                        "error": None, **extra}

        def log(text: str) -> None:
            self.job["log"] = (self.job["log"] + [text])[-200:]

        def progress(p: float) -> None:
            self.job["progress"] = round(p, 4)

        def target() -> None:
            try:
                work(log, progress)
                self.job["status"] = "done"
                self.job["progress"] = 1.0
            except Exception as exc:  # 화면에 오류를 보여준다
                traceback.print_exc()
                self.job["status"] = "error"
                self.job["error"] = str(exc)

        threading.Thread(target=target, daemon=True).start()
        return True


class Handler(BaseHTTPRequestHandler):
    project: Project  # serve() 에서 지정
    server_version = "kbroll"
    protocol_version = "HTTP/1.1"  # 영상 탐색(Range 요청)을 위해 연결 재사용

    def log_message(self, fmt: str, *args) -> None:  # 요청마다 찍히는 로그는 생략
        pass

    # ---- 응답 도우미 ----
    def send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int = 400) -> None:
        # 요청 본문을 다 읽지 않았을 수 있으므로 연결을 닫는다
        self.close_connection = True
        self.send_json({"error": message}, status)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _same_origin(self) -> bool:
        """다른 사이트에서 이 서버로 요청을 보내는 것(CSRF)을 막는다."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc == host

    # ---- 라우팅 ----
    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            page = resources.files("kbroll").joinpath("webui.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
        elif route == "/api/state":
            self.send_json(self.project.snapshot())
        elif route == "/api/job":
            self.send_json(self.project.job_view())
        elif route.startswith("/files/"):
            self.send_file(route[len("/files/"):])
        else:
            self.send_error_json("없는 주소입니다", 404)

    def do_PUT(self) -> None:
        if not self._same_origin():
            return self.send_error_json("허용되지 않은 요청", 403)
        route = urlparse(self.path).path
        if route == "/api/upload":
            return self.upload()
        self.send_error_json("없는 주소입니다", 404)

    def do_POST(self) -> None:
        if not self._same_origin():
            return self.send_error_json("허용되지 않은 요청", 403)
        route = urlparse(self.path).path
        try:
            body = self.read_json()
            handler = {
                "/api/scan": self.api_scan,
                "/api/preview": self.api_preview,
                "/api/render": self.api_render,
                "/api/save": self.api_save,
                "/api/delete": self.api_delete,
            }.get(route)
            if not handler:
                return self.send_error_json("없는 주소입니다", 404)
            handler(body)
        except (ValueError, KeyError, TypeError) as exc:
            self.send_error_json(str(exc))
        except ffmpeg_util.FFmpegError as exc:
            self.send_error_json(str(exc), 500)

    # ---- 파일 전송 (영상 탐색을 위해 Range 지원) ----
    def send_file(self, rel: str) -> None:
        full = self.project.resolve_file(rel)
        if not full:
            return self.send_error_json("파일이 없습니다", 404)
        size = os.path.getsize(full)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        status = HTTPStatus.OK
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if self.query().get("download"):
            name = os.path.basename(full)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
        self.end_headers()
        with open(full, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 브라우저가 탐색하면서 연결을 끊는 것은 정상

    # ---- API ----
    def upload(self) -> None:
        q = self.query()
        kind = q.get("kind")
        if kind not in ("source", "clip"):
            return self.send_error_json("kind 는 source 또는 clip 이어야 합니다")
        name = safe_name(q.get("name", ""))
        ext = os.path.splitext(name)[1].lower()
        allowed = VIDEO_EXTS if kind == "source" else VIDEO_EXTS | IMAGE_EXTS
        if ext not in allowed:
            return self.send_error_json(f"지원하지 않는 파일 형식입니다: {ext or name}")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self.send_error_json("빈 파일입니다")
        project = self.project
        folder = "source" if kind == "source" else "clips"
        dest = project.path(folder, name)
        tmp = dest + ".uploading"
        with open(tmp, "wb") as f:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(CHUNK, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining:
            os.unlink(tmp)
            return self.send_error_json("업로드가 중간에 끊겼습니다")
        if kind == "source":
            if project.job["status"] == "running":
                os.unlink(tmp)
                return self.send_error_json("작업이 진행 중입니다. 끝난 뒤 다시 올려주세요.", 409)
            for old in os.listdir(project.path("source")):
                if old != os.path.basename(tmp):
                    os.unlink(project.path("source", old))
            shutil.rmtree(project.path("thumbs"), ignore_errors=True)
            os.makedirs(project.path("thumbs"))
            project.state.update(source=name, scenes=[], saved={})
        os.replace(tmp, dest)
        ffmpeg_util.probe.cache_clear()
        info = project.media_info(dest)
        if "error" in info:
            os.unlink(dest)
            if kind == "source":
                project.state["source"] = None
            project.save()
            return self.send_error_json(f"영상을 읽을 수 없습니다: {info['error']}")
        project.save()
        self.send_json({"ok": True, "name": name, **info})

    def api_delete(self, body: dict) -> None:
        name = safe_name(body.get("name", ""))
        kind = body.get("kind")
        folder = {"clip": "clips", "output": "output"}.get(kind)
        if not folder:
            return self.send_error_json("삭제할 수 없는 종류입니다")
        path = self.project.path(folder, name)
        if os.path.isfile(path):
            os.unlink(path)
        self.send_json({"ok": True})

    def api_save(self, body: dict) -> None:
        self.project.state["saved"] = body
        self.project.save()
        self.send_json({"ok": True})

    def api_scan(self, body: dict) -> None:
        project = self.project
        source = project.source_path()
        if not source:
            return self.send_error_json("먼저 원본 영상을 올려주세요")
        threshold = float(body.get("threshold", 0.3))
        min_length = float(body.get("min_length", 0.6))
        if not 0 < threshold < 1:
            raise ValueError("민감도는 0~1 사이여야 합니다")

        def work(log, progress) -> None:
            project.state["scenes"] = []
            project.save()
            thumbs = project.path("thumbs")
            shutil.rmtree(thumbs, ignore_errors=True)
            scenes = analyze(source, thumbs, threshold=threshold, min_length=min_length,
                             progress=progress, log=log)
            stamp = int(time.time())
            project.state["scenes"] = [
                {"s": round(sc.start, 3), "e": round(sc.end, 3),
                 "thumb": project.url("thumbs", os.path.basename(sc.thumb)) + f"?v={stamp}"
                 if sc.thumb else ""}
                for sc in scenes
            ]
            project.save()

        if not project.start_job("scan", work):
            return self.send_error_json("다른 작업이 진행 중입니다", 409)
        self.send_json({"ok": True})

    def _subs(self, body: dict) -> SubtitleOptions:
        s = body.get("subs") or {}
        mode = s.get("mode", "key")
        if mode not in SUBTITLE_MODES:
            raise ValueError("자막 방식이 올바르지 않습니다")
        region = tuple(float(v) for v in s.get("region", (0, 0.72, 1, 0.28)))
        if len(region) != 4:
            raise ValueError("자막 영역이 올바르지 않습니다")
        x, y, w, h = region
        w, h = min(w, 1 - x), min(h, 1 - y)
        if w <= 0 or h <= 0:
            raise ValueError("자막 영역이 올바르지 않습니다")
        return SubtitleOptions(mode=mode, region=(x, y, w, h),
                               threshold=int(s.get("threshold", 200)),
                               outline=int(s.get("outline", 4)), dark=int(s.get("dark", 80)))

    def _clip_path(self, name: str | None) -> str:
        clips = ffmpeg_util.list_media(self.project.path("clips"))
        if name:
            path = self.project.path("clips", safe_name(name))
            if path in clips:
                return path
            raise ValueError(f"한국 영상이 없습니다: {name}")
        if not clips:
            raise ValueError("먼저 한국 영상을 올려주세요")
        return clips[0]

    def api_preview(self, body: dict) -> None:
        project = self.project
        source = project.source_path()
        if not source:
            return self.send_error_json("먼저 원본 영상을 올려주세요")
        at = float(body.get("at", 0))
        clip = self._clip_path(body.get("clip"))
        out = project.path("preview", "preview.jpg")
        preview_frame(source, at, clip, out, self._subs(body))
        self.send_json({"url": project.url("preview", "preview.jpg") + f"?v={time.time():.3f}"})

    def api_render(self, body: dict) -> None:
        project = self.project
        source = project.source_path()
        if not source:
            return self.send_error_json("먼저 원본 영상을 올려주세요")
        segments = []
        for item in body.get("segments", []):
            start, end = float(item["start"]), float(item["end"])
            if end - start < 0.04:
                continue
            clip = item.get("clip") or None
            if clip:
                clip = self._clip_path(clip)
            segments.append(Segment(start, end, clip))
        segments = normalize(segments)
        if not segments:
            return self.send_error_json("교체할 구간을 하나 이상 골라주세요")
        if not any(s.clip for s in segments) or not all(s.clip for s in segments):
            if not ffmpeg_util.list_media(project.path("clips")):
                return self.send_error_json("먼저 한국 영상을 올려주세요")
        subs = self._subs(body)
        quality = {"high": (18, "medium"), "normal": (21, "fast"), "draft": (26, "ultrafast")}
        crf, preset = quality.get(body.get("quality", "high"), quality["high"])
        stem, ext = os.path.splitext(project.state["source"])
        suffix = "_미리보기" if body.get("quality") == "draft" else ""
        out_name = f"{stem}_한국영상교체{suffix}{ext if ext.lower() in ('.mp4', '.mov', '.mkv') else '.mp4'}"
        output = project.path("output", out_name)
        shuffle = bool(body.get("shuffle"))

        def work(log, progress) -> None:
            tmp_out = project.path("preview", "rendering" + os.path.splitext(output)[1])
            try:
                replace_segments(source, segments, tmp_out,
                                 clips_dir=project.path("clips"), subs=subs, shuffle=shuffle,
                                 crf=crf, preset=preset, progress=progress,
                                 log=lambda t: None if t.startswith("완료") else
                                 log(t.replace(project.root + os.sep, "")))
                os.replace(tmp_out, output)
                log(f"완료: {out_name}")
            finally:
                if os.path.exists(tmp_out):
                    os.unlink(tmp_out)

        if not project.start_job("render", work, output=out_name):
            return self.send_error_json("다른 작업이 진행 중입니다", 409)
        self.send_json({"ok": True, "output": out_name})


def serve(workdir: str, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    project = Project(workdir)
    handler = type("BoundHandler", (Handler,), {"project": project})
    httpd = None
    for p in range(port, port + 20):  # 포트가 사용 중이면 다음 번호로
        try:
            httpd = ThreadingHTTPServer((host, p), handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise OSError(f"{port}~{port + 19} 포트를 모두 사용할 수 없습니다")
    shown_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown_host}:{httpd.server_address[1]}/"
    print(f"한국 영상 교체기 실행 중: {url}")
    print(f"작업 폴더: {project.root}")
    print("끝내려면 이 창에서 Ctrl+C 를 누르세요.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        httpd.server_close()
