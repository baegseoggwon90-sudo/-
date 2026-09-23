"""나레이션 내용에 맞는 한국 영상을 자동으로 찾는다.

흐름
  1. 나레이션 글자 얻기   : 자막 파일(SRT/VTT) → 없으면 음성 인식(faster-whisper, 선택 설치)
                           → 둘 다 없으면 Claude 가 화면의 자막을 직접 읽는다
  2. 장면 판단            : 장면 사진 + 그 구간 나레이션을 Claude 에게 보여주고
                           "외국 영상인지 / 나레이션과 안 맞는지 / 어떤 화면이 필요한지" 를 받는다
  3. 후보 찾기            : 내 자료(한국 영상 폴더)의 설명 + 픽사베이 무료 영상 검색 결과
  4. 가장 잘 맞는 것 선택 : Claude 가 후보 사진들을 보고 고른다 → 픽사베이 영상이면 내려받는다
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import ffmpeg_util
from .ai import Claude, image_block, text_block
from .scan import make_thumb
from .segments import format_time

Log = Callable[[str], None]
Progress = Callable[[float], None]


# --------------------------------------------------------------------------
# 1. 나레이션 글자
# --------------------------------------------------------------------------
@dataclass
class Caption:
    start: float
    end: float
    text: str


_TS = r"(\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}"


def _ts(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def parse_subtitles(text: str) -> list[Caption]:
    """SRT / WebVTT 자막을 읽는다."""
    captions: list[Caption] = []
    pattern = re.compile(rf"({_TS})\s*-->\s*({_TS})[^\n]*\n(.*?)(?=\n\s*\n|\Z)", re.S)
    for m in pattern.finditer(text.replace("\r\n", "\n").replace("﻿", "")):
        body = re.sub(r"<[^>]+>", "", m.group(5)).strip()
        body = re.sub(r"\s*\n\s*", " ", body)
        if body:
            captions.append(Caption(_ts(m.group(1)), _ts(m.group(3)), body))
    return captions


def load_subtitles(path: str) -> list[Caption]:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return parse_subtitles(f.read())


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def transcribe(source: str, log: Log = print, model_size: str = "small") -> list[Caption]:
    """영상의 음성을 글자로 바꾼다 (faster-whisper 필요)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "음성 인식을 쓰려면 'pip install faster-whisper' 를 실행하거나 자막 파일(SRT)을 올려주세요."
        ) from exc
    log(f"음성 인식 모델({model_size}) 준비 중... (처음 한 번은 내려받느라 오래 걸립니다)")
    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _info = model.transcribe(source, language="ko", vad_filter=True)
    captions = [Caption(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]
    log(f"음성 인식 완료: {len(captions)}문장")
    return captions


def narration_between(captions: list[Caption], start: float, end: float) -> str:
    return " ".join(c.text for c in captions if c.end > start and c.start < end)


def transcript_text(captions: list[Caption]) -> str:
    return "\n".join(f"[{format_time(c.start)[:-4]}] {c.text}" for c in captions)


# --------------------------------------------------------------------------
# 공통: 영상에서 사진 뽑기
# --------------------------------------------------------------------------
def frame_jpeg(path: str, at: float, width: int = 512) -> bytes | None:
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "f.jpg")
        if make_thumb(path, at, out, width=width):
            with open(out, "rb") as f:
                return f.read()
    return None


def clip_frames(path: str, count: int = 3, width: int = 512) -> list[bytes]:
    info = ffmpeg_util.probe(path)
    if info.is_image or not info.duration:
        frame = frame_jpeg(path, 0, width)
        return [frame] if frame else []
    times = [info.duration * (i + 1) / (count + 1) for i in range(count)]
    return [f for f in (frame_jpeg(path, t, width) for t in times) if f]


# --------------------------------------------------------------------------
# 2. 내 자료 설명 만들기 (한 번 만들면 저장해 두고 다시 씀)
# --------------------------------------------------------------------------
CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "tags_en": {"type": "array", "items": {"type": "string"}},
        "in_korea": {"type": "boolean"},
    },
    "required": ["description", "tags_en", "in_korea"],
    "additionalProperties": False,
}

