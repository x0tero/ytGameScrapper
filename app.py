import io
import os
import re
import queue
import shutil
import subprocess
import threading
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk

import yt_dlp


TIME_RE = re.compile(r"^\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)\s*$")

PREVIEW_W = 640
PREVIEW_H = 360
NUM_FRAMES = 6

DEFAULT_QUALITIES = [
    "Best available", "2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "Worst"
]


def parse_time(text: str) -> float | None:
    """Parse 'SS', 'MM:SS', or 'HH:MM:SS' (decimals allowed) to seconds."""
    if not text or not text.strip():
        return None
    m = TIME_RE.match(text)
    if not m:
        raise ValueError(f"Invalid time format: {text!r}. Use SS, MM:SS, or HH:MM:SS.")
    a, b, c = m.groups()
    parts = [float(p) for p in (a, b, c) if p is not None]
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


class TkLogger:
    def __init__(self, q: queue.Queue):
        self.q = q

    def debug(self, msg):
        if not msg.startswith("[debug] "):
            self.info(msg)

    def info(self, msg):
        self.q.put(("log", msg))

    def warning(self, msg):
        self.q.put(("log", f"WARNING: {msg}"))

    def error(self, msg):
        self.q.put(("log", f"ERROR: {msg}"))


def _hidden_popen_kwargs() -> dict:
    """Suppress console windows when spawning ffmpeg on Windows."""
    if os.name != "nt":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"creationflags": flags}


