"""장면(컷) 단위로 영상을 나누고, 교체할 장면을 고르는 검토 페이지(HTML)를 만든다."""

from __future__ import annotations

import base64
import html
import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Callable

from . import ffmpeg_util
from .segments import format_time

_PTS_RE = re.compile(r"pts_time:\s*([\d.]+)")


@dataclass
class Scene:
    index: int
    start: float
    end: float
    thumb: str = ""  # data URI


def detect_cuts(source: str, threshold: float = 0.3) -> list[float]:
    """장면이 바뀌는 시각(초) 목록."""
    proc = ffmpeg_util.run(
        [
            "-i", source, "-an", "-sn", "-dn",
            "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-",
        ]
    )
    if proc.returncode != 0:
        raise ffmpeg_util.FFmpegError(proc.stderr[-2000:])
    return [float(m.group(1)) for m in _PTS_RE.finditer(proc.stderr)]


def build_scenes(cuts: list[float], duration: float, min_length: float = 0.6) -> list[Scene]:
    points = [0.0] + sorted(c for c in cuts if 0 < c < duration) + [duration]
    scenes: list[Scene] = []
    for a, b in zip(points, points[1:]):
        if scenes and (b - a < min_length or scenes[-1].end - scenes[-1].start < min_length):
            scenes[-1].end = b
            continue
        scenes.append(Scene(len(scenes), a, b))
    return scenes


def make_thumb(source: str, at: float, workdir: str) -> str:
    path = os.path.join(workdir, "thumb.jpg")
    proc = ffmpeg_util.run(
        ["-y", "-ss", f"{at:.3f}", "-i", source, "-frames:v", "1",
         "-vf", "scale=320:-2", "-q:v", "5", path]
    )
    if proc.returncode != 0 or not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    os.unlink(path)
    return "data:image/jpeg;base64," + data


def scan(
    source: str,
    output_html: str,
    *,
    clips_dir: str | None = None,
    threshold: float = 0.3,
    min_length: float = 0.6,
    progress: Callable[[float], None] | None = None,
    log: Callable[[str], None] = print,
) -> list[Scene]:
    info = ffmpeg_util.probe(source)
    if not info.duration:
        raise ffmpeg_util.FFmpegError("영상 길이를 알 수 없습니다")
    log("장면 전환 분석 중...")
    cuts = detect_cuts(source, threshold)
    scenes = build_scenes(cuts, info.duration, min_length)
    log(f"장면 {len(scenes)}개 발견, 미리보기 이미지 생성 중...")
    with tempfile.TemporaryDirectory() as tmp:
        for i, sc in enumerate(scenes):
            sc.thumb = make_thumb(source, sc.start + (sc.end - sc.start) / 2, tmp)
            if progress:
                progress((i + 1) / len(scenes))

    clips = []
    if clips_dir:
        clips = [os.path.basename(p) for p in ffmpeg_util.list_media(clips_dir)]

    out_dir = os.path.dirname(os.path.abspath(output_html))
    os.makedirs(out_dir, exist_ok=True)
    video_rel = os.path.relpath(os.path.abspath(source), out_dir).replace(os.sep, "/")
    page = render_html(scenes, video_rel, os.path.basename(source), clips)
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(page)
    log(f"검토 페이지: {output_html}")
    return scenes


