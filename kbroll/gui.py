"""tkinter 로 만든 간단한 창 프로그램."""

from __future__ import annotations

import os
import queue
import threading
import webbrowser

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .replace import SubtitleOptions, parse_region, replace_segments
from .scan import scan
from .segments import load_segments

SUB_MODES = {
    "글자만 보존 (흰/노란 글자 자막)": "key",
    "자막 띠 전체 보존 (배경 박스 자막)": "band",
    "보존 안 함 (화면에 자막 없음)": "none",
}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("한국 영상 교체기")
        self.minsize(640, 0)
        self.events: queue.Queue = queue.Queue()
        self.busy = False

        self.video = tk.StringVar()
        self.clips = tk.StringVar()
        self.segments = tk.StringVar()
        self.output = tk.StringVar()
        self.sub_mode = tk.StringVar(value=next(iter(SUB_MODES)))
        self.sub_region = tk.StringVar(value="0,0.72,1,0.28")
        self.shuffle = tk.BooleanVar(value=False)

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        row = 0
        for label, var, pick in [
            ("원본 영상", self.video, self._pick_video),
            ("한국 영상 폴더", self.clips, self._pick_clips),
            ("교체 구간 (segments.csv)", self.segments, self._pick_segments),
            ("결과 파일", self.output, self._pick_output),
        ]:
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(frm, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6)
            ttk.Button(frm, text="찾기", command=pick).grid(row=row, column=2)
            row += 1

        ttk.Label(frm, text="자막 처리").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(frm, textvariable=self.sub_mode, values=list(SUB_MODES),
                     state="readonly").grid(row=row, column=1, columnspan=2, sticky="ew", padx=6)
        row += 1
        ttk.Label(frm, text="자막 영역 x,y,w,h").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.sub_region).grid(row=row, column=1, sticky="ew", padx=6)
        ttk.Checkbutton(frm, text="무작위 순서", variable=self.shuffle).grid(row=row, column=2)
        row += 1

        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=3, pady=(12, 6), sticky="ew")
        btns.columnconfigure((0, 1), weight=1)
        self.scan_btn = ttk.Button(btns, text="① 장면 분석 → 교체할 장면 고르기", command=self._scan)
        self.scan_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.run_btn = ttk.Button(btns, text="② 영상 만들기", command=self._replace)
        self.run_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        row += 1

        self.bar = ttk.Progressbar(frm, maximum=100)
        self.bar.grid(row=row, column=0, columnspan=3, sticky="ew", pady=4)
        row += 1
        self.logbox = tk.Text(frm, height=12, state="disabled")
        self.logbox.grid(row=row, column=0, columnspan=3, sticky="nsew")
        frm.rowconfigure(row, weight=1)

        self.after(100, self._poll)

    # ---- 파일 선택 ----
    def _pick_video(self) -> None:
        path = filedialog.askopenfilename(
            title="원본 영상", filetypes=[("영상", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("모든 파일", "*.*")])
        if path:
            self.video.set(path)
            stem, ext = os.path.splitext(path)
            if not self.output.get():
                self.output.set(f"{stem}_한국영상교체{ext}")

    def _pick_clips(self) -> None:
        path = filedialog.askdirectory(title="한국 영상 폴더")
        if path:
            self.clips.set(path)

    def _pick_segments(self) -> None:
        path = filedialog.askopenfilename(title="교체 구간 파일", filetypes=[("CSV", "*.csv"), ("모든 파일", "*.*")])
        if path:
            self.segments.set(path)

    def _pick_output(self) -> None:
        path = filedialog.asksaveasfilename(title="결과 파일", defaultextension=".mp4",
                                            filetypes=[("MP4", "*.mp4"), ("모든 파일", "*.*")])
        if path:
            self.output.set(path)

    # ---- 작업 ----
    def _log(self, text: str) -> None:
        self.events.put(("log", text))

    def _progress(self, p: float) -> None:
        self.events.put(("progress", p))

    def _run_bg(self, work) -> None:
        if self.busy:
            return
        self.busy = True
        self.scan_btn.state(["disabled"])
        self.run_btn.state(["disabled"])
        self.bar["value"] = 0

        def target() -> None:
            try:
                work()
                self.events.put(("done", None))
            except Exception as exc:  # 사용자에게 오류를 보여준다
                self.events.put(("error", str(exc)))

        threading.Thread(target=target, daemon=True).start()

    def _scan(self) -> None:
        video = self.video.get()
        if not video:
            messagebox.showwarning("알림", "원본 영상을 선택하세요.")
            return
        out = os.path.join(os.path.dirname(os.path.abspath(video)), "kbroll_work", "review.html")

        def work() -> None:
            scan(video, out, clips_dir=self.clips.get() or None,
                 progress=self._progress, log=self._log)
            webbrowser.open("file://" + os.path.abspath(out))
            self._log("브라우저에서 교체할 장면을 고른 뒤 segments.csv 를 저장하고, "
                      "위 '교체 구간'에 그 파일을 지정하세요.")

        self._run_bg(work)

    def _replace(self) -> None:
        video, seg_path, output = self.video.get(), self.segments.get(), self.output.get()
        if not (video and seg_path and output):
            messagebox.showwarning("알림", "원본 영상, 교체 구간 파일, 결과 파일을 모두 지정하세요.")
            return
        try:
            subs = SubtitleOptions(mode=SUB_MODES[self.sub_mode.get()],
                                   region=parse_region(self.sub_region.get()))
            segments = load_segments(seg_path)
        except (ValueError, OSError) as exc:
            messagebox.showerror("오류", str(exc))
            return

        def work() -> None:
            replace_segments(video, segments, output, clips_dir=self.clips.get() or None,
                             subs=subs, shuffle=self.shuffle.get(),
                             progress=self._progress, log=self._log)

        self._run_bg(work)

    def _poll(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", value + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "progress":
                    self.bar["value"] = value * 100
                else:
                    self.busy = False
                    self.scan_btn.state(["!disabled"])
                    self.run_btn.state(["!disabled"])
                    if kind == "error":
                        messagebox.showerror("오류", value)
                    else:
                        self.bar["value"] = 100
        except queue.Empty:
            pass
        self.after(100, self._poll)


def main() -> None:
    App().mainloop()
