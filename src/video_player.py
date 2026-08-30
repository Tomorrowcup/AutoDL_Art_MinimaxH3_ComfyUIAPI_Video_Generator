# -*- coding: utf-8 -*-
"""视频播放器（cv2 帧控制版）：精确逐帧、颜色正确、自适应画布、可选音频同步播放。

帧数据以 BGR numpy 数组保存；显示到 tkinter 时转 RGB PIL；导出时原样 BGR 存 PNG。
"""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import numpy as np
from PIL import Image, ImageTk

import ffmpeg_utils

APP_DIR = os.getcwd()


def set_app_dir(x):
    global APP_DIR
    APP_DIR = x


def frames_dir():
    d = os.path.join(APP_DIR, "extracted_frames")
    os.makedirs(d, exist_ok=True)
    return d


class VideoPlayerFrame(ttk.Frame):
    """cv2 帧级播放器：精确帧步进/跳转、颜色正确、自适应缩放、可选声音播放。"""

    def __init__(self, master, on_extracted=None, **kwargs):
        super().__init__(master, **kwargs)
        self.on_extracted = on_extracted
        self.cap = None
        self.path = ""
        self.fps = 24.0
        self.total_frames = 0
        self.cur = 0               # 当前帧号
        self._playing = False
        self._play_thread = None
        self._stop_evt = threading.Event()
        self._audio_player = None
        self._audio_muted = False  # 默认开声音
        self._audio_gen = 0         # 音频准备代际（open/teardown 递增使旧线程结果失效）
        self._thumb = None         # 当前显示用 PhotoImage
        self._display_bgr = None   # 当前显示帧（BGR，供导出）
        self._build()
        self.bind("<Configure>", lambda e: self._redraw())
        self._updating = False

    def _build(self):
        # grid 布局：画布占可扩展区(0,0)，控制区固定底部(1,0)，任何窗口下按钮可见
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, bg="#111", highlightthickness=0, bd=0)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=4, pady=(4, 0))
        self._draw_placeholder()

        # 进度条：紧贴画布下方（视觉融合）
        self.var_seek = tk.DoubleVar(value=0.0)
        self.seek = ttk.Scale(self, from_=0, to=100, orient="horizontal",
                              command=self._on_seek_slide)
        self.seek.grid(row=1, column=0, sticky="ew", padx=6, pady=(2, 0))
        self.seek.bind("<Button-1>", self._seek_press)
        self.seek.bind("<ButtonRelease-1>", self._seek_commit)
        self._updating_seek = False

        ctrl_top = ttk.Frame(self)
        ctrl_top.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 2))

        # 第一行：文件名（左）+ 播放控制组（最左优先）
        r1 = ttk.Frame(ctrl_top)
        r1.pack(fill="x", pady=2)
        ttk.Button(r1, text="打开...", width=6, command=self._open_dialog).pack(side="left", padx=(0, 8))
        self.btn_play = ttk.Button(r1, text="▶ 播放", width=7, command=self._toggle_play)
        self.btn_play.pack(side="left", padx=1)
        self.btn_pause = ttk.Button(r1, text="⏸ 暂停", width=7, command=self._pause)
        self.btn_pause.pack(side="left", padx=1)
        self.btn_mute = ttk.Button(r1, text="🔇 静音", width=7, command=self._toggle_audio)
        self.btn_mute.pack(side="left", padx=1)
        self.lbl_sep = ttk.Separator(r1, orient="vertical")
        self.lbl_sep.pack(side="left", fill="y", padx=6, pady=2)
        ttk.Button(r1, text="◀ 上一帧", width=7, command=lambda: self._step(-1)).pack(side="left", padx=1)
        ttk.Button(r1, text="下一帧 ▶", width=7, command=lambda: self._step(+1)).pack(side="left", padx=1)
        self.lbl_sep2 = ttk.Separator(r1, orient="vertical")
        self.lbl_sep2.pack(side="left", fill="y", padx=6, pady=2)
        ttk.Button(r1, text="提取当前帧", width=9, command=self._extract_current).pack(side="left", padx=1)
        ttk.Button(r1, text="提取首帧", width=9, command=self._extract_first).pack(side="left", padx=1)
        ttk.Button(r1, text="提取末帧", width=9, command=self._extract_last).pack(side="left", padx=1)
        self.btn_use_last = ttk.Button(r1, text="末帧→首帧素材", width=13,
                                       command=self._use_last_as_first, state="disabled")
        self.btn_use_last.pack(side="left", padx=1)
        ttk.Button(r1, text="帧库", width=5, command=self._open_frames_dir).pack(side="left", padx=1)
        self.var_file = tk.StringVar(value="未打开视频")
        ttk.Label(r1, textvariable=self.var_file, anchor="w",
                  foreground="#555").pack(side="left", padx=8)

        # 第二行：帧号/时间输入 + 提示
        r2 = ttk.Frame(ctrl_top)
        r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="帧号:").pack(side="left")
        self.var_frame = tk.StringVar(value="0")
        ent = ttk.Entry(r2, textvariable=self.var_frame, width=7)
        ent.pack(side="left", padx=2)
        ent.bind("<Return>", lambda e: self._jump())
        self.lbl_total_f = ttk.Label(r2, text="/0")
        self.lbl_total_f.pack(side="left")
        ttk.Button(r2, text="跳转", width=5, command=self._jump).pack(side="left", padx=2)
        ttk.Label(r2, text=" 时间s:").pack(side="left")
        self.var_pos = tk.StringVar(value="0.00")
        ent2 = ttk.Entry(r2, textvariable=self.var_pos, width=7)
        ent2.pack(side="left", padx=2)
        ent2.bind("<Return>", lambda e: self._jump_time())
        self.lbl_total_t = ttk.Label(r2, text="/0.0")
        self.lbl_total_t.pack(side="left")
        ttk.Button(r2, text="跳转", width=5, command=self._jump_time).pack(side="left", padx=2)

        self.lbl_msg = ttk.Label(r2, text="", foreground="#888")
        self.lbl_msg.pack(side="left", padx=10)

    # ---------- 打开/关闭 ----------
    def open_video(self, path):
        if not os.path.exists(path):
            messagebox.showerror("错误", f"文件不存在: {path}")
            return
        self._teardown()
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            messagebox.showerror("错误", f"无法打开视频: {path}")
            return
        self.cap = cap
        self.path = path
        self._audio_gen += 1   # 使旧视频的后台音频准备失效
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.total_frames = n
        self.cur = 0
        self.var_file.set(os.path.basename(path))
        self.lbl_total_f.config(text=f"/{n}")
        self.lbl_total_t.config(text=f"/{n / self.fps:.2f}")
        self.btn_use_last.config(state="normal")
        self._audio_player = None
        self.var_frame.set("0")
        self.var_pos.set("0.00")
        self._show_frame(0)
        self.lbl_msg.config(text=f"帧率 {self.fps:.1f} | 总帧数 {n} | 时长 {n / self.fps:.2f}s")
        self.btn_mute.config(text="🔊 开声")
        self.after(50, self._ensure_audio)  # 懒准备音频（不阻塞打开）

    def _teardown(self):
        self._pause()
        if self.cap:
            self.cap.release()
            self.cap = None
        if self._audio_player:
            try:
                self._audio_player.stop()
            except Exception:
                pass
            self._audio_player = None
        self._audio_gen += 1   # 使进行中的后台音频准备失效
        self._display_bgr = None
        self._thumb = None

    def close_video(self):
        self._teardown()
        self.var_file.set("未打开视频")
        self.var_frame.set("0")
        self.var_pos.set("0.00")
        self.lbl_total_f.config(text="/0")
        self.lbl_total_t.config(text="/0.0")
        self.btn_use_last.config(state="disabled")
        self._draw_placeholder()

    # ---------- 进度条 ----------
    def _seek_pct(self):
        if self.total_frames <= 0:
            return 0.0
        return self.cur / self.total_frames * 100.0

    def _on_seek_slide(self, val):
        """拖动进度条：实时显示当前位置（推迟 seek 到鼠标释放，避免卡顿）"""
        if getattr(self, "_updating_seek", False):
            return
        try:
            pct = float(val)
        except (ValueError, TypeError):
            return
        if self.cap is None:
            return
        # 拖动中实时预览
        target_frame = int(pct / 100.0 * self.total_frames)
        target_frame = max(0, min(self.total_frames - 1, target_frame))
        self._preview_frame(target_frame)
        self.var_frame.set(str(target_frame))
        self.var_pos.set(f"{target_frame / self.fps:.2f}")

    def _preview_frame(self, idx):
        """seek 目标帧但不改变 cur（拖动时轻量预览）"""
        if self.cap is None:
            return
        idx = max(0, min(self.total_frames - 1, idx))
        cap = self.cap
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if ok and frame is not None:
            self._display_bgr = frame
            self._redraw()

    def _seek_to_frame(self, idx):
        """提交最终 seek"""
        self._pause()
        idx = max(0, min(self.total_frames - 1, idx))
        self._show_frame(idx)
        self._sync_seek_bar()

    def _sync_seek_bar(self):
        if self.total_frames > 0 and hasattr(self, "seek"):
            try:
                self._updating_seek = True
                self.seek.set(self._seek_pct())
            except Exception:
                pass
            finally:
                self._updating_seek = False

    def _seek_press(self, event=None):
        """点击轨道或按下滑块：暂停播放并立即跳转到点击位置（点击即跳，非拖也可）"""
        self._pause()
        if event is None or self.cap is None:
            return
        try:
            w = self.seek.winfo_width()
            if w <= 0:
                return
            # 点击位置比例（考虑 ttk.Scale 的槽边距，近似用 x/w）
            ratio = max(0.0, min(1.0, event.x / w))
            target = int(ratio * self.total_frames)
            self._seek_to_frame(target)
        except Exception:
            pass

    def _seek_commit(self, event=None):
        """进度条拖动结束：提交最终 seek（若拖动了滑块，以最终值为准）"""
        if self.cap is None:
            return
        try:
            pct = float(self.seek.get())
        except Exception:
            return
        target = int(pct / 100.0 * self.total_frames)
        self._seek_to_frame(target)

    # ---------- 帧基础 ----------
    def _read_frame_at(self, idx):
        """读指定帧（BGR）. idx 0..total-1"""
        idx = max(0, min(self.total_frames - 1, idx))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = self.cap.read()
        return idx, ok, frame

    def _show_frame(self, idx):
        if self.cap is None:
            return
        idx, ok, frame = self._read_frame_at(idx)
        if not ok:
            return
        self.cur = idx
        self._display_bgr = frame
        self.var_frame.set(str(idx))
        self.var_pos.set(f"{idx / self.fps:.2f}")
        self._redraw()

    def _redraw(self):
        """把 _display_bgr 按 canvas 大小等比缩放后绘制"""
        if self._display_bgr is None:
            self._draw_placeholder()
            return
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)
        frame = self._display_bgr
        h, w = frame.shape[:2]
        scale = min(cw / w, ch / h)
        nh, nw = max(2, int(h * scale)), max(2, int(w * scale))
        if nh != h or nw != w:
            frame = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        self._thumb = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, image=self._thumb)

    def _draw_placeholder(self):
        self.canvas.delete("all")
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)
        self.canvas.create_text(cw // 2, ch // 2,
                                text="双击任务列表中的视频\n打开查看", fill="#666", justify="center")

    # ---------- 帧操作 ----------
    def _step(self, delta):
        """delta=+1 下一帧（前进），-1 上一帧（后退）"""
        if self.cap is None:
            return
        self._pause()
        self._show_frame(self.cur + delta)

    def _jump(self):
        if self.cap is None:
            return
        try:
            f = int(self.var_frame.get())
        except ValueError:
            return
        self._pause()
        self._show_frame(f)

    def _jump_time(self):
        if self.cap is None:
            return
        try:
            t = float(self.var_pos.get())
        except ValueError:
            return
        self._pause()
        self._show_frame(int(t * self.fps))

    # ---------- 播放（后台线程）+ 音频 ----------
    def _ensure_audio(self):
        """异步准备音频：后台抽取音轨（ffmpeg），完成后 _audio_player 可用，不阻塞 UI"""
        if self._audio_player is not None:
            return
        if getattr(self, "_audio_busy_gen", -1) == self._audio_gen:
            return  # 当前视频已在准备中
        if not self.path:
            return
        gen = self._audio_gen
        self._audio_busy_gen = gen
        src = self.path

        def work():
            try:
                player = self._build_audio_player(src)
                if player is not None and gen == self._audio_gen:
                    self._audio_player = player
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _build_audio_player(self, src):
        """抽取音轨并构建播放器（纯计算，无 Tk 调用，可后台执行）"""
        import sounddevice as sd
        import wave
        tmp = os.path.join(APP_DIR, "imported_cache")
        os.makedirs(tmp, exist_ok=True)
        # 清理旧缓存，避免 imported_cache 无限增长
        try:
            for old in os.listdir(tmp):
                if old.startswith("audio_") and old.endswith(".wav"):
                    try:
                        os.remove(os.path.join(tmp, old))
                    except Exception:
                        pass
        except Exception:
            pass
        wav_p = os.path.join(tmp, f"audio_{int(time.time() * 1000) % 100000000}.wav")
        cmd = [ffmpeg_utils.ffmpeg_path(), "-y", "-i", src, "-vn",
               "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", wav_p]
        import subprocess
        subprocess.run(cmd, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                       timeout=120)
        if not os.path.exists(wav_p):
            return None
        with wave.open(wav_p, "rb") as wf:
            sr = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
        snd = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2).mean(axis=1) / 32768.0

        class _Player:
            def __init__(self, arr, rate):
                self.arr = arr
                self.rate = rate
                self.muted = False

            def play(self, start=0.0):
                if self.muted:
                    return
                sd.stop()
                idx = int(start * self.rate)
                sd.play(self.arr[idx:] if idx < len(self.arr) else self.arr, self.rate)

            def pause(self):
                sd.stop()

            def stop(self):
                sd.stop()

        return _Player(snd, sr)

    def _toggle_play(self):
        if self._playing:
            self._pause()
        else:
            self._play()

    def _play(self):
        if self.cap is None:
            return
        if self.cur >= self.total_frames - 1:
            self._show_frame(0)
        if not self._audio_muted:
            self._ensure_audio()
            if self._audio_player:
                self._audio_player.play(self.cur / self.fps)
        self._stop_evt.clear()
        self._playing = True
        self._play_thread = threading.Thread(target=self._play_loop, daemon=True)
        self._play_thread.start()
        self.lbl_msg.config(text="播放中")

    def _play_loop(self):
        # 顺序读取：从当前帧继续
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.cur + 1)
        idx = self.cur + 1
        interval = 1.0 / self.fps
        last = time.time()
        while self._playing and not self._stop_evt.is_set() and self.cap is not None:
            if idx >= self.total_frames:
                break
            ok, frame = self.cap.read()
            if not ok or frame is None:
                break
            self.cur = idx
            self._display_bgr = frame
            try:
                self.after(0, self._sync_ui)
            except RuntimeError:
                pass  # 主循环未运行（如关闭窗口瞬间），忽略本帧刷新
            now = time.time()
            dt = interval - (now - last)
            if dt > 0:
                time.sleep(dt)
            last = now + max(0, dt)
            idx += 1
        try:
            self.after(0, self._pause)
        except RuntimeError:
            pass

    def _sync_ui(self):
        self.var_frame.set(str(self.cur))
        self.var_pos.set(f"{self.cur / self.fps:.2f}")
        self._sync_seek_bar()
        self._redraw()

    def _pause(self):
        self._playing = False
        self._stop_evt.set()
        # 等待播放线程退出（最多 0.5s），避免主线程与播放线程并发操作 cv2 cap
        t = self._play_thread
        if t is not None and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout=0.5)
        if self._audio_player:
            self._audio_player.pause()
        self.btn_play.config(text="▶ 播放")

    def _toggle_audio(self):
        self._audio_muted = not self._audio_muted
        if self._audio_player:
            self._audio_player.muted = self._audio_muted
        if self._audio_muted:
            self.btn_mute.config(text="🔇 静音")
            if self._audio_player:
                self._audio_player.pause()
        else:
            self.btn_mute.config(text="🔊 开声")
            self._ensure_audio()
            self.lbl_msg.config(text="已开启声音（播放时同步）")

    # ---------- 提取帧 ----------
    def _save_bgr(self, idx, tag=""):
        if self.cap is None:
            return None
        idx, ok, frame = self._read_frame_at(idx)
        if not ok:
            return None
        d = frames_dir()
        ts = time.strftime("%Y%m%d_%H%M%S")
        out = os.path.join(d, f"frame_{ts}_{tag or idx}.png")
        cv2.imwrite(out, frame)
        return out

    def _extract_current(self):
        p = self._save_bgr(self.cur, "cur")
        if p:
            self.lbl_msg.config(text=f"已保存: {os.path.basename(p)}")
            if self.on_extracted:
                self.on_extracted(p)

    def _extract_first(self):
        p = self._save_bgr(0, "first")
        if p:
            self.lbl_msg.config(text=f"首帧已保存: {os.path.basename(p)}")
            if self.on_extracted:
                self.on_extracted(p)

    def _extract_last(self):
        p = self._save_bgr(self.total_frames - 1, "last")
        if p:
            self.lbl_msg.config(text=f"末帧已保存: {os.path.basename(p)}")
            if self.on_extracted:
                self.on_extracted(p)

    def _use_last_as_first(self):
        self._extract_last()

    def _open_frames_dir(self):
        os.startfile(frames_dir())

    def _open_dialog(self):
        path = filedialog.askopenfilename(title="打开视频",
                                          filetypes=[("视频", "*.mp4 *.avi *.mov *.mkv *.webm")])
        if path:
            self.open_video(path)