def render_html(scenes: list[Scene], video_src: str, title: str, clips: list[str]) -> str:
    data = [
        {"i": s.index, "s": round(s.start, 3), "e": round(s.end, 3),
         "ls": format_time(s.start)[:-2], "le": format_time(s.end)[:-2], "t": s.thumb}
        for s in scenes
    ]
    return (
        _TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__VIDEO__", html.escape(video_src, quote=True))
        .replace("__SCENES__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
        .replace("__CLIPS__", json.dumps(clips, ensure_ascii=False).replace("</", "<\\/"))
    )


_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>교체 구간 선택 - __TITLE__</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#6b7280; --line:#dde1e7;
          --pick:#e0342f; --pick-bg:#fdecec; --accent:#2563eb; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14161a; --card:#1e2127; --text:#e8eaee; --muted:#9aa1ad; --line:#30343c;
            --pick:#ff5a52; --pick-bg:#3a1f1f; --accent:#6aa0ff; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family: system-ui, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }
  header { position:sticky; top:0; z-index:5; background:var(--card); border-bottom:1px solid var(--line);
           padding:12px 16px; display:flex; flex-wrap:wrap; gap:12px; align-items:center; }
  header h1 { font-size:16px; margin:0; flex:1 1 240px; }
  header .count { color:var(--muted); font-size:14px; }
  button { font:inherit; border:1px solid var(--line); background:var(--card); color:var(--text);
           border-radius:8px; padding:8px 14px; cursor:pointer; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  main { display:grid; grid-template-columns: minmax(0,1fr); gap:16px; padding:16px; }
  @media (min-width: 1000px) { main { grid-template-columns: 420px minmax(0,1fr); }
    .player { position:sticky; top:72px; align-self:start; } }
  .player video { width:100%; border-radius:8px; background:#000; }
  .help { color:var(--muted); font-size:13px; line-height:1.6; }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap:10px; align-content:start; }
  .card { background:var(--card); border:2px solid var(--line); border-radius:10px; overflow:hidden;
          cursor:pointer; user-select:none; }
  .card img { width:100%; aspect-ratio:16/9; object-fit:cover; display:block; background:#000; }
  .card .meta { padding:6px 8px; font-size:12px; display:flex; justify-content:space-between; gap:4px; }
  .card .meta span:last-child { color:var(--muted); }
  .card select { width:calc(100% - 16px); margin:0 8px 8px; font-size:12px; display:none; }
  .card.picked { border-color:var(--pick); background:var(--pick-bg); }
  .card.picked select { display:block; }
  .card.picked .meta span:first-child::before { content:"교체 · "; color:var(--pick); font-weight:600; }
  .manual { margin-top:16px; }
  .manual textarea { width:100%; min-height:70px; font-family:ui-monospace, monospace; font-size:13px;
                     background:var(--card); color:var(--text); border:1px solid var(--line); border-radius:8px; padding:8px; }
</style>
</head>
<body>
<header>
  <h1>교체할 장면 선택 · __TITLE__</h1>
  <span class="count" id="count">선택 0개</span>
  <button id="clear">선택 해제</button>
  <button class="primary" id="save">segments.csv 저장</button>
</header>
<main>
  <section class="player">
    <video id="video" src="__VIDEO__" controls preload="metadata"></video>
    <p class="help">
      · 외국 영상이 나오는 장면을 <b>클릭</b>하면 빨간색(교체)으로 표시됩니다. 다시 누르면 해제.<br>
      · <b>Shift+클릭</b>: 이전에 누른 장면부터 범위 선택<br>
      · 장면을 선택하면 위 플레이어가 그 위치로 이동해 확인할 수 있습니다.<br>
      · 선택된 장면 아래에서 사용할 한국 영상을 고를 수 있습니다 (기본: 자동).<br>
      · 다 고르면 <b>segments.csv 저장</b>을 누르고, 저장된 파일로 영상 만들기를 실행하세요.
    </p>
    <div class="manual">
      <p class="help">장면 구분이 맞지 않으면 직접 구간을 적어도 됩니다 (한 줄에 <code>시작,끝</code>, 예: <code>1:23.5,1:30</code>)</p>
      <textarea id="manual" placeholder="1:23.5,1:30"></textarea>
    </div>
  </section>
  <section class="grid" id="grid"></section>
</main>
<script>
const SCENES = __SCENES__;
const CLIPS = __CLIPS__;
const picked = new Map();   // index -> clip name ('' = 자동)
let last = null;
const grid = document.getElementById('grid');
const video = document.getElementById('video');

function option(v, label) { const o = document.createElement('option'); o.value = v; o.textContent = label; return o; }

for (const sc of SCENES) {
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.i = sc.i;
  const img = document.createElement('img');
  img.src = sc.t; img.alt = sc.ls; img.loading = 'lazy';
  const meta = document.createElement('div');
  meta.className = 'meta';
  const a = document.createElement('span'); a.textContent = sc.ls;
  const b = document.createElement('span'); b.textContent = (sc.e - sc.s).toFixed(1) + '초';
  meta.append(a, b);
  const sel = document.createElement('select');
  sel.append(option('', '한국 영상: 자동'));
  for (const c of CLIPS) sel.append(option(c, c));
  sel.addEventListener('click', e => e.stopPropagation());
  sel.addEventListener('change', () => picked.set(sc.i, sel.value));
  card.append(img, meta, sel);
  card.addEventListener('click', e => toggle(sc.i, e.shiftKey));
  grid.append(card);
}

function setPicked(i, on) {
  const card = grid.children[i];
  if (on && !picked.has(i)) picked.set(i, card.querySelector('select').value);
  if (!on) picked.delete(i);
  card.classList.toggle('picked', on);
}

function toggle(i, range) {
  if (range && last !== null) {
    const [a, b] = last < i ? [last, i] : [i, last];
    for (let k = a; k <= b; k++) setPicked(k, true);
  } else {
    setPicked(i, !picked.has(i));
  }
  last = i;
  try { video.currentTime = SCENES[i].s + 0.05; } catch (e) {}
  update();
}

function update() { document.getElementById('count').textContent = '선택 ' + picked.size + '개'; }

document.getElementById('clear').onclick = () => { for (const i of [...picked.keys()]) setPicked(i, false); update(); };

function fmt(t) {
  const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = (t % 60).toFixed(3);
  return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':' + s.padStart(6, '0');
}
function csvCell(v) { return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }

document.getElementById('save').onclick = () => {
  const rows = [...picked.entries()].sort((x, y) => x[0] - y[0])
    .map(([i, clip]) => [fmt(SCENES[i].s), fmt(SCENES[i].e), csvCell(clip), ''].join(','));
  const manual = document.getElementById('manual').value.split('\n').map(l => l.trim()).filter(Boolean);
  if (!rows.length && !manual.length) { alert('선택한 장면이 없습니다.'); return; }
  const text = ['start,end,clip,clip_start', ...rows, ...manual].join('\n') + '\n';
  const blob = new Blob(['﻿' + text], {type: 'text/csv;charset=utf-8'});
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'segments.csv';
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
};
</script>
</body>
</html>
"""
