"""내 PC 에서 실행하는 웹 화면.

    python -m kbroll web          # http://127.0.0.1:8765 이 브라우저로 열림

영상 올리기 → 장면 고르기 → 자막 설정/미리보기 → 영상 만들기/다운로드 를 브라우저에서 한다.
외부에서 접속할 수 없도록 기본적으로 127.0.0.1 에만 연결한다.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import ffmpeg_util, matcher, userconfig
from .ai import DEFAULT_MODEL, AIError, Claude
from .capcut import (CAPCUT_SUB_MODES, CapCutError, capcut_available, default_draft_folder,
                     export_draft, open_folder, pack_draft_zip)
from .ffmpeg_util import IMAGE_EXTS, VIDEO_EXTS
from .replace import (SUBTITLE_MODES, SubtitleOptions, assign_clips, preview_frame,
                      replace_segments)
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

    def __init__(self, root: str, *, public: bool = False, password: str | None = None,
                 password_hash: str | None = None, tunnel: bool = False) -> None:
        self.root = os.path.abspath(root)
        # public: 인터넷 서버로 운영 (비밀번호 로그인 필요, CapCut 초안은 ZIP 으로 내려받기)
        self.public = public
        # 비밀번호는 해시로만 들고 있는다
        self.password_hash = userconfig.hash_password(password) if password else (password_hash or None)
        self.tunnel = tunnel          # Cloudflare 터널로 열려 있으면 Cf-Connecting-Ip 를 믿는다
        self.public_url: str | None = None
        self.sessions: set[str] = set()
        self.failed_logins: dict[str, list[float]] = {}
        for sub in ("source", "clips", "thumbs", "preview", "output", "capcut_drafts"):
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
        state.setdefault("subtitle", None)
        state.setdefault("recommend", None)
        state.setdefault("credits", {})
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

    # ---- 설정 (API 키) ----
    def settings(self) -> dict:
        try:
            with open(self.path("settings.json"), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data.setdefault("anthropic_key", "")
        data.setdefault("pixabay_key", "")
        data.setdefault("model", "")
        data.setdefault("capcut_folder", "")
        return data

    def save_settings(self, data: dict) -> None:
        path = self.path("settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        try:
            os.chmod(path, 0o600)  # 키가 들어 있으므로 본인만 읽을 수 있게
        except OSError:
            pass

    def capcut_direct_folder(self) -> str | None:
        """이 컴퓨터에 CapCut 초안 폴더가 있으면 그 경로 (있으면 ZIP 없이 바로 저장한다)."""
        folder = (self.settings().get("capcut_folder") or "").strip()
        if folder and os.path.isdir(folder):
            return folder
        return None if folder and self.public else default_draft_folder()

    def subtitle_path(self) -> str | None:
        name = self.state.get("subtitle")
        return self.path("source", name) if name and os.path.exists(self.path("source", name)) else None

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
        credits = self.state.get("credits", {})
        for c in clips:
            if c["name"] in credits:
                c["credit"] = credits[c["name"]]
        settings = self.settings()
        ai = {
            "anthropic": bool(settings["anthropic_key"] or os.environ.get("ANTHROPIC_API_KEY")),
            "pixabay": bool(settings["pixabay_key"] or os.environ.get("PIXABAY_API_KEY")),
            "whisper": matcher.whisper_available(),
            "capcut": capcut_available(),
            "capcut_folder": settings.get("capcut_folder") or ("" if self.public else default_draft_folder() or ""),
            "capcut_direct": bool(self.capcut_direct_folder()),
        }
        return {"source": source, "clips": clips, "scenes": self.state["scenes"],
                "saved": self.state["saved"], "outputs": outputs, "job": self.job_view(),
                "workdir": self.root, "public": self.public, "public_url": self.public_url,
                "login": bool(self.password_hash), "subtitle": self.state.get("subtitle") if self.subtitle_path() else None,
                "recommend": self.state.get("recommend"), "ai": ai}

    def job_view(self) -> dict:
        return {k: self.job.get(k) for k in ("kind", "status", "progress", "log", "error", "output",
                                             "draft_name", "draft_path")}

    def run_scan(self, source: str, threshold: float, min_length: float, log, progress) -> None:
        project = self
        project.state["scenes"] = []
        project.state["recommend"] = None
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


QUALITY = {"high": (18, "medium"), "normal": (21, "fast"), "draft": (26, "ultrafast")}


def make_render_work(project: "Project", segments: list[Segment], subs: SubtitleOptions,
                     quality: str, shuffle: bool):
    """영상 만들기 작업 함수와 결과 파일 이름을 만든다."""
    source = project.source_path()
    crf, preset = QUALITY.get(quality, QUALITY["high"])
    stem, ext = os.path.splitext(project.state["source"])
    suffix = "_미리보기" if quality == "draft" else ""
    out_name = f"{stem}_한국영상교체{suffix}{ext if ext.lower() in ('.mp4', '.mov', '.mkv') else '.mp4'}"
    output = project.path("output", out_name)

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

    return out_name, work


def make_capcut_work(project: "Project", segments: list[Segment], subs: SubtitleOptions,
                     folder: str, draft_name: str, shuffle: bool, local_folder: str | None = None):
    """CapCut 초안 만들기 작업 함수.

    local_folder 가 있으면(서버 모드) 초안을 ZIP 으로 묶어 결과 목록에 올린다.
    """
    source = project.source_path()

    def work(log, progress) -> None:
        clip_files = ffmpeg_util.list_media(project.path("clips"))
        plan = assign_clips(segments, clip_files, clips_dir=project.path("clips"), shuffle=shuffle)
        captions = matcher.load_subtitles(project.subtitle_path()) if project.subtitle_path() else None
        result = export_draft(source, plan, folder, draft_name, subs, captions=captions,
                              log=log, progress=progress)
        project.job["draft_name"] = result.draft_name
        if local_folder:
            zip_name = f"{result.draft_name}_CapCut초안.zip"
            log("초안과 영상들을 ZIP 으로 묶는 중...")
            pack_draft_zip(result.draft_path, project.path("output", zip_name), local_folder)
            shutil.rmtree(result.draft_path, ignore_errors=True)
            project.job["output"] = zip_name
            log(f"완료! '{zip_name}' 을 내려받아 PC 의 CapCut 초안 폴더에 압축을 푸세요.")
        else:
            project.job["draft_path"] = result.draft_path
            log(f"완료! CapCut 을 열면 초안 목록에 '{result.draft_name}' 이(가) 있습니다. "
                "(안 보이면 CapCut 을 껐다 켜세요)")

    return work


def mask_key(key: str | None) -> str:
    return "" if not key else (key[:4] + "…" + key[-4:] if len(key) > 12 else "설정됨")


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
        hosts = {self.headers.get("Host", ""), self.headers.get("X-Forwarded-Host", "")}
        return urlparse(origin).netloc in hosts - {""}

    # ---- 로그인 (서버 모드) ----
    def _cookie_token(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == "kbroll_session":
                return value
        return None

    def _authed(self) -> bool:
        if not self.project.password_hash:
            return True
        token = self._cookie_token()
        return bool(token) and any(hmac.compare_digest(token, t) for t in self.project.sessions)

    def _client_ip(self) -> str:
        if self.project.tunnel and self.headers.get("Cf-Connecting-Ip"):
            return self.headers["Cf-Connecting-Ip"]  # 터널 모드: 모든 요청이 Cloudflare 를 거친다
        # 앞단 프록시가 맨 뒤에 붙인 값이 실제 접속 주소 (앞쪽 값은 사용자가 꾸밀 수 있음)
        forwarded = self.headers.get("X-Forwarded-For", "")
        return forwarded.split(",")[-1].strip() or self.client_address[0]

    def api_login(self, body: dict) -> None:
        project = self.project
        ip = self._client_ip()
        now = time.time()
        recent = [t for t in project.failed_logins.get(ip, []) if now - t < 600]
        if len(recent) >= 10:
            return self.send_error_json("로그인 시도가 너무 많습니다. 10분 뒤 다시 시도하세요.", 429)
        password = str(body.get("password", ""))
        if not project.password_hash or not userconfig.check_password(password, project.password_hash):
            project.failed_logins[ip] = recent + [now]
            time.sleep(1)
            return self.send_error_json("비밀번호가 틀렸습니다", 401)
        project.failed_logins.pop(ip, None)
        token = secrets.token_urlsafe(32)
        project.sessions.add(token)
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        body_bytes = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Set-Cookie", f"kbroll_session={token}; Path=/; HttpOnly; SameSite=Lax; "
                                       f"Max-Age={30 * 24 * 3600}{secure}")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def api_logout(self, body: dict) -> None:
        self.project.sessions.discard(self._cookie_token() or "")
        self.send_response(200)
        self.send_header("Set-Cookie", "kbroll_session=; Path=/; Max-Age=0")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def send_page(self, name: str) -> None:
        page = resources.files("kbroll").joinpath(name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)

    # ---- 라우팅 ----
    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/healthz":  # 서버 상태 확인용 (로그인 불필요)
            return self.send_json({"ok": True})
        if not self._authed():
            if route in ("/", "/index.html"):
                return self.send_page("login.html")
            return self.send_error_json("로그인이 필요합니다", 401)
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
        elif route == "/api/settings":
            self.send_json(self.settings_view())
        elif route == "/api/job":
            self.send_json(self.project.job_view())
        elif route.startswith("/files/"):
            self.send_file(route[len("/files/"):])
        else:
            self.send_error_json("없는 주소입니다", 404)

    def do_PUT(self) -> None:
        if not self._same_origin():
            return self.send_error_json("허용되지 않은 요청", 403)
        if not self._authed():
            return self.send_error_json("로그인이 필요합니다", 401)
        route = urlparse(self.path).path
        if route == "/api/upload":
            return self.upload()
        self.send_error_json("없는 주소입니다", 404)

    def do_POST(self) -> None:
        if not self._same_origin():
            return self.send_error_json("허용되지 않은 요청", 403)
        route = urlparse(self.path).path
        if route in ("/api/login", "/api/logout"):
            try:
                body = self.read_json()
            except ValueError:
                body = {}
            return (self.api_login if route == "/api/login" else self.api_logout)(body)
        if not self._authed():
            return self.send_error_json("로그인이 필요합니다", 401)
        try:
            body = self.read_json()
            handler = {
                "/api/scan": self.api_scan,
                "/api/preview": self.api_preview,
                "/api/render": self.api_render,
                "/api/save": self.api_save,
                "/api/delete": self.api_delete,
                "/api/settings": self.api_settings,
                "/api/auto": self.api_auto,
                "/api/capcut": self.api_capcut,
                "/api/open-draft": self.api_open_draft,
                "/api/workdir": self.api_workdir,
                "/api/pick-folder": self.api_pick_folder,
                "/api/open-output": self.api_open_output,
            }.get(route)
            if not handler:
                return self.send_error_json("없는 주소입니다", 404)
            handler(body)
        except (ValueError, KeyError, TypeError) as exc:
            self.send_error_json(str(exc))
        except (ffmpeg_util.FFmpegError, AIError, CapCutError) as exc:
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
        if kind not in ("source", "clip", "subtitle"):
            return self.send_error_json("kind 는 source, clip, subtitle 중 하나여야 합니다")
        name = safe_name(q.get("name", ""))
        ext = os.path.splitext(name)[1].lower()
        if kind == "subtitle":
            return self.upload_subtitle(name, ext)
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

    def upload_subtitle(self, name: str, ext: str) -> None:
        if ext not in (".srt", ".vtt"):
            return self.send_error_json("자막 파일은 .srt 또는 .vtt 만 올릴 수 있습니다")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 20 * 1024 * 1024:
            return self.send_error_json("자막 파일 크기가 올바르지 않습니다")
        data = self.rfile.read(length)
        captions = matcher.parse_subtitles(data.decode("utf-8-sig", errors="replace"))
        if not captions:
            return self.send_error_json("자막을 읽을 수 없습니다. SRT/VTT 형식인지 확인하세요.")
        project = self.project
        old = project.subtitle_path()
        if old:
            os.unlink(old)
        dest_name = "narration" + ext
        with open(project.path("source", dest_name), "wb") as f:
            f.write(data)
        project.state["subtitle"] = dest_name
        project.save()
        self.send_json({"ok": True, "captions": len(captions)})

    def api_settings(self, body: dict) -> None:
        project = self.project
        data = project.settings()
        for key in ("anthropic_key", "pixabay_key", "model", "capcut_folder"):
            value = body.get(key)
            if value is None:
                continue
            value = str(value).strip()
            if key.endswith("_key") and value and value == mask_key(data[key]):
                continue  # 화면에 가려서 보여준 값을 그대로 다시 보낸 경우 → 기존 키 유지
            data[key] = value
        project.save_settings(data)
        self.send_json(self.settings_view())

    def settings_view(self) -> dict:
        data = self.project.settings()
        return {"anthropic_key": mask_key(data["anthropic_key"]),
                "pixabay_key": mask_key(data["pixabay_key"]),
                "model": data["model"] or DEFAULT_MODEL,
                "capcut_folder": data["capcut_folder"],
                "capcut_detected": default_draft_folder() or "",
                "env_anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "env_pixabay": bool(os.environ.get("PIXABAY_API_KEY"))}

    def api_auto(self, body: dict) -> None:
        project = self.project
        source = project.source_path()
        if not source:
            return self.send_error_json("먼저 원본 영상을 올려주세요")
        settings = project.settings()
        options = matcher.AutoOptions(
            use_library=bool(body.get("use_library", True)),
            use_pixabay=bool(body.get("use_pixabay", True)),
            min_score=int(body.get("min_score", 50)),
        )
        if not options.use_library and not options.use_pixabay:
            raise ValueError("내 자료 또는 픽사베이 중 하나 이상을 선택하세요")
        pixabay_key = settings["pixabay_key"] or os.environ.get("PIXABAY_API_KEY", "")
        if options.use_pixabay and not pixabay_key:
            raise ValueError("픽사베이를 쓰려면 AI 설정에서 픽사베이 API 키를 입력하세요")
        if not options.use_pixabay and not ffmpeg_util.list_media(project.path("clips")):
            raise ValueError("내 자료만 쓰려면 먼저 한국 영상을 올려주세요")
        want_transcribe = bool(body.get("transcribe"))
        render_after = bool(body.get("render_after"))
        to_capcut = body.get("target") == "capcut"
        subs = self._subs(body, capcut=to_capcut)
        capcut_folder = self._capcut_folder(body) if render_after and to_capcut else None
        local_capcut = self._local_capcut_folder() if render_after and to_capcut else None
        if render_after and to_capcut and subs.mode == "text" and not project.subtitle_path():
            raise ValueError("텍스트 자막으로 넣으려면 자막 파일(SRT/VTT)을 먼저 올려주세요")
        quality = body.get("quality", "high")
        shuffle = bool(body.get("shuffle"))
        ai = Claude(settings["anthropic_key"] or None, settings["model"] or None)

        def work(log, progress) -> None:
            def part(lo, hi):
                return lambda p: progress(lo + (hi - lo) * p)

            if not project.state["scenes"]:
                project.run_scan(source, 0.3, 0.6, log, part(0, 0.08))
            scenes = project.state["scenes"]

            captions: list = []
            sub_path = project.subtitle_path()
            if sub_path:
                captions = matcher.load_subtitles(sub_path)
                log(f"자막 파일에서 나레이션 {len(captions)}문장을 읽었습니다.")
            elif want_transcribe:
                captions = matcher.transcribe(source, log)
            else:
                log("나레이션 대본이 없어 화면의 자막을 읽어서 판단합니다.")

            pixabay = None
            if options.use_pixabay:
                pixabay = matcher.Pixabay(pixabay_key, project.path("preview", "pixabay_cache"))
            end = 0.6 if render_after else 1.0
            result = matcher.recommend(ai, source, scenes, captions, project.path("clips"),
                                       project.root, options, pixabay, log, part(0.08, end))
            for rec in result["scenes"]:
                if rec.get("credit") and rec.get("clip"):
                    project.state["credits"][rec["clip"]] = rec["credit"]
            project.state["recommend"] = result
            picked = {str(r["index"]): r["clip"] for r in result["scenes"] if r.get("clip")}
            project.state["saved"] = {**project.state.get("saved", {}), "picked": picked}
            project.save()
            count = len(picked)
            log(f"추천 완료: {count}개 장면을 교체하도록 골랐습니다.")

            if render_after and not count:
                log("교체할 장면이 없어 영상/초안은 만들지 않았습니다.")
            if render_after and count:
                segments = normalize([
                    Segment(scenes[int(i)]["s"], scenes[int(i)]["e"], project.path("clips", clip))
                    for i, clip in picked.items()])
                if to_capcut:
                    name = os.path.splitext(project.state["source"])[0] + "_한국영상"
                    log("추천대로 CapCut 초안을 만듭니다...")
                    make_capcut_work(project, segments, subs, capcut_folder, name, shuffle,
                                     local_capcut)(log, part(0.6, 1.0))
                else:
                    out_name, render = make_render_work(project, segments, subs, quality, shuffle)
                    project.job["output"] = out_name
                    log("추천대로 영상을 만듭니다...")
                    render(log, part(0.6, 1.0))

        if not project.start_job("auto", work):
            return self.send_error_json("다른 작업이 진행 중입니다", 409)
        self.send_json({"ok": True})

    def api_delete(self, body: dict) -> None:
        name = safe_name(body.get("name", ""))
        kind = body.get("kind")
        if kind == "subtitle":
            path = self.project.subtitle_path()
            if path:
                os.unlink(path)
            self.project.state["subtitle"] = None
            self.project.save()
            return self.send_json({"ok": True})
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
            project.run_scan(source, threshold, min_length, log, progress)

        if not project.start_job("scan", work):
            return self.send_error_json("다른 작업이 진행 중입니다", 409)
        self.send_json({"ok": True})

    def _subs(self, body: dict, capcut: bool = False) -> SubtitleOptions:
        s = body.get("subs") or {}
        mode = s.get("mode", "key")
        if mode not in (CAPCUT_SUB_MODES if capcut else SUBTITLE_MODES):
            if mode == "text":
                raise ValueError("'텍스트 자막' 방식은 CapCut 초안을 만들 때만 쓸 수 있습니다")
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
        subs = self._subs(body, capcut=True)
        if subs.mode == "text":  # CapCut 텍스트 자막은 CapCut 에서 보이므로 화면만 미리 본다
            subs.mode = "none"
        preview_frame(source, at, clip, out, subs)
        self.send_json({"url": project.url("preview", "preview.jpg") + f"?v={time.time():.3f}"})

    def _segments(self, body: dict) -> list[Segment]:
        project = self.project
        segments = []
        for item in body.get("segments", []):
            start, end = float(item["start"]), float(item["end"])
            if end - start < 0.04:
                continue
            clip = item.get("clip") or None
            segments.append(Segment(start, end, self._clip_path(clip) if clip else None))
        segments = normalize(segments)
        if not segments:
            raise ValueError("교체할 구간을 하나 이상 골라주세요")
        if not all(s.clip for s in segments) and not ffmpeg_util.list_media(project.path("clips")):
            raise ValueError("먼저 한국 영상을 올려주세요")
        return segments

    def _capcut_folder(self, body: dict) -> str:
        direct = self.project.capcut_direct_folder()
        if direct:
            return direct
        if self.project.public:
            return self.project.path("capcut_drafts")
        folder = (self.project.settings().get("capcut_folder") or "").strip()
        if not folder:
            raise ValueError("CapCut 초안 폴더를 찾지 못했습니다. CapCut > 설정 > 초안 위치 의 경로를 "
                             "AI 설정의 'CapCut 초안 폴더' 에 넣어주세요.")
        if not os.path.isdir(folder):
            raise ValueError(f"CapCut 초안 폴더가 없습니다: {folder}")
        return folder

    def _local_capcut_folder(self) -> str | None:
        """서버 모드일 때 사용자 PC 의 CapCut 초안 폴더 경로 (ZIP 속 경로에 씀)."""
        if not self.project.public or self.project.capcut_direct_folder():
            return None
        local = (self.project.settings().get("capcut_folder") or "").strip()
        if not local:
            raise ValueError("설정에서 '내 PC 의 CapCut 초안 폴더' 경로를 입력하세요 "
                             "(CapCut > 설정 > 초안 위치 에 보이는 경로).")
        return local

    def api_capcut(self, body: dict) -> None:
        project = self.project
        source = project.source_path()
        if not source:
            return self.send_error_json("먼저 원본 영상을 올려주세요")
        segments = self._segments(body)
        subs = self._subs(body, capcut=True)
        if subs.mode == "text" and not project.subtitle_path():
            raise ValueError("텍스트 자막으로 넣으려면 2단계에서 자막 파일(SRT/VTT)을 먼저 올려주세요")
        folder = self._capcut_folder(body)
        name = body.get("draft_name") or os.path.splitext(project.state["source"])[0] + "_한국영상"
        work = make_capcut_work(project, segments, subs, folder, name, bool(body.get("shuffle")),
                                self._local_capcut_folder())
        if not project.start_job("capcut", work):
            return self.send_error_json("다른 작업이 진행 중입니다", 409)
        self.send_json({"ok": True})

    def api_workdir(self, body: dict) -> None:
        """작업 폴더(올린 영상·결과물·설정이 저장되는 곳)를 바꾼다."""
        old = self.project
        raw = str(body.get("path", "")).strip().strip('"')
        if not raw:
            raise ValueError("저장할 폴더 경로를 입력하세요")
        path = os.path.abspath(os.path.expanduser(raw))
        if old.job["status"] == "running":
            return self.send_error_json("작업이 끝난 뒤에 바꿔 주세요", 409)
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".kbroll_write_test")
            with open(probe, "w") as f:
                f.write("ok")
            os.unlink(probe)
        except OSError as exc:
            raise ValueError(f"이 폴더에 저장할 수 없습니다: {path} ({exc.strerror or exc})") from exc
        if path != old.root:
            new = Project(path, public=old.public, password_hash=old.password_hash, tunnel=old.tunnel)
            new.sessions, new.failed_logins, new.public_url = old.sessions, old.failed_logins, old.public_url
            if not os.path.exists(new.path("settings.json")) and os.path.exists(old.path("settings.json")):
                shutil.copy2(old.path("settings.json"), new.path("settings.json"))  # API 키 등은 따라간다
            type(self).project = new
            userconfig.save(workdir=path)
        self.send_json({"ok": True, "workdir": path})

    def api_pick_folder(self, body: dict) -> None:
        """이 컴퓨터에서 '폴더 선택' 창을 띄운다 (내 PC 에서 쓸 때만)."""
        if self.project.public:
            return self.send_error_json("서버 모드에서는 폴더 경로를 직접 입력해 주세요")
        result: dict = {}

        def ask() -> None:
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                result["path"] = filedialog.askdirectory(
                    title=body.get("title") or "폴더 선택", initialdir=body.get("start") or None) or ""
                root.destroy()
            except Exception as exc:  # tkinter 가 없거나 화면이 없는 경우
                result["error"] = str(exc)

        t = threading.Thread(target=ask, daemon=True)
        t.start()
        t.join(600)
        if "error" in result or "path" not in result:
            return self.send_error_json("폴더 선택 창을 열 수 없습니다. 경로를 직접 입력해 주세요.")
        self.send_json({"path": os.path.normpath(result["path"]) if result["path"] else ""})

    def api_open_output(self, body: dict) -> None:
        if self.project.public:
            return self.send_error_json("서버에서는 폴더를 열 수 없습니다")
        open_folder(self.project.path("output"))
        self.send_json({"ok": True})

    def api_open_draft(self, body: dict) -> None:
        if self.project.public:
            return self.send_error_json("서버에서는 폴더를 열 수 없습니다")
        path = self.project.job.get("draft_path")
        if not path or not os.path.isdir(path):
            return self.send_error_json("열 초안 폴더가 없습니다")
        open_folder(path)
        self.send_json({"ok": True})

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
        out_name, work = make_render_work(project, segments, self._subs(body),
                                          body.get("quality", "high"), bool(body.get("shuffle")))

        if not project.start_job("render", work, output=out_name):
            return self.send_error_json("다른 작업이 진행 중입니다", 409)
        self.send_json({"ok": True, "output": out_name})


def serve(workdir: str, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          public: bool = False, password: str | None = None, password_hash: str | None = None,
          tunnel: bool = False) -> None:
    if public and not (password or password_hash):
        raise ValueError("서버 모드(--public)는 비밀번호가 꼭 필요합니다. "
                         "환경변수 KBROLL_PASSWORD 에 비밀번호를 넣어주세요.")
    project = Project(workdir, public=public, password=password, password_hash=password_hash,
                      tunnel=tunnel)
    handler = type("BoundHandler", (Handler,), {"project": project})
    httpd = None
    for p in (range(port, port + 1) if public else range(port, port + 20)):  # 사용 중이면 다음 번호로
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
    if public:
        print("서버 모드: 비밀번호 로그인이 켜져 있습니다.")
    print("끝내려면 이 창에서 Ctrl+C 를 누르세요.")
    tun = None
    if tunnel:
        from .tunnel import Tunnel, TunnelError
        try:
            tun = Tunnel(httpd.server_address[1])
            project.public_url = tun.start()
            line = "=" * 60
            print(f"\n{line}\n  인터넷 접속 주소:  {project.public_url}\n"
                  f"  (휴대폰·다른 컴퓨터에서 이 주소로 접속해 비밀번호로 로그인)\n"
                  f"  주소는 프로그램을 다시 켤 때마다 바뀝니다.\n{line}\n")
        except (TunnelError, OSError) as exc:
            print(f"⚠ 인터넷 주소를 만들지 못했습니다: {exc}\n  이 컴퓨터에서는 {url} 로 계속 쓸 수 있습니다.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def _stop(*_args):  # 종료 신호를 받아도 터널까지 정리하고 끝낸다
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _stop)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        if tun:
            tun.stop()
        httpd.server_close()