CLIP_PROMPT = (
    "영상 편집용 자료(B-roll) 목록을 만드는 중입니다. 사진은 한 영상에서 뽑은 장면들입니다.\n"
    "- description: 화면에 무엇이 보이는지 한국어 한두 문장 (장소, 사물, 행동, 분위기, 계절/시간대)\n"
    "- tags_en: 검색용 영어 키워드 5~12개\n"
    "- in_korea: 간판·도로표지·번호판·랜드마크·건축 등 배경 단서로 볼 때 한국에서 찍은 화면이면 true. "
    "사람의 외모로 판단하지 마세요. 단서가 없으면 true 로 둡니다."
)


def _file_key(path: str) -> str:
    st = os.stat(path)
    return f"{st.st_size}:{int(st.st_mtime)}"


def index_library(ai: Claude, clips_dir: str, cache_path: str, log: Log = print,
                  progress: Progress | None = None) -> dict[str, dict]:
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache: dict[str, dict] = json.load(f)
    except (OSError, ValueError):
        cache = {}
    # 픽사베이에서 받은 영상은 이미 태그가 있으므로 설명을 따로 만들지 않는다
    files = [p for p in ffmpeg_util.list_media(clips_dir)
             if not os.path.basename(p).startswith("pixabay_")]
    index: dict[str, dict] = {}
    todo = [p for p in files if cache.get(os.path.basename(p), {}).get("key") != _file_key(p)]
    if todo:
        log(f"내 자료 {len(todo)}개 내용 파악 중...")
    for n, path in enumerate(files):
        name = os.path.basename(path)
        entry = cache.get(name)
        if path in todo:
            frames = clip_frames(path)
            if not frames:
                continue
            result = ai.ask_json(
                [text_block(CLIP_PROMPT)],
                [*(image_block(f) for f in frames), text_block(f"파일 이름: {name}")],
                CLIP_SCHEMA, max_tokens=4000,
            )
            entry = {**(entry or {}), **result, "key": _file_key(path)}
            cache[name] = entry
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
        if entry:
            index[name] = entry
        if progress:
            progress((n + 1) / max(1, len(files)))
    return index


# --------------------------------------------------------------------------
# 3. 픽사베이
# --------------------------------------------------------------------------
class PixabayError(RuntimeError):
    pass


def _http_get(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "kbroll/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def _http_save(url: str, path: str, timeout: float = 60) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "kbroll/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as res, open(path, "wb") as f:
        while chunk := res.read(1024 * 1024):
            f.write(chunk)


class Pixabay:
    """https://pixabay.com/api/docs/#api_search_videos

    약관에 따라 검색 결과는 24시간 저장해 두고 재사용하며, 영상은 내려받아서 쓴다(직접 링크 금지).
    """

    API = "https://pixabay.com/api/videos/"
    CACHE_SECONDS = 24 * 3600

    def __init__(self, key: str, cache_dir: str, http_get: Callable[[str], bytes] = _http_get,
                 http_save: Callable[[str, str], None] = _http_save):
        if not key:
            raise PixabayError("픽사베이 API 키가 없습니다. 설정에서 입력하세요 (pixabay.com/api/docs 에서 무료 발급).")
        self.key = key
        self.cache_dir = cache_dir
        self.http_get = http_get
        self.http_save = http_save
        os.makedirs(cache_dir, exist_ok=True)

    def search(self, query: str, per_page: int = 8) -> list[dict]:
        query = " ".join(query.split())[:100]
        cache_file = os.path.join(
            self.cache_dir, hashlib.sha1(f"{query}|{per_page}".encode()).hexdigest() + ".json")
        if os.path.exists(cache_file) and time.time() - os.path.getmtime(cache_file) < self.CACHE_SECONDS:
            with open(cache_file, encoding="utf-8") as f:
                return json.load(f)
        params = urllib.parse.urlencode({
            "key": self.key, "q": query, "per_page": max(3, min(per_page, 200)),
            "safesearch": "true", "video_type": "film",
        })
        try:
            data = json.loads(self.http_get(f"{self.API}?{params}"))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403):
                raise PixabayError("픽사베이 API 키가 올바르지 않습니다.") from exc
            if exc.code == 429:
                raise PixabayError("픽사베이 검색 한도(1분 100회)에 걸렸습니다. 잠시 뒤 다시 시도하세요.") from exc
            raise PixabayError(f"픽사베이 오류 ({exc.code})") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PixabayError(f"픽사베이에 연결할 수 없습니다: {exc}") from exc
        hits = [self._simplify(h) for h in data.get("hits", [])]
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(hits, f, ensure_ascii=False)
        return hits

    @staticmethod
    def _simplify(hit: dict) -> dict:
        videos = hit.get("videos") or {}
        files = {k: {"url": v.get("url"), "width": v.get("width", 0), "height": v.get("height", 0)}
                 for k, v in videos.items() if v and v.get("url")}
        thumb = next((videos[k].get("thumbnail") for k in ("small", "medium", "tiny", "large")
                      if videos.get(k, {}).get("thumbnail")), None)
        return {"id": hit.get("id"), "tags": hit.get("tags", ""), "duration": hit.get("duration", 0),
                "page_url": hit.get("pageURL", ""), "user": hit.get("user", ""),
                "thumb": thumb, "files": files}

    def thumbnail(self, hit: dict) -> bytes | None:
        if not hit.get("thumb"):
            return None
        try:
            return self.http_get(hit["thumb"])
        except (urllib.error.URLError, OSError):
            return None

    def download(self, hit: dict, dest_dir: str, want_height: int = 1080) -> str:
        """원본 해상도에 가장 가까운(넘지 않는) 파일을 내려받는다."""
        files = hit["files"]
        if not files:
            raise PixabayError("내려받을 영상 파일이 없습니다")
        ordered = sorted(files.values(), key=lambda f: f["height"] or 0)
        choice = next((f for f in reversed(ordered) if f["height"] and f["height"] <= want_height), ordered[0])
        dest = os.path.join(dest_dir, f"pixabay_{hit['id']}.mp4")
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return dest
        tmp = dest + ".download"
        try:
            self.http_save(choice["url"], tmp)
            os.replace(tmp, dest)
        except (urllib.error.URLError, OSError) as exc:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise PixabayError(f"픽사베이 영상 내려받기 실패: {exc}") from exc
        return dest


