# -*- coding: utf-8 -*-
r"""右侧素材/帧库面板：上方切换「帧库 / 素材库」。

帧库：extracted_frames\ 下提取的帧图片。
素材库：materials\ 下用户放入的图片与音频（图片预览缩略图、音频图标+名称）。

交互：
  双击图片 -> on_pick(path)（填入引用或首帧）
  右键图片 -> 菜单：作为首帧 / 作为尾帧 / 插入引用
  双击音频 -> on_pick_audio(path)（填入参考音频槽）
支持鼠标滚轮。
"""

import os
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
AUDIO_EXT = (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac")


def _mousewheel_bind(widget):
    widget.bind("<MouseWheel>", lambda e: _wheel_scroll(widget, e))
    widget.bind("<Button-4>", lambda e: _wheel_scroll(widget, None, up=True))
    widget.bind("<Button-5>", lambda e: _wheel_scroll(widget, None, up=False))


def _wheel_scroll(canvas, event=None, up=None):
    try:
        delta = -event.delta if event else (120 if up else -120)
    except Exception:
        delta = 0
    # 只有内容超出可视区时才允许滚动
    try:
        bbox = canvas.bbox("all")
        view = canvas.winfo_height()
        if bbox and bbox[3] - bbox[1] <= view:
            return
    except Exception:
        pass
    canvas.yview_scroll(int(delta / 120), "units")


class _GridPanel(ttk.Frame):
    """通用缩略图网格容器（含滚轮与滚动条）"""

    THUMB = 150
    PAD = 6

    def __init__(self, master, canvas_width_cb=None, **kwargs):
        super().__init__(master, **kwargs)
        self.canvas_width_cb = canvas_width_cb
        self._photos = {}
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg="#f0f0f0", highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        # 不强制 inner 宽度：内容宽时自动出水平滚动条；窄时仅垂直占位
        self._mousewheel_bind(self.canvas)
        self._mousewheel_bind(self.inner)
        self._mousewheel_bind(wrap)

    def _mousewheel_bind(self, widget):
        widget.bind("<MouseWheel>", lambda e: _wheel_scroll(self.canvas, e))
        widget.bind("<Shift-MouseWheel>", lambda e: self.canvas.xview_scroll(
            int(-e.delta / 120), "units"))

    def clear(self):
        for c in self.inner.winfo_children():
            c.destroy()
        self._photos.clear()


class FrameLibraryPanel(ttk.Frame):
    def __init__(self, master, lib_dir, material_dir,
                 on_pick=None, on_first=None, on_last=None, on_pick_audio=None, **kwargs):
        super().__init__(master, **kwargs)
        self.lib_dir = lib_dir
        self.material_dir = material_dir
        self.on_pick = on_pick
        self.on_first = on_first
        self.on_last = on_last
        self.on_pick_audio = on_pick_audio
        self._right_click_path = None
        self._dl_after = None
        self._rf_after = None
        self._build()

    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill="x", pady=(2, 2))

        ttk.Button(header, text="刷新", width=6, command=self.refresh).pack(side="right", padx=4)
        ttk.Button(header, text="打开目录", width=8, command=self._open_dir).pack(side="right", padx=2)
        self.lbl_hint = ttk.Label(header, text="双击图片=填入引用 | 右键=更多",
                                  foreground="#888")
        self.lbl_hint.pack(side="right", padx=8)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=(0, 4), pady=(0, 4))

        self.frame_tab = _GridPanel(self.notebook)
        self.material_tab = _GridPanel(self.notebook)
        self.notebook.add(self.frame_tab, text="帧库")
        self.notebook.add(self.material_tab, text="素材库")

        # 右键菜单
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="作为首帧", command=self._m_first)
        self.menu.add_command(label="作为尾帧", command=self._m_last)
        self.menu.add_command(label="插入引用", command=self._m_pick)

        self.refresh()
        # 布局就绪后重排一次（初始 notebook 宽度未知，列数不准导致图片被截断）
        self.after(250, self.refresh)
        self.after(1000, self.refresh)

        # 窗口大小变化时动态重排（列数变化才重建，防频繁闪烁）
        self._last_cols = None
        self.bind("<Configure>", self._dynamic_relayout)
        # 面板变为可见时自动刷新（切到本 Tab / 提取帧后回来）
        self.bind("<Map>", lambda e: self._schedule_refresh())
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._schedule_refresh())

    def _schedule_refresh(self, delay=150):
        if hasattr(self, "_rf_after") and self._rf_after:
            try:
                self.after_cancel(self._rf_after)
            except Exception:
                pass
        self._rf_after = self.after(delay, self.refresh)

    def _dynamic_relayout(self, event=None):
        if not hasattr(self, "_last_cols"):
            self._last_cols = None
        cols = self._base_cols()
        if cols != self._last_cols:
            self._last_cols = cols
            # 轻微防抖：避免拖动窗口时反复重排
            if hasattr(self, "_dl_after") and self._dl_after:
                try:
                    self.after_cancel(self._dl_after)
                except Exception:
                    pass
            self._dl_after = self.after(180, self.refresh)

    def _open_dir(self):
        d = self.material_dir if self.notebook.index("current") == 1 else self.lib_dir
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def refresh(self):
        self.frame_tab.clear()
        self.material_tab.clear()
        self._refresh_frames()
        self._refresh_materials()

    def _base_cols(self):
        """列数基准：优先用 notebook 宽度，布局未就绪时用缺省的 3 列"""
        try:
            w = self.notebook.winfo_width()
            if w and w > 50:
                n = w // (self.frame_tab.THUMB + self.frame_tab.PAD * 2 + 12)
                return max(1, n)
        except Exception:
            pass
        return 3

    # ---------- 帧库 ----------
    def _refresh_frames(self):
        panel = self.frame_tab
        os.makedirs(self.lib_dir, exist_ok=True)
        files = sorted([f for f in os.listdir(self.lib_dir)
                        if f.lower().endswith(IMAGE_EXT)])
        if not files:
            ttk.Label(panel.inner, text="帧库为空。\n在[任务列表]打开视频→提取首帧/当前帧/末帧后，\n图片会出现在这里。",
                      foreground="#999").pack(padx=20, pady=30)
            return
        cols = self._base_cols()
        for i, f in enumerate(files):
            path = os.path.join(self.lib_dir, f)
            self._add_image_cell(panel, i, cols, path, f)

    # ---------- 素材库 ----------
    def _refresh_materials(self):
        panel = self.material_tab
        os.makedirs(self.material_dir, exist_ok=True)
        files = sorted([f for f in os.listdir(self.material_dir)
                        if f.lower().endswith(IMAGE_EXT + AUDIO_EXT)])
        if not files:
            ttk.Label(panel.inner,
                      text="素材库为空。\n把图片/音频文件放进去，或点右上「打开目录」添加。",
                      foreground="#999").pack(padx=20, pady=30)
            return
        cols = self._base_cols()
        for i, f in enumerate(files):
            path = os.path.join(self.material_dir, f)
            if f.lower().endswith(IMAGE_EXT):
                self._add_image_cell(panel, i, cols, path, f, material=True)
            else:
                self._add_audio_cell(panel, i, cols, path, f)

    # ---------- cell 构建 ----------
    def _panel_width(self, panel):
        try:
            return max(panel.canvas.winfo_width(), 300)
        except Exception:
            return 400

    def _add_image_cell(self, panel, i, cols, path, name, material=False):
        cell = ttk.Frame(panel.inner)
        cell.grid(row=i // cols, column=i % cols, padx=panel.PAD, pady=panel.PAD)
        try:
            img = Image.open(path)
            img.thumbnail((panel.THUMB, panel.THUMB))
            photo = ImageTk.PhotoImage(img)
        except Exception:
            photo = None
        if photo:
            panel._photos[path] = photo
            lbl = tk.Label(cell, image=photo, borderwidth=1, relief="solid", bg="#ddd")
        else:
            lbl = tk.Label(cell, text="(损坏)", width=12, height=6, bg="#eee")
        lbl.pack()
        ttk.Label(cell, text=name, foreground="#555", width=17).pack()
        lbl.bind("<Double-1>", lambda e, p=path: self._pick(p))
        lbl.bind("<Button-3>", lambda e, p=path: self._right_click(e, p))

    def _add_audio_cell(self, panel, i, cols, path, name):
        cell = ttk.Frame(panel.inner)
        cell.grid(row=i // cols, column=i % cols, padx=panel.PAD, pady=panel.PAD)
        # 与图片格同尺寸：固定宽高一致的容器避免一行一个
        lbl = tk.Label(cell, text="🎵", font=("Segoe UI Emoji", 22), width=10, height=4,
                       bg="#eee", relief="solid", bd=1)
        lbl.pack()
        ttk.Label(cell, text=name, foreground="#555", width=17).pack()
        lbl.bind("<Double-1>", lambda e, p=path: self._pick_audio(p))
        # 右键：播放
        m = tk.Menu(cell, tearoff=0)
        m.add_command(label="播放", command=lambda p=path: os.startfile(p))
        lbl.bind("<Button-3>", lambda e: m.tk_popup(e.x_root, e.y_root))

    # ---------- 事件 ----------
    def _pick(self, path):
        if self.on_pick:
            self.on_pick(path)

    def _pick_audio(self, path):
        if self.on_pick_audio:
            self.on_pick_audio(path)

    def _right_click(self, event, path):
        self._right_click_path = path
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _m_first(self):
        if self.on_first and self._right_click_path:
            self.on_first(self._right_click_path)

    def _m_last(self):
        if self.on_last and self._right_click_path:
            self.on_last(self._right_click_path)

    def _m_pick(self):
        if self.on_pick and self._right_click_path:
            self.on_pick(self._right_click_path)