def find_ffmpeg() -> str | None:
    """Resolve ffmpeg: PATH first, then known winget / chocolatey locations.

    Winget's user-PATH update doesn't reach processes launched from an
    already-open shell, so scanning the install dir is the reliable fallback.
    """
    hit = shutil.which("ffmpeg")
    if hit:
        return hit
    if os.name != "nt":
        return None
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        winget_pkgs = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.is_dir():
            candidates.extend(winget_pkgs.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"))
        links = Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
        if links.is_file():
            candidates.append(links)
    program_data = os.environ.get("ProgramData")
    if program_data:
        candidates.extend(Path(program_data).glob("chocolatey/bin/ffmpeg.exe"))
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def extract_frame(ffmpeg_path: str, stream_url: str, timestamp: float) -> Image.Image | None:
    """Grab a single JPEG frame near the given timestamp using ffmpeg."""
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{timestamp:.2f}",
        "-i", stream_url,
        "-frames:v", "1",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-q:v", "3",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,
            **_hidden_popen_kwargs(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return Image.open(io.BytesIO(proc.stdout)).convert("RGB")
    except Exception:
        return None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("yt Game Scrapper")
        self.geometry("900x820")
        self.minsize(760, 720)

        self.msg_queue: queue.Queue = queue.Queue()
        self.ffmpeg_path = find_ffmpeg()
        self.has_ffmpeg = self.ffmpeg_path is not None
        if self.ffmpeg_path:
            # yt-dlp's partial-download check (FFmpegFD.available) uses PATH and
            # ignores the ffmpeg_location option, so patch PATH for this process.
            ffmpeg_dir = str(Path(self.ffmpeg_path).parent)
            existing = os.environ.get("PATH", "")
            if ffmpeg_dir not in existing.split(os.pathsep):
                os.environ["PATH"] = ffmpeg_dir + os.pathsep + existing

        # Preview/crop state
        self.frames: list[Image.Image] = []            # original frames (any size)
        self.frame_photos: list[ImageTk.PhotoImage] = []  # displayed, letterboxed
        self.frame_index: int = 0
        # crop_rect stored normalized: (x1, y1, x2, y2) in [0,1], relative to the video frame
        self.crop_rect: tuple[float, float, float, float] | None = None
        # Geometry of the currently-displayed image inside the canvas:
        # (offset_x, offset_y, width, height) in canvas pixels. Needed because
        # images are letterboxed inside a fixed canvas.
        self._img_box: tuple[int, int, int, int] = (0, 0, PREVIEW_W, PREVIEW_H)
        self._drag_start: tuple[int, int] | None = None
        self._rect_id: int | None = None

        self._build_ui()
        self.after(100, self._drain_queue)

        if self.has_ffmpeg:
            self._log(f"ffmpeg: {self.ffmpeg_path}")
        else:
            self._log(
                "Note: ffmpeg not found. Preview frame extraction and shape-cropping "
                "need ffmpeg. Basic downloads still work.\n"
                "Install: winget install Gyan.FFmpeg  (then restart this app)."
            )

    # ---------- UI ----------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True, padx=8, pady=8)

        # URL row
        ttk.Label(root, text="YouTube URL:").grid(row=0, column=0, sticky="w", **pad)
        self.url_var = tk.StringVar()
        ttk.Entry(root, textvariable=self.url_var).grid(
            row=0, column=1, columnspan=2, sticky="ew", **pad
        )
        self.load_btn = ttk.Button(root, text="Load preview", command=self._start_preview)
        self.load_btn.grid(row=0, column=3, sticky="ew", **pad)

        # Output folder row
        ttk.Label(root, text="Output folder:").grid(row=1, column=0, sticky="w", **pad)
        self.out_var = tk.StringVar(value=str(Path.cwd() / "downloads"))
        ttk.Entry(root, textvariable=self.out_var).grid(
            row=1, column=1, columnspan=2, sticky="ew", **pad
        )
        ttk.Button(root, text="Browse…", command=self._pick_folder).grid(
            row=1, column=3, sticky="ew", **pad
        )

        # Audio + time crop
        self.audio_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(root, text="Include audio", variable=self.audio_var).grid(
            row=2, column=0, sticky="w", **pad
        )

        ttk.Label(root, text="Quality:").grid(row=2, column=1, sticky="e", **pad)
        self.quality_var = tk.StringVar(value=DEFAULT_QUALITIES[0])
        self.quality_combo = ttk.Combobox(
            root,
            textvariable=self.quality_var,
            values=DEFAULT_QUALITIES,
            state="readonly",
            width=18,
        )
        self.quality_combo.grid(row=2, column=2, sticky="w", **pad)

        self.time_crop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            root,
            text="Crop time range",
            variable=self.time_crop_var,
            command=self._toggle_time_crop,
        ).grid(row=3, column=0, sticky="w", **pad)

        ttk.Label(root, text="Start:").grid(row=3, column=1, sticky="e", **pad)
        self.start_var = tk.StringVar()
        self.start_entry = ttk.Entry(root, textvariable=self.start_var, width=12)
        self.start_entry.grid(row=3, column=2, sticky="w", **pad)
        ttk.Label(root, text="End:").grid(row=3, column=2, sticky="e", **pad)
        self.end_var = tk.StringVar()
        self.end_entry = ttk.Entry(root, textvariable=self.end_var, width=12)
        self.end_entry.grid(row=3, column=3, sticky="w", **pad)

        ttk.Label(
            root,
            text="Time formats: 90 (s), 3:00 (mm:ss), 1:02:30 (hh:mm:ss)",
            foreground="#666",
        ).grid(row=4, column=1, columnspan=3, sticky="w", **pad)

        self._toggle_time_crop()

        # Preview area
        preview_frame = ttk.LabelFrame(root, text="Preview  —  click-drag on the image to draw a crop rectangle")
        preview_frame.grid(row=5, column=0, columnspan=4, sticky="nsew", **pad)

        self.canvas = tk.Canvas(
            preview_frame,
            width=PREVIEW_W,
            height=PREVIEW_H,
            bg="#111",
            highlightthickness=1,
            highlightbackground="#444",
            cursor="crosshair",
        )
        self.canvas.pack(padx=6, pady=6)
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self._canvas_placeholder = self.canvas.create_text(
            PREVIEW_W // 2,
            PREVIEW_H // 2,
            text="Paste a URL and click Load preview",
            fill="#888",
            font=("Segoe UI", 12),
        )

        nav = ttk.Frame(preview_frame)
        nav.pack(pady=(0, 6))
        self.prev_btn = ttk.Button(nav, text="◀ Prev frame", command=self._prev_frame, state="disabled")
        self.prev_btn.grid(row=0, column=0, padx=4)
        self.frame_label = ttk.Label(nav, text="No frames loaded")
        self.frame_label.grid(row=0, column=1, padx=8)
        self.next_btn = ttk.Button(nav, text="Next frame ▶", command=self._next_frame, state="disabled")
        self.next_btn.grid(row=0, column=2, padx=4)

        crop_row = ttk.Frame(preview_frame)
        crop_row.pack(pady=(0, 6))
        self.shape_crop_var = tk.BooleanVar(value=False)
        self.shape_crop_chk = ttk.Checkbutton(
            crop_row,
            text="Crop to shape (use drawn rectangle)",
            variable=self.shape_crop_var,
            state="disabled",
        )
        self.shape_crop_chk.grid(row=0, column=0, padx=6)
        self.clear_rect_btn = ttk.Button(
            crop_row, text="Clear rectangle", command=self._clear_rect, state="disabled"
        )
        self.clear_rect_btn.grid(row=0, column=1, padx=6)

        # Editable dimensions: pixel coordinates relative to the preview frame.
        dims_row = ttk.Frame(preview_frame)
        dims_row.pack(pady=(0, 8))
        self.dim_x = tk.StringVar()
        self.dim_y = tk.StringVar()
        self.dim_w = tk.StringVar()
        self.dim_h = tk.StringVar()
        self._dim_entries: list[ttk.Entry] = []
        for i, (label, var) in enumerate(
            [("X:", self.dim_x), ("Y:", self.dim_y), ("W:", self.dim_w), ("H:", self.dim_h)]
        ):
            ttk.Label(dims_row, text=label).grid(row=0, column=i * 2, padx=(8, 2))
            entry = ttk.Entry(dims_row, textvariable=var, width=6, state="disabled")
            entry.grid(row=0, column=i * 2 + 1, padx=(0, 2))
            entry.bind("<Return>", lambda _e: self._apply_dim_edit())
            entry.bind("<FocusOut>", lambda _e: self._apply_dim_edit())
            self._dim_entries.append(entry)
        self.dims_ref_label = ttk.Label(dims_row, text="", foreground="#666")
        self.dims_ref_label.grid(row=0, column=8, padx=(10, 0))

        # Action row
        self.download_btn = ttk.Button(root, text="Download", command=self._start_download)
        self.download_btn.grid(row=6, column=0, columnspan=4, sticky="ew", **pad)

        self.progress = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.progress.grid(row=7, column=0, columnspan=4, sticky="ew", **pad)

        # Log
        ttk.Label(root, text="Log:").grid(row=8, column=0, sticky="w", **pad)
        self.log = tk.Text(root, height=8, wrap="word", state="disabled")
        self.log.grid(row=9, column=0, columnspan=4, sticky="nsew", **pad)
        scroll = ttk.Scrollbar(root, orient="vertical", command=self.log.yview)
        scroll.grid(row=9, column=4, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        for c in (1, 2, 3):
            root.columnconfigure(c, weight=1)
        root.rowconfigure(9, weight=1)

    # ---------- Small helpers ----------

    def _toggle_time_crop(self):
        state = "normal" if self.time_crop_var.get() else "disabled"
        self.start_entry.configure(state=state)
        self.end_entry.configure(state=state)

    def _pick_folder(self):
        folder = filedialog.askdirectory(initialdir=self.out_var.get() or str(Path.cwd()))
        if folder:
            self.out_var.set(folder)

    def _log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    if str(self.progress.cget("mode")) != "determinate":
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                    self.progress["value"] = payload
                elif kind == "preview_frame":
                    self._add_preview_frame(payload)
                elif kind == "preview_done":
                    self._finish_preview(payload)
                elif kind == "qualities":
                    self._update_qualities(payload)
                elif kind == "busy_start":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(80)
                elif kind == "busy_stop":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress["value"] = 0
                elif kind == "done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress["value"] = 0
                    self.download_btn.configure(state="normal")
                    if payload:
                        messagebox.showerror("Download failed", payload)
                    else:
                        messagebox.showinfo("Done", "Download finished.")
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    # ---------- Preview ----------

    def _start_preview(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Paste a YouTube URL first.")
            return
        self.load_btn.configure(state="disabled")
        self.frames.clear()
        self.frame_photos.clear()
        self.frame_index = 0
        self.crop_rect = None
        self.shape_crop_var.set(False)
        self.shape_crop_chk.configure(state="disabled")
        self.clear_rect_btn.configure(state="disabled")
        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.frame_label.configure(text="Loading…")
        self.canvas.delete("all")
        self._canvas_placeholder = self.canvas.create_text(
            PREVIEW_W // 2, PREVIEW_H // 2,
            text="Loading preview…", fill="#888", font=("Segoe UI", 12),
        )
        self._log(f"Loading preview for: {url}")
        threading.Thread(target=self._preview_worker, args=(url,), daemon=True).start()

    def _preview_worker(self, url: str):
        info = None
        try:
            # Prefer a single combined mp4 so ffmpeg can seek over HTTP without needing
            # to mux two separate streams.
            with yt_dlp.YoutubeDL(
                {"format": "b[ext=mp4]/b", "quiet": True, "no_warnings": True,
                 "logger": TkLogger(self.msg_queue)}
            ) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            self.msg_queue.put(("preview_done", f"{type(e).__name__}: {e}"))
            return

        duration = info.get("duration") or 0
        stream_url = info.get("url")
        thumb = info.get("thumbnail")

        # Collect actual available (height, fps) pairs from the full format
        # list so the quality dropdown shows exactly what the server offers.
        try:
            with yt_dlp.YoutubeDL(
                {"quiet": True, "no_warnings": True, "logger": TkLogger(self.msg_queue)}
            ) as ydl2:
                full = ydl2.extract_info(url, download=False)
            pairs: set[tuple[int, int | None]] = set()
            for f in (full.get("formats") or []):
                if f.get("vcodec") in (None, "none"):
                    continue
                h = f.get("height")
                if not h:
                    continue
                fps = f.get("fps")
                pairs.add((int(h), int(round(fps)) if fps else None))
            variants = sorted(pairs, key=lambda t: (-t[0], -(t[1] or 0)))
            if variants:
                self.msg_queue.put(("qualities", variants))
        except Exception as e:
            self.msg_queue.put(("log", f"(Couldn't list qualities: {e})"))

        produced_any = False

        if self.has_ffmpeg and stream_url and duration > 1:
            # Pick NUM_FRAMES timestamps spread across the video, skipping the very
            # ends where black frames or intros are common.
            margin = min(2.0, duration * 0.05)
            usable = max(1.0, duration - 2 * margin)
            stamps = [margin + usable * i / (NUM_FRAMES - 1) for i in range(NUM_FRAMES)]
            self.msg_queue.put(("log", f"Extracting {NUM_FRAMES} preview frames with ffmpeg…"))
            for ts in stamps:
                img = extract_frame(self.ffmpeg_path, stream_url, ts)
                if img is not None:
                    produced_any = True
                    self.msg_queue.put(("preview_frame", img))

        if not produced_any and thumb:
            # Fallback: single thumbnail download.
            try:
                self.msg_queue.put(("log", "Falling back to video thumbnail."))
                with urllib.request.urlopen(thumb, timeout=15) as resp:
                    data = resp.read()
                img = Image.open(io.BytesIO(data)).convert("RGB")
                self.msg_queue.put(("preview_frame", img))
                produced_any = True
            except Exception as e:
                self.msg_queue.put(("log", f"Thumbnail fetch failed: {e}"))

        if not produced_any:
            self.msg_queue.put(("preview_done", "Could not produce any preview image."))
        else:
            self.msg_queue.put(("preview_done", None))

    def _add_preview_frame(self, img: Image.Image):
        self.frames.append(img)
        photo, box = self._fit_to_canvas(img)
        self.frame_photos.append(photo)
        if len(self.frames) == 1:
            self._show_frame(0)

    def _update_qualities(self, variants: list[tuple[int, int | None]]):
        labels: list[str] = []
        seen: set[str] = set()
        for h, fps in variants:
            label = f"{h}p{fps}" if fps else f"{h}p"
            if label not in seen:
                seen.add(label)
                labels.append(label)
        values = ["Best available"] + labels + ["Worst"]
        current = self.quality_var.get()
        self.quality_combo.configure(values=values)
        if current not in values:
            self.quality_var.set(values[0])
        self._log(f"Available qualities: {', '.join(labels)}")

    def _finish_preview(self, err: str | None):
        self.load_btn.configure(state="normal")
        if err:
            self._log(f"Preview error: {err}")
            self.frame_label.configure(text="Preview failed")
            self.canvas.delete("all")
            self.canvas.create_text(
                PREVIEW_W // 2, PREVIEW_H // 2,
                text="Preview failed — see log", fill="#c66", font=("Segoe UI", 12),
            )
            return
        n = len(self.frames)
        self._log(f"Preview ready ({n} frame{'s' if n != 1 else ''}).")
        self.frame_label.configure(text=f"Frame 1 / {n}")
        if n > 1:
            self.prev_btn.configure(state="normal")
            self.next_btn.configure(state="normal")

    def _fit_to_canvas(self, img: Image.Image) -> tuple[ImageTk.PhotoImage, tuple[int, int, int, int]]:
        """Letterbox `img` to (PREVIEW_W, PREVIEW_H), preserving aspect."""
        iw, ih = img.size
        scale = min(PREVIEW_W / iw, PREVIEW_H / ih)
        w = max(1, int(iw * scale))
        h = max(1, int(ih * scale))
        resized = img.resize((w, h), Image.LANCZOS)
        off_x = (PREVIEW_W - w) // 2
        off_y = (PREVIEW_H - h) // 2
        return ImageTk.PhotoImage(resized), (off_x, off_y, w, h)

    def _show_frame(self, idx: int):
        if not self.frames:
            return
        idx = max(0, min(idx, len(self.frames) - 1))
        self.frame_index = idx
        photo = self.frame_photos[idx]
        # Recompute image box from the original frame (all frames may not share aspect).
        _, self._img_box = self._fit_to_canvas(self.frames[idx])

        self.canvas.delete("all")
        off_x, off_y, w, h = self._img_box
        self.canvas.create_image(off_x, off_y, anchor="nw", image=photo)
        self.frame_label.configure(text=f"Frame {idx + 1} / {len(self.frames)}")
        # Redraw the crop rectangle (if any) in the new image's canvas coords.
        if self.crop_rect is not None:
            x1, y1, x2, y2 = self.crop_rect
            cx1 = off_x + x1 * w
            cy1 = off_y + y1 * h
            cx2 = off_x + x2 * w
            cy2 = off_y + y2 * h
            self._rect_id = self.canvas.create_rectangle(
                cx1, cy1, cx2, cy2, outline="#f44", width=2
            )
        else:
            self._rect_id = None

    def _prev_frame(self):
        self._show_frame(self.frame_index - 1)

    def _next_frame(self):
        self._show_frame(self.frame_index + 1)

    # ---------- Rectangle drawing ----------

    def _on_drag_start(self, ev):
        if not self.frames:
            return
        self._drag_start = (ev.x, ev.y)
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
            self._rect_id = None
        self._rect_id = self.canvas.create_rectangle(
            ev.x, ev.y, ev.x, ev.y, outline="#f44", width=2
        )

    def _on_drag_move(self, ev):
        if self._drag_start is None or self._rect_id is None:
            return
        x0, y0 = self._drag_start
        self.canvas.coords(self._rect_id, x0, y0, ev.x, ev.y)

    def _on_drag_end(self, ev):
        if self._drag_start is None or self._rect_id is None:
            return
        x0, y0 = self._drag_start
        x1, y1 = ev.x, ev.y
        self._drag_start = None

        off_x, off_y, w, h = self._img_box
        # Clamp to the image box (ignore letterbox margin).
        def clamp(v, lo, hi): return max(lo, min(hi, v))
        x0 = clamp(x0, off_x, off_x + w)
        x1 = clamp(x1, off_x, off_x + w)
        y0 = clamp(y0, off_y, off_y + h)
        y1 = clamp(y1, off_y, off_y + h)
        lx, rx = sorted((x0, x1))
        ty, by = sorted((y0, y1))

        if rx - lx < 4 or by - ty < 4:
            # Too small — treat as a cancel.
            self.canvas.delete(self._rect_id)
            self._rect_id = None
            return

        # Redraw normalized rectangle on the canvas (may have been clamped).
        self.canvas.coords(self._rect_id, lx, ty, rx, by)

        # Store normalized relative to the *image* (not the canvas).
        self.crop_rect = (
            (lx - off_x) / w,
            (ty - off_y) / h,
            (rx - off_x) / w,
            (by - off_y) / h,
        )
        self.shape_crop_chk.configure(state="normal")
        self.shape_crop_var.set(True)
        self.clear_rect_btn.configure(state="normal")
        self._sync_dim_entries()
        self._log(
            "Crop rectangle: "
            f"x={self.crop_rect[0]:.2%}, y={self.crop_rect[1]:.2%}, "
            f"w={self.crop_rect[2]-self.crop_rect[0]:.2%}, "
            f"h={self.crop_rect[3]-self.crop_rect[1]:.2%} of the video frame."
        )

    def _ref_dims(self) -> tuple[int, int] | None:
        """Reference (width, height) used to show pixel values in the X/Y/W/H
        boxes. We use the first frame's original resolution — all extracted
        frames share the source video's dimensions, so it's stable across
        Prev/Next."""
        if not self.frames:
            return None
        return self.frames[0].size

    def _sync_dim_entries(self):
        """Push self.crop_rect -> the X/Y/W/H entry boxes, in pixels."""
        ref = self._ref_dims()
        if ref is None or self.crop_rect is None:
            for v in (self.dim_x, self.dim_y, self.dim_w, self.dim_h):
                v.set("")
            for e in self._dim_entries:
                e.configure(state="disabled")
            self.dims_ref_label.configure(text="")
            return
        rw, rh = ref
        x1, y1, x2, y2 = self.crop_rect
        self.dim_x.set(str(int(round(x1 * rw))))
        self.dim_y.set(str(int(round(y1 * rh))))
        self.dim_w.set(str(int(round((x2 - x1) * rw))))
        self.dim_h.set(str(int(round((y2 - y1) * rh))))
        for e in self._dim_entries:
            e.configure(state="normal")
        self.dims_ref_label.configure(text=f"/ {rw}×{rh}")

    def _apply_dim_edit(self):
        """Read the X/Y/W/H entry boxes, clamp, and redraw the rectangle."""
        ref = self._ref_dims()
        if ref is None:
            return
        rw, rh = ref
        try:
            x = int(float(self.dim_x.get()))
            y = int(float(self.dim_y.get()))
            w = int(float(self.dim_w.get()))
            h = int(float(self.dim_h.get()))
        except ValueError:
            # Invalid entry — re-sync so the user sees the last good values.
            self._sync_dim_entries()
            return
        x = max(0, min(rw - 1, x))
        y = max(0, min(rh - 1, y))
        w = max(1, min(rw - x, w))
        h = max(1, min(rh - y, h))

        new_rect = (x / rw, y / rh, (x + w) / rw, (y + h) / rh)
        if new_rect == self.crop_rect:
            return
        self.crop_rect = new_rect
        # Redraw rectangle on the canvas in its (possibly different) geometry.
        off_x, off_y, cw, ch = self._img_box
        x1, y1, x2, y2 = self.crop_rect
        cx1 = off_x + x1 * cw
        cy1 = off_y + y1 * ch
        cx2 = off_x + x2 * cw
        cy2 = off_y + y2 * ch
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(
            cx1, cy1, cx2, cy2, outline="#f44", width=2
        )
        # Re-sync in case clamping changed anything.
        self._sync_dim_entries()

    def _clear_rect(self):
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
            self._rect_id = None
        self.crop_rect = None
        self.shape_crop_var.set(False)
        self.shape_crop_chk.configure(state="disabled")
        self.clear_rect_btn.configure(state="disabled")
        self._sync_dim_entries()
        self._log("Crop rectangle cleared.")

    # ---------- Download ----------

    def _start_download(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Please paste a YouTube URL.")
            return

        out_dir = Path(self.out_var.get().strip() or "downloads")
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Bad folder", str(e))
            return

        start = end = None
        if self.time_crop_var.get():
            try:
                start = parse_time(self.start_var.get())
                end = parse_time(self.end_var.get())
            except ValueError as e:
                messagebox.showerror("Invalid time", str(e))
                return
            if start is None and end is None:
                messagebox.showwarning(
                    "Crop", "Enter a start and/or end time, or uncheck 'Crop time range'."
                )
                return
            if start is not None and end is not None and end <= start:
                messagebox.showerror("Crop", "End time must be greater than start time.")
                return
            if not self.has_ffmpeg:
                if not messagebox.askyesno(
                    "ffmpeg missing",
                    "Time cropping needs ffmpeg, which wasn't found. Continue anyway?",
                ):
                    return

        shape_crop = self.shape_crop_var.get() and self.crop_rect is not None
        if shape_crop and not self.has_ffmpeg:
            messagebox.showerror(
                "ffmpeg missing",
                "Shape cropping needs ffmpeg. Install it and restart the app.",
            )
            return

        opts = self._build_ydl_opts(out_dir, self.audio_var.get())
        self.download_btn.configure(state="disabled")
        self.progress["value"] = 0
        self._log(f"Starting download: {url}")

        rect = self.crop_rect if shape_crop else None
        threading.Thread(
            target=self._run_download,
            args=(url, opts, rect, start, end),
            daemon=True,
        ).start()

    _QUALITY_RE = re.compile(r"^(\d+)p(\d+)?$")

    def _format_selector(self, include_audio: bool, quality: str) -> str:
        """Map the UI quality choice to a yt-dlp -f expression.

        Caps by height, and optionally by fps when the label carries one
        (e.g. '1080p30' limits to 30fps so a 60fps stream isn't picked).
        """
        q = (quality or "").strip().lower()
        if q == "worst":
            return "wv*+wa/w" if include_audio and self.has_ffmpeg else ("w" if include_audio else "wv*/w")
        cap = ""
        m = self._QUALITY_RE.match(q)
        if m:
            height, fps = m.group(1), m.group(2)
            cap = f"[height<={height}]"
            if fps:
                cap += f"[fps<={fps}]"
        if include_audio:
            if self.has_ffmpeg:
                return f"bv*{cap}+ba/b{cap}/b"
            return f"b{cap}/b"
        return f"bv*{cap}/b{cap}/b"

    def _build_ydl_opts(self, out_dir: Path, include_audio: bool) -> dict:
        # Download to an ASCII-only filename keyed by the video id so ffmpeg on
        # Windows never has to open a path with fullwidth/Unicode characters
        # (that's what "Error opening output files: Invalid argument" is). The
        # real title is restored at the end, with Windows-invalid chars stripped.
        outtmpl = str(out_dir / "ytdl_%(id)s.%(ext)s")
        fmt = self._format_selector(include_audio, self.quality_var.get())

        opts: dict = {
            "outtmpl": outtmpl,
            "format": fmt,
            "noplaylist": True,
            "logger": TkLogger(self.msg_queue),
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._pp_hook],
            "quiet": True,
        }
        if self.ffmpeg_path:
            opts["ffmpeg_location"] = self.ffmpeg_path
        return opts

    def _progress_hook(self, d: dict):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            frag_i = d.get("fragment_index")
            frag_n = d.get("fragment_count")
            if total:
                self.msg_queue.put(("progress", min(100.0, downloaded * 100.0 / total)))
            else:
                # No byte total (FFmpegFD / some HLS): keep the bar alive.
                self.msg_queue.put(("busy_start", None))
            speed = d.get("speed")
            eta = d.get("eta")
            msg = f"Downloading… {downloaded/1_000_000:.1f} MB"
            if frag_i and frag_n:
                msg += f"  frag {frag_i}/{frag_n}"
            if speed:
                msg += f" @ {speed/1_000_000:.2f} MB/s"
            if eta:
                msg += f", ETA {eta}s"
            self.msg_queue.put(("log", msg))
        elif status == "finished":
            self.msg_queue.put(("progress", 100))
            info = d.get("info_dict") or {}
            w, h, fps = info.get("width"), info.get("height"), info.get("fps")
            if w and h:
                fps_txt = f" @ {int(round(fps))}fps" if fps else ""
                self.msg_queue.put(("log", f"Got {w}×{h}{fps_txt}"))
            self.msg_queue.put(("log", f"Saved: {d.get('filename', '')}"))

    def _pp_hook(self, d: dict):
        """yt-dlp post-processing hook — shows merge/remux activity."""
        status = d.get("status")
        pp = d.get("postprocessor") or "postprocess"
        if status == "started":
            self.msg_queue.put(("log", f"{pp}: running ffmpeg (this can take a while on large videos)…"))
            self.msg_queue.put(("busy_start", None))
        elif status == "finished":
            self.msg_queue.put(("log", f"{pp}: done."))
            self.msg_queue.put(("busy_stop", None))

    def _run_download(
        self,
        url: str,
        opts: dict,
        rect: tuple[float, float, float, float] | None,
        start: float | None,
        end: float | None,
    ):
        err = None
        working_file: Path | None = None
        info: dict | None = None
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                candidate = None
                if info is not None:
                    rd = info.get("requested_downloads") or []
                    if rd:
                        candidate = rd[0].get("filepath") or rd[0].get("_filename")
                    if not candidate:
                        candidate = info.get("_filename") or info.get("filepath")
                if candidate:
                    working_file = Path(candidate)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            self.msg_queue.put(("log", err))

        need_post = (rect is not None) or (start is not None) or (end is not None)
        if err is None and need_post and working_file and working_file.exists():
            new_path, pp_err = self._apply_post_process(working_file, rect, start, end)
            if pp_err:
                err = pp_err
            elif new_path:
                working_file = new_path

        # Rename the ASCII-named file back to the real title (Windows-safe).
        if err is None and info is not None and working_file and working_file.exists():
            pretty = self._pretty_output_path(info, working_file)
            if pretty and pretty != working_file:
                try:
                    working_file.rename(pretty)
                    working_file = pretty
                    self.msg_queue.put(("log", f"Final file: {pretty.name}"))
                except OSError as e:
                    self.msg_queue.put((
                        "log",
                        f"(Kept ASCII filename {working_file.name}; rename to title failed: {e})",
                    ))

        self.msg_queue.put(("done", err))

    @staticmethod
    def _windows_safe(name: str) -> str:
        """Replace filename characters Windows forbids, preserve Unicode."""
        # <>:"/\|?* and control chars are forbidden on NTFS.
        trans = {ord(c): "_" for c in '<>:"/\\|?*'}
        out = name.translate(trans)
        # Strip trailing dots/spaces (also forbidden).
        return out.rstrip(" .") or "video"

    def _pretty_output_path(self, info: dict, working: Path) -> Path | None:
        title = info.get("title") or info.get("id") or working.stem
        vid = info.get("id") or ""
        stem = self._windows_safe(f"{title} [{vid}]".strip())
        return working.with_name(stem + working.suffix)

    def _apply_post_process(
        self,
        path: Path,
        rect: tuple[float, float, float, float] | None,
        start: float | None,
        end: float | None,
    ) -> tuple[Path | None, str | None]:
        """Trim and/or shape-crop in one ffmpeg pass.

        Writes to an ASCII-safe sibling filename so Windows never hits the
        "Invalid argument" open-output error on Unicode titles. Returns the
        resulting path and an optional error message.
        """
        # Sibling file with a simple ASCII stem — guaranteed safe path.
        out = path.with_name(path.stem + "__post" + path.suffix)
        cmd: list[str] = [
            self.ffmpeg_path or "ffmpeg", "-y",
            "-hide_banner", "-loglevel", "error", "-stats",
        ]
        if start is not None:
            cmd += ["-ss", f"{start:.3f}"]
        cmd += ["-i", str(path)]
        if end is not None:
            duration = end - (start or 0.0)
            cmd += ["-t", f"{duration:.3f}"]

        # Optional stream maps: don't fail if the input has no audio.
        cmd += ["-map", "0:v?", "-map", "0:a?"]

        if rect is not None:
            x1, y1, x2, y2 = rect
            w_ratio, h_ratio, x_ratio, y_ratio = (x2 - x1), (y2 - y1), x1, y1
            crop_expr = (
                f"crop=trunc(iw*{w_ratio:.6f}/2)*2:"
                f"trunc(ih*{h_ratio:.6f}/2)*2:"
                f"trunc(iw*{x_ratio:.6f}/2)*2:"
                f"trunc(ih*{y_ratio:.6f}/2)*2"
            )
            cmd += [
                "-vf", crop_expr,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "copy",
            ]
            tag = "trim+crop" if (start is not None or end is not None) else "crop"
            self.msg_queue.put(("log", f"Post-process: {tag} {crop_expr}"))
        else:
            cmd += ["-c", "copy"]
            self.msg_queue.put(("log", "Post-process: trimming (stream copy, no re-encode)."))

        cmd += ["-movflags", "+faststart", str(out)]
        self.msg_queue.put(("busy_start", None))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                **_hidden_popen_kwargs(),
            )
        except FileNotFoundError:
            self.msg_queue.put(("busy_stop", None))
            return None, "ffmpeg vanished between startup and post-process — is it still installed?"

        assert proc.stderr is not None
        last_err = ""
        buf = ""
        while True:
            ch = proc.stderr.read(1)
            if not ch:
                break
            if ch in ("\r", "\n"):
                if buf.strip():
                    self.msg_queue.put(("log", buf.strip()))
                    last_err = buf.strip()
                buf = ""
            else:
                buf += ch
        if buf.strip():
            self.msg_queue.put(("log", buf.strip()))
            last_err = buf.strip()
        rc = proc.wait()
        self.msg_queue.put(("busy_stop", None))

        if rc != 0:
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
            return None, f"ffmpeg post-process failed: {last_err or f'exit code {rc}'}"

        # Replace the original ASCII-named file with the processed one.
        try:
            path.unlink()
            out.rename(path)
            final = path
        except OSError as e:
            self.msg_queue.put(("log", f"(Kept post-processed file as {out.name}; {e})"))
            final = out
        self.msg_queue.put(("log", f"Post-processed file saved: {final.name}"))
        return final, None


if __name__ == "__main__":
    App().mainloop()