# --------------------------------------------------------------------------
# 4. 장면 판단
# --------------------------------------------------------------------------
SCENE_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "narration": {"type": "string"},
                    "foreign": {"type": "boolean"},
                    "mismatch": {"type": "boolean"},
                    "replace": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "wanted": {"type": "string"},
                    "query_en": {"type": "string"},
                },
                "required": ["index", "narration", "foreign", "mismatch", "replace",
                             "reason", "wanted", "query_en"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["topic", "scenes"],
    "additionalProperties": False,
}

SCENE_PROMPT = """당신은 한국어 나레이션 영상(정보·소개 영상)의 화면을 검수하는 편집자입니다.
나레이션은 한국의 장소·음식·문화·사회 등을 소개하는데, 중간중간 외국에서 찍은 자료 화면이나
나레이션 내용과 맞지 않는 화면이 들어가 있습니다. 이런 장면을 찾아 한국 자료 화면으로 바꾸려고 합니다.

각 장면(사진 1장 = 장면의 가운데 프레임)에 대해 판단하세요.
- narration: 이 장면 동안의 나레이션. 아래 대본에서 찾고, 대본이 없으면 화면에 보이는 자막을 읽어서 적으세요.
- foreign: 화면이 한국이 아닌 곳에서 찍힌 것이 분명하면 true.
  간판·표지판의 언어, 번호판, 랜드마크, 건축 양식, 도로/교통 방식 같은 배경 단서로만 판단하고,
  사람의 외모나 인종으로 판단하지 마세요. 단서가 부족하면 false.
- mismatch: 화면 내용이 나레이션이 말하는 대상과 뚜렷하게 다르면 true (예: 나레이션은 김치, 화면은 피자).
- replace: foreign 또는 mismatch 이면 true. 단, 화면이 제목·그래픽·자막만 있는 카드이거나
  진행자가 나오는 장면이면 바꾸지 않습니다(false).
- reason: 판단 이유를 한국어로 짧게.
- wanted: 나레이션에 어울리는 한국 자료 화면을 한국어로 한 문장 (replace 가 false 여도 적기).
- query_en: 그 화면을 무료 영상 사이트에서 찾을 영어 검색어 2~5단어. 한국 장면이면 korea/seoul/busan 등
  지명을 넣으세요 (예: "seoul street night", "korean kimchi making").
- topic: 영상 전체 주제를 한국어 한 문장으로.
"""

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": "string"},
        "score": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["choice", "score", "reason"],
    "additionalProperties": False,
}

PICK_PROMPT = """한국어 나레이션 영상의 한 장면을 바꿀 자료 화면을 고릅니다.
후보는 두 종류입니다.
- 내 자료: 'L:파일이름' 으로 표시 (설명 글)
- 픽사베이 무료 영상: 'P:번호' 로 표시 (사진 + 태그)
나레이션 내용과 가장 잘 맞고, 한국 배경인(또는 배경이 드러나지 않는) 후보를 고르세요.
내 자료가 픽사베이 후보와 비슷하게 잘 맞으면 내 자료를 우선합니다.
워터마크·글자가 크게 들어간 영상, 외국 배경이 분명한 영상은 피하세요.
- choice: 고른 후보 표시(예: 'L:seoul.mp4', 'P:3'). 알맞은 후보가 없으면 'none'.
- score: 0~100, 나레이션과 얼마나 잘 맞는지.
- reason: 한국어로 짧게.
"""


def analyze_scenes(ai: Claude, source: str, scenes: list[dict], captions: list[Caption],
                   log: Log = print, progress: Progress | None = None,
                   batch: int = 8) -> tuple[str, list[dict]]:
    """각 장면에 대해 교체가 필요한지 판단한다. scenes: [{'s':..,'e':..}]"""
    script = transcript_text(captions) if captions else "(대본 없음 — 화면의 자막을 읽으세요)"
    system = [text_block(SCENE_PROMPT), text_block("## 전체 대본\n" + script, cache=True)]
    results: dict[int, dict] = {}
    topic = ""
    for b in range(0, len(scenes), batch):
        chunk = list(enumerate(scenes))[b:b + batch]
        content: list[dict] = []
        for i, sc in chunk:
            frame = frame_jpeg(source, (sc["s"] + sc["e"]) / 2)
            narr = narration_between(captions, sc["s"], sc["e"]) if captions else ""
            content.append(text_block(
                f"장면 {i}: {format_time(sc['s'])[:-4]} ~ {format_time(sc['e'])[:-4]}"
                + (f"\n이 구간 나레이션: {narr}" if narr else "")))
            if frame:
                content.append(image_block(frame))
        content.append(text_block(f"위 장면 {len(chunk)}개를 모두 판단하세요 (index 는 장면 번호)."))
        data = ai.ask_json(system, content, SCENE_SCHEMA)
        topic = topic or data.get("topic", "")
        for item in data.get("scenes", []):
            if isinstance(item.get("index"), int):
                results[item["index"]] = item
        log(f"장면 판단 {min(b + batch, len(scenes))}/{len(scenes)}")
        if progress:
            progress(min(1.0, (b + batch) / len(scenes)))
    out = []
    for i in range(len(scenes)):
        r = results.get(i) or {"narration": "", "foreign": False, "mismatch": False,
                               "replace": False, "reason": "판단 결과 없음", "wanted": "", "query_en": ""}
        out.append({**r, "index": i})
    return topic, out


def choose_clip(ai: Claude, scene: dict, verdict: dict, topic: str, library: dict[str, dict],
                hits: list[dict], thumbs: list[bytes | None], frame: bytes | None) -> dict:
    lib_text = "\n".join(
        f"L:{name} — {e.get('description', '')} (태그: {', '.join(e.get('tags_en', []))})"
        + ("" if e.get("in_korea", True) else " [외국 배경]")
        for name, e in library.items()) or "(내 자료 없음)"
    system = [text_block(PICK_PROMPT), text_block("## 내 자료 목록\n" + lib_text, cache=True)]
    content: list[dict] = [text_block(
        f"영상 주제: {topic}\n장면 길이: {scene['e'] - scene['s']:.1f}초\n"
        f"나레이션: {verdict.get('narration') or '(없음)'}\n필요한 화면: {verdict.get('wanted', '')}\n"
        f"지금 화면이 문제인 이유: {verdict.get('reason', '')}")]
    if frame:
        content += [text_block("지금 화면:"), image_block(frame)]
    for n, hit in enumerate(hits, 1):
        content.append(text_block(f"P:{n} — 태그: {hit['tags']} / 길이 {hit['duration']}초"))
        if thumbs[n - 1]:
            content.append(image_block(thumbs[n - 1]))
    content.append(text_block("가장 알맞은 후보를 하나 고르세요."))
    return ai.ask_json(system, content, PICK_SCHEMA, max_tokens=4000)


# --------------------------------------------------------------------------
# 전체 자동 추천
# --------------------------------------------------------------------------
@dataclass
class AutoOptions:
    use_library: bool = True
    use_pixabay: bool = True
    only_flagged: bool = True     # AI 가 교체가 필요하다고 본 장면만
    min_score: int = 50           # 이 점수보다 낮으면 교체하지 않음
    pixabay_per_query: int = 6


def recommend(
    ai: Claude,
    source: str,
    scenes: list[dict],
    captions: list[Caption],
    clips_dir: str,
    work_dir: str,
    options: AutoOptions,
    pixabay: Pixabay | None = None,
    log: Log = print,
    progress: Progress | None = None,
) -> dict:
    """장면마다 교체 여부와 사용할 영상을 정한다. 결과는 웹 화면에 그대로 보여줄 수 있는 dict."""
    def stage(lo: float, hi: float) -> Progress:
        return lambda p: progress(lo + (hi - lo) * p) if progress else None

    library: dict[str, dict] = {}
    if options.use_library and ffmpeg_util.list_media(clips_dir):
        library = index_library(ai, clips_dir, os.path.join(work_dir, "library_index.json"),
                                log, stage(0.0, 0.2))

    log("장면마다 외국 영상/내용 불일치 여부 판단 중...")
    topic, verdicts = analyze_scenes(ai, source, scenes, captions, log, stage(0.2, 0.55))
    log(f"영상 주제: {topic}")
    targets = [v for v in verdicts if v["replace"] or not options.only_flagged]
    log(f"교체 후보 장면: {len(targets)}개")

    info = ffmpeg_util.probe(source)
    recs: list[dict] = []
    for n, v in enumerate(targets):
        sc = scenes[v["index"]]
        hits: list[dict] = []
        if options.use_pixabay and pixabay and v.get("query_en"):
            try:
                hits = pixabay.search(v["query_en"], options.pixabay_per_query)
                if len(hits) < 3 and "korea" in v["query_en"].lower():
                    extra = pixabay.search(re.sub(r"(?i)\bkorea(n)?\b", "", v["query_en"]),
                                           options.pixabay_per_query)
                    hits += [h for h in extra if h["id"] not in {x["id"] for x in hits}]
            except PixabayError as exc:
                log(f"  픽사베이 검색 실패: {exc}")
            hits = hits[:options.pixabay_per_query]
        if not library and not hits:
            recs.append({**v, "clip": None, "score": 0, "pick_reason": "후보 영상이 없습니다"})
            continue
        thumbs = [pixabay.thumbnail(h) if pixabay else None for h in hits]
        frame = frame_jpeg(source, (sc["s"] + sc["e"]) / 2)
        pick = choose_clip(ai, sc, v, topic, library, hits, thumbs, frame)
        rec = {**v, "clip": None, "score": int(pick.get("score", 0)),
               "pick_reason": pick.get("reason", ""), "source": None, "credit": None}
        choice = (pick.get("choice") or "none").strip()
        if rec["score"] < options.min_score or choice == "none":
            rec["pick_reason"] = rec["pick_reason"] or "알맞은 영상을 찾지 못했습니다"
        elif choice.startswith("L:") and choice[2:] in library:
            rec.update(clip=choice[2:], source="library")
        elif choice.startswith("P:") and choice[2:].isdigit() and 1 <= int(choice[2:]) <= len(hits):
            hit = hits[int(choice[2:]) - 1]
            try:
                path = pixabay.download(hit, clips_dir, want_height=info.height)  # type: ignore[union-attr]
                rec.update(clip=os.path.basename(path), source="pixabay",
                           credit={"user": hit["user"], "url": hit["page_url"], "tags": hit["tags"]})
                log(f"  픽사베이 영상 내려받음: {os.path.basename(path)}")
            except PixabayError as exc:
                rec["pick_reason"] = f"내려받기 실패: {exc}"
        else:
            rec["pick_reason"] = f"AI 가 고른 후보({choice})를 찾을 수 없습니다"
        label = rec["clip"] or "교체 안 함"
        log(f"  {format_time(sc['s'])[:-4]}  {v['wanted']}  →  {label} ({rec['score']}점)")
        recs.append(rec)
        if progress:
            progress(0.55 + 0.45 * (n + 1) / max(1, len(targets)))

    by_index = {r["index"]: r for r in recs}
    return {
        "topic": topic,
        "scenes": [by_index.get(v["index"], {**v, "clip": None, "score": None}) for v in verdicts],
        "created": time.time(),
    }
