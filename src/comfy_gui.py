# -*- coding: utf-8 -*-
"""AutoDL ComfyUI 视频生成器 - GUI 主程序"""

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk

import api_client
from workflows import WORKFLOWS, WORKFLOW_BY_ID, SEED_MIN, SEED_MAX
import ffmpeg_utils
from video_player import VideoPlayerFrame, set_app_dir as vp_set_dir
from frame_library import FrameLibraryPanel

# 数据目录：始终相对于用户双击启动的所在目录（exe/脚本所在处），而非 _MEIPASS
APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0])) if hasattr(sys, "argv") and sys.argv and sys.argv[0] else os.getcwd()

# ffmpeg/ffprobe 路径准备（vendor 优先）
ffmpeg_utils.set_app_dir(lambda: APP_DIR)
ffmpeg_utils.setup()
vp_set_dir(APP_DIR)

CONFIG_FILE = os.path.join(APP_DIR, "config.json")
REGISTRY_FILE = os.path.join(APP_DIR, "tasks.json")
DEFAULT_DOWNLOAD_DIR = os.path.join(APP_DIR, "outputs")

STATUS_CN = {
    "QUEUED": "排队中",
    "RUNNING": "生成中",
    "SUCCESS": "已完成",
    "FAILED": "失败",
}

WF_NAME_TO_SHORT = {w["name"]: w.get("short_name") or w["id"] for w in WORKFLOWS}


def _workflow_short_name(wf_name):
    return WF_NAME_TO_SHORT.get(wf_name, wf_name[:10] or "video")


def read_text_file(path):
    """读取 UTF-8 JSON/文本；若系统为 GBK 且文件以 GBK 保存，自动兜底解码。"""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "gbk", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            cfg = json.loads(read_text_file(CONFIG_FILE))
            if not os.path.isabs(cfg.get("download_dir", "")):
                cfg["download_dir"] = os.path.join(APP_DIR, cfg.get("download_dir", "outputs"))
            return cfg
        except Exception:
            pass
    return {"base_url": "https://autodl.art", "api_key": "", "poll_interval": 5, "download_dir": DEFAULT_DOWNLOAD_DIR}


def save_config(cfg):
    """原子写：写临时文件后替换，防止写入中断损坏 config.json"""
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


def load_registry():
    if os.path.exists(REGISTRY_FILE):
        try:
            return json.loads(read_text_file(REGISTRY_FILE))
        except Exception:
            # 文件损坏：备份后从空注册表开始，避免静默丢失全部任务记录
            try:
                os.replace(REGISTRY_FILE, REGISTRY_FILE + ".corrupt")
            except OSError:
                pass
    return {}


_REG_LOCK = threading.Lock()
_DELETED_TIDS = set()  # 本会话明确删除的 key（跨进程合并时防已删记录复活）


def save_registry(reg):
    """线程安全 + 原子写 + 跨进程合并：
    1) 写入前先把 tasks.json 里本会话未知的新记录（如 CLI manager.py 提交的）并入 reg，
       避免基于旧快照的写入覆盖掉其他进程刚创建的任务记录；
    2) 序列化重试（防迭代中修改）+ 临时文件原子替换（防写坏文件）。"""
    with _REG_LOCK:
        try:
            if os.path.exists(REGISTRY_FILE):
                latest = json.loads(read_text_file(REGISTRY_FILE))
                if isinstance(latest, dict):
                    for k, v in latest.items():
                        if k not in reg and k not in _DELETED_TIDS:
                            reg[k] = v
        except Exception:
            pass
        data = None
        for _ in range(3):
            try:
                data = json.dumps(reg, ensure_ascii=False, indent=2)
                break
            except RuntimeError:
                time.sleep(0.05)
        if data is None:
            return
        tmp = REGISTRY_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, REGISTRY_FILE)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AutoDL ComfyUI 视频生成器")
        self.geometry("1180x780")
        self.minsize(1000, 680)

        self.cfg = load_config()
        self.registry = load_registry()
        # 启动即创建用户素材/帧库目录，避免用户不知道素材放哪里
        for d in ("materials", "extracted_frames", "outputs"):
            os.makedirs(os.path.join(APP_DIR, d), exist_ok=True)
        self.img_paths = []          # 当前表单选中的参考图路径
        self.audio_paths = []        # 当前表单选中的参考音频路径
        self.first_frame_path = ""   # 首尾帧：首帧
        self.last_frame_path = ""    # 首尾帧：尾帧
        self.thumb_refs = []         # 缩略图引用（防GC）
        self.msg_queue = queue.Queue()
        self._poll_busy = threading.Event()   # 轮询进行中标志（线程安全）

        # 关闭窗口时优雅退出：停缩略图线程 + 停播放器（音频/播放线程）
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self.var_workflow.set(WORKFLOWS[0]["name"])
        self._on_workflow_change()
        self.after(200, self._process_queue)
        self.after(3000, self._poll_tick)
        self._refresh_task_table()

        if not self.cfg.get("api_key"):
            self.after(300, self._show_welcome)

    # ---------- UI 构建 ----------
    def _on_close(self):
        """关闭窗口：停后台缩略图线程 + 释放播放器（音频/播放线程），再销毁"""
        try:
            self._thumb_stop = True
        except Exception:
            pass
        try:
            self.vplayer._teardown()
        except Exception:
            pass
        self.destroy()
    def _build_ui(self):
        # root 用 grid：顶部/中部/底部状态栏 三行固定
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # 顶部：API Key
        top = ttk.LabelFrame(self, text="API 配置")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        ttk.Label(top, text="Authorization Token:").pack(side="left", padx=(8, 4))
        self.var_key = tk.StringVar(value=self.cfg.get("api_key", ""))
        ent_key = ttk.Entry(top, textvariable=self.var_key, show="*", width=60)
        ent_key.pack(side="left", padx=4)
        ttk.Button(top, text="保存配置", command=self._save_key).pack(side="left", padx=4)
        ttk.Label(top, text="（在 autodl.art > 令牌管理创建，分组选 ComfyUI）").pack(side="left", padx=4)

        # 中部：左表单 / 右任务
        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)

        self.nb = ttk.Notebook(body)
        self.nb.pack(fill="both", expand=True)

        self.tab_form = ttk.Frame(self.nb)
        self.tab_tasks = ttk.Frame(self.nb)
        self.tab_plan = ttk.Frame(self.nb)
        self.nb.add(self.tab_form, text="创建任务")
        self.nb.add(self.tab_tasks, text="任务列表")
        self.nb.add(self.tab_plan, text="任务计划")

        self._build_form_tab()
        self._build_task_tab()
        self._build_plan_tab()

        # 切换到任务列表/任务计划页时自动刷新
        self.nb.bind("<<NotebookTabChanged>>", self._on_nb_tab_changed)

        # 底部状态栏（grid 固定底部行，永远可见）
        self.status = ttk.Label(self, text="就绪", anchor="w", relief="sunken", padding=(8, 4))
        self.status.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))

    def _on_nb_tab_changed(self, event=None):
        """切 Tab 时刷新对应页面"""
        try:
            cur = self.nb.index("current")
            if cur == 1:
                self._refresh_task_table()
            elif cur == 2:
                self._refresh_plan_table()
        except Exception:
            pass

    def _build_form_tab(self):
        tab = self.tab_form
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        # 工作流选择
        row0 = ttk.Frame(tab)
        row0.grid(row=0, column=0, sticky="ew", padx=8, pady=6)
        ttk.Label(row0, text="生成方式:").pack(side="left")
        self.var_workflow = tk.StringVar()
        self.cmb_workflow = ttk.Combobox(row0, textvariable=self.var_workflow, state="readonly", width=42,
                                         values=[w["name"] for w in WORKFLOWS])
        self.cmb_workflow.pack(side="left", padx=6)
        self.cmb_workflow.bind("<<ComboboxSelected>>", lambda e: self._on_workflow_change())
        self.lbl_wf_id = ttk.Label(row0, text="", foreground="#666")
        self.lbl_wf_id.pack(side="left", padx=6)

        # 中部：左表单 + 右帧库（可拖分隔条）
        self.pane = ttk.PanedWindow(tab, orient="horizontal")
        self.pane.grid(row=1, column=0, sticky="nsew", padx=8)
        self.pane.columnconfigure(0, weight=1)
        self.pane.rowconfigure(0, weight=1)

        left_host = ttk.Frame(self.pane)
        self.pane.add(left_host, weight=3)
        right_host = ttk.Frame(self.pane)
        self.pane.add(right_host, weight=2)

        # ---------- 左侧：上=参数滚动区 下=prompt固定区 ----------
        left_host.columnconfigure(0, weight=1)
        left_host.rowconfigure(0, weight=3)   # 参数滚动区（可伸缩）
        left_host.rowconfigure(1, weight=1)   # prompt 固定区

        # 参数区（说明/时长/分辨率/seed/素材，滚动容器）
        self.form_canvas = tk.Canvas(left_host, highlightthickness=0)
        self.form_canvas.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        vsb = ttk.Scrollbar(left_host, orient="vertical", command=self.form_canvas.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.form_canvas.configure(yscrollcommand=vsb.set)
        self.form_content = ttk.Frame(self.form_canvas)
        self._win_id = self.form_canvas.create_window((0, 0), window=self.form_content, anchor="nw")
        self.form_content.bind("<Configure>", lambda e: self.form_canvas.configure(scrollregion=self.form_canvas.bbox("all")))
        self.form_canvas.bind("<Configure>", lambda e: self.form_canvas.itemconfig(self._win_id, width=e.width))
        # 参数区滚轮
        self.form_canvas.bind_all("<MouseWheel>", self._form_wheel)

        # prompt 固定区（带纵向滚动条，始终可见）
        prompt_host = ttk.Frame(left_host)
        prompt_host.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=(0, 2), pady=2)
        prompt_host.columnconfigure(0, weight=1)
        prompt_host.rowconfigure(0, weight=1)
        self.txt_prompt = tk.Text(prompt_host, width=70, height=12, wrap="word")
        pvsb = ttk.Scrollbar(prompt_host, orient="vertical", command=self.txt_prompt.yview)
        phsb = ttk.Scrollbar(prompt_host, orient="horizontal", command=self.txt_prompt.xview)
        self.txt_prompt.configure(yscrollcommand=pvsb.set, xscrollcommand=phsb.set)
        self.txt_prompt.grid(row=0, column=0, sticky="nsew")
        pvsb.grid(row=0, column=1, sticky="ns")
        phsb.grid(row=1, column=0, sticky="ew")
        ttk.Label(prompt_host, text="提示词 prompt 窗格:", foreground="#555").grid(row=2, column=0, sticky="w", pady=(2, 0))

        # 右侧：帧库/素材库面板
        self.lib_panel = FrameLibraryPanel(
            right_host,
            lib_dir=os.path.join(APP_DIR, "extracted_frames"),
            material_dir=os.path.join(APP_DIR, "materials"),
            on_pick=self._lib_pick,
            on_first=lambda p: self._lib_set_frame("first", p),
            on_last=lambda p: self._lib_set_frame("last", p),
            on_pick_audio=self._lib_pick_audio,
        )
        self.lib_panel.pack(fill="both", expand=True)

        # 提交按钮
        row_submit = ttk.Frame(tab)
        row_submit.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        self.btn_submit = ttk.Button(row_submit, text="提交生成任务", command=self._submit)
        self.btn_submit.pack(side="left", padx=4)
        ttk.Button(row_submit, text="加入计划", command=self._add_to_plan).pack(side="left", padx=4)
        ttk.Button(row_submit, text="清空表单", command=self._clear_form).pack(side="left", padx=4)
        ttk.Button(row_submit, text="导入 payload...", command=self._import_payload).pack(side="left", padx=4)
        ttk.Button(row_submit, text="导出 payload...", command=self._export_payload).pack(side="left", padx=4)
        self.var_autosave = tk.BooleanVar(value=True)
        ttk.Checkbutton(row_submit, text="提交任务或任务计划时自动保存 payload", variable=self.var_autosave).pack(side="left", padx=8)

    def _form_wheel(self, event):
        """滚轮：仅当指针位于参数滚动区内且内容溢出时才滚动"""
        try:
            w = event.widget
            inside = False
            cur = w
            while cur is not None:
                if cur is self.form_canvas:
                    inside = True
                    break
                try:
                    cur = cur.master
                except Exception:
                    break
            if not inside:
                return
            bbox = self.form_canvas.bbox("all")
            view = self.form_canvas.winfo_height()
            if bbox and bbox[3] - bbox[1] <= view:
                return
            self.form_canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass

    # ---- 帧库交互：填入表单 ----
    def _lib_pick(self, path):
        """双击/右键插入：填入当前工作流的下一个空引用槽"""
        wf = self.workflow
        if wf.get("frame_mode") == "first_last":
            self._lib_set_frame("first", path)
            return
        if wf.get("max_images", 0) > 0:
            idx = next((i for i, p in enumerate(self.img_paths) if not p), 0)
            if idx < wf["max_images"]:
                self.img_paths[idx] = path
                self._update_img_thumb(idx, path)
                self.status.config(text=f"帧库 → 已填入引用图片{idx}")
                return
        # 无引用槽或已满：兜底填入首帧
        self._lib_set_frame("first", path)

    def _lib_set_frame(self, which, path):
        wf = self.workflow
        if wf.get("frame_mode") != "first_last":
            messagebox.showinfo("提示", "当前工作流不是「首尾帧」模式。\n请切换到「H3 首尾帧生成视频」后再使用首/尾帧。")
            return
        if which == "first":
            self.first_frame_path = path
            self.var_ff.set(os.path.basename(path))
        else:
            self.last_frame_path = path
            self.var_lf.set(os.path.basename(path))

    def _lib_pick_audio(self, path):
        """素材库音频双击：填入当前工作流的参考音频空槽"""
        wf = self.workflow
        n_aud = wf.get("max_audios", 0)
        if n_aud == 0:
            messagebox.showinfo("提示", "当前工作流不支持参考音频。\n请切换到支持音频的工作流（多图多音频/对口型）。")
            return
        idx = next((i for i, p in enumerate(self.audio_paths) if not p), 0)
        if idx >= n_aud:
            messagebox.showinfo("提示", "音频槽位已满，请先清空一个槽位。")
            return
        self.audio_paths[idx] = path
        lbl = self._audio_labels.get(idx)
        if lbl:
            lbl.config(text=os.path.basename(path)[:14], foreground="#333")
        self.status.config(text=f"素材库 → 已填入参考音频{idx}")

    def _build_task_tab(self):
        tab = self.tab_tasks
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)   # 上半：任务表（可伸缩）
        tab.rowconfigure(1, weight=1)   # 下半：播放器

        cols = ("time", "status", "file", "gen_dur", "video_dur", "payload")
        style_ttk = ttk.Style(self)
        try:
            style_ttk.configure("Task.Treeview", rowheight=58)
        except Exception:
            pass
        self.tree = ttk.Treeview(tab, columns=cols, show="tree headings", height=6, style="Task.Treeview")
        self.tree.heading("#0", text="预览")
        self.tree.column("#0", width=110, minwidth=90, anchor="center", stretch=False)
        self.tree.heading("time", text="生成时间")
        self.tree.column("time", width=110, anchor="w", stretch=False)
        self.tree.heading("status", text="状态")
        self.tree.column("status", width=56, anchor="center", stretch=False)
        self.tree.heading("file", text="视频文件名")
        self.tree.column("file", width=260, anchor="w", stretch=True)
        self.tree.heading("gen_dur", text="生成耗时s")
        self.tree.column("gen_dur", width=70, anchor="center", stretch=False)
        self.tree.heading("video_dur", text="视频时长s")
        self.tree.column("video_dur", width=70, anchor="center", stretch=False)
        self.tree.heading("payload", text="payload存档")
        self.tree.column("payload", width=180, anchor="w", stretch=True)
        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=(6, 2))
        vsb.grid(row=0, column=1, sticky="ns", pady=(6, 2))
        self.tree.bind("<Double-1>", self._open_result)
        self.tree.bind("<<TreeviewSelect>>", self._on_task_selected)

        # 下半：播放器（全宽自适应）
        self.vplayer = VideoPlayerFrame(tab, on_extracted=self._on_frame_extracted)
        self.vplayer.grid(row=1, column=0, sticky="nsew", padx=6, pady=(2, 6))

        # 右键菜单
        self.tree_menu = tk.Menu(self, tearoff=0)
        self.tree_menu.add_command(label="打开结果文件夹", command=self._open_result_dir)
        self.tree_menu.add_command(label="在预览中打开", command=self._open_selected_in_preview)
        self.tree_menu.add_command(label="带声音播放（系统播放器）", command=self._play_selected)

        btn_bar = ttk.Frame(tab)
        btn_bar.grid(row=2, column=0, sticky="ew", padx=6, pady=2)
        ttk.Button(btn_bar, text="立即刷新", command=self._refresh_task_table).pack(side="left", padx=4)
        ttk.Button(btn_bar, text="打开下载目录", command=self._open_download_dir).pack(side="left", padx=4)
        ttk.Button(btn_bar, text="移除选中任务记录", command=self._delete_task).pack(side="right", padx=4)

    def _build_plan_tab(self):
        tab = self.tab_plan
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        # 计划设置行
        self.plan_cfg = ttk.LabelFrame(tab, text="计划设置")
        cfg = self.plan_cfg
        cfg.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        row = ttk.Frame(cfg)
        row.pack(fill="x", padx=6, pady=4)
        # 相对时间：在 [H] 小时 [M] 分 [S] 秒 后开始
        ttk.Label(row, text="在").pack(side="left")
        self.var_plan_hh = tk.StringVar(value="0")
        self.var_plan_mm = tk.StringVar(value="0")
        self.var_plan_ss = tk.StringVar(value="10")
        sp_hh = ttk.Spinbox(row, from_=0, to=999, textvariable=self.var_plan_hh, width=5,
                            command=self._plan_eta_refresh)
        sp_hh.pack(side="left", padx=1)
        ttk.Label(row, text="小时").pack(side="left")
        sp_mm = ttk.Spinbox(row, from_=0, to=59, textvariable=self.var_plan_mm, width=3,
                            format="%02.0f", wrap=True, command=self._plan_eta_refresh)
        sp_mm.pack(side="left", padx=1)
        ttk.Label(row, text="分").pack(side="left")
        sp_ss = ttk.Spinbox(row, from_=0, to=59, textvariable=self.var_plan_ss, width=3,
                            format="%02.0f", wrap=True, command=self._plan_eta_refresh)
        sp_ss.pack(side="left", padx=1)
        ttk.Label(row, text="秒后开始，每隔").pack(side="left")
        self.var_plan_interval = tk.StringVar(value="30")
        ttk.Spinbox(row, from_=1, to=9999, textvariable=self.var_plan_interval, width=5).pack(side="left", padx=1)
        ttk.Label(row, text="秒提交一个任务").pack(side="left")
        self.btn_plan_start = ttk.Button(row, text="▶ 定时启动", command=self._plan_start)
        self.btn_plan_start.pack(side="left", padx=6)
        self.btn_plan_now = ttk.Button(row, text="⏩ 立刻按间隔开始", command=self._plan_start_now)
        self.btn_plan_now.pack(side="left", padx=4)

        # 实时换算显示
        row2 = ttk.Frame(cfg)
        row2.pack(fill="x", padx=6, pady=(0, 4))
        self.var_plan_eta = tk.StringVar(value="")
        ttk.Label(row2, textvariable=self.var_plan_eta, foreground="#0a7").pack(side="left")
        ttk.Label(row2, text="  |  失败自动重排队尾，累计≥5次中断",
                  foreground="#888").pack(side="left", padx=4)
        # 输入变化时也刷新换算
        for sp, var in ((sp_hh, self.var_plan_hh), (sp_mm, self.var_plan_mm), (sp_ss, self.var_plan_ss)):
            sp.bind("<KeyRelease>", lambda e: self._plan_eta_refresh())
            var.trace_add("write", lambda *a: self._plan_eta_refresh())
        self._plan_eta_refresh()

        # 剩余失败名额显示
        self.var_plan_state = tk.StringVar(value="计划未启动")
        ttk.Label(tab, textvariable=self.var_plan_state, foreground="#555").grid(row=2, column=0, sticky="w", padx=10, pady=(0, 2))

        # 计划队列表格
        cols = ("seq", "workflow", "payload", "status")
        self.plan_tree = ttk.Treeview(tab, columns=cols, show="headings", height=10)
        for c, text, width in [("seq", "顺序", 50), ("workflow", "工作流", 240),
                               ("payload", "payload文件", 260), ("status", "状态", 200)]:
            self.plan_tree.heading(c, text=text)
            self.plan_tree.column(c, width=width, anchor="w")
        self.plan_tree.grid(row=1, column=0, sticky="nsew", padx=8)
        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.plan_tree.yview)
        self.plan_tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=1, column=1, sticky="ns")

        btn_bar = ttk.Frame(tab)
        btn_bar.grid(row=3, column=0, sticky="ew", padx=8, pady=4)
        ttk.Button(btn_bar, text="上移", command=lambda: self._plan_move(-1)).pack(side="left", padx=2)
        ttk.Button(btn_bar, text="下移", command=lambda: self._plan_move(1)).pack(side="left", padx=2)
        ttk.Button(btn_bar, text="删除选中", command=self._plan_delete).pack(side="left", padx=2)
        ttk.Button(btn_bar, text="刷新", command=self._refresh_plan_table).pack(side="left", padx=2)
        self.btn_plan_pause = ttk.Button(btn_bar, text="暂停", command=self._plan_pause)
        self.btn_plan_pause.pack(side="left", padx=8)
        ttk.Button(btn_bar, text="🧹 清除全部", command=self._plan_clear_all).pack(side="right", padx=2)

        self._plan_running = False
        self._plan_fail_count = 0
        self._plan_submitted_count = 0
        self._plan_next_ts = 0.0
        self._plan_submitting = set()   # 正在异步提交中的 key（防重复提交）
        self._refresh_plan_table()
        # 启动后台秒级检查
        self.after(1000, self._plan_tick)

        # 缩略图缓存
        self._thumb_cache = {}   # task_id -> PhotoImage
        self._thumb_keys = {}    # iid -> task_id
        self._thumb_stop = False

    # ---------- 表单动态生成 ----------
    @property
    def workflow(self):
        wf_name = self.var_workflow.get()
        for w in WORKFLOWS:
            if w["name"] == wf_name:
                return w
        return WORKFLOWS[0]

    def _on_workflow_change(self):
        wf = self.workflow
        self.lbl_wf_id.config(text=wf["id"])
        for child in self.form_content.winfo_children():
            child.destroy()
        self.img_paths = [None] * wf["max_images"]
        self.audio_paths = [None] * wf["max_audios"]
        self.first_frame_path = ""
        self.last_frame_path = ""

        r = 0
        # 工作流说明（置顶，始终可见）
        desc_frame = ttk.Frame(self.form_content)
        desc_frame.grid(row=r, column=0, columnspan=3, sticky="ew", padx=8, pady=(2, 6))
        desc_frame.columnconfigure(0, weight=1)
        ttk.Label(desc_frame, text=wf["name"], font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(desc_frame, text=wf["id"], foreground="#888").grid(row=0, column=1, sticky="e")
        ttk.Label(desc_frame, text=wf["desc"], wraplength=620, foreground="#333").grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        r += 1

        # 时长
        dur_label = "音频时长" if wf.get("duration_field") == "audio_duration" else "时长"
        ttk.Label(self.form_content, text=f"{dur_label}（1-{wf['duration_max']}s）:").grid(row=r, column=0, sticky="w", padx=8, pady=4)
        default_dur = min(5, wf["duration_max"]) if wf["duration_max"] else 5
        self.var_duration = tk.StringVar(value=str(default_dur))
        ttk.Spinbox(self.form_content, from_=1, to=wf["duration_max"], textvariable=self.var_duration, width=8).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        # 分辨率
        ttk.Label(self.form_content, text="分辨率:").grid(row=r, column=0, sticky="w", padx=8, pady=4)
        self.var_resolution = tk.StringVar(value=wf["resolutions"][0])
        ttk.Combobox(self.form_content, textvariable=self.var_resolution, state="readonly",
                     values=wf["resolutions"], width=14).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        # seed
        if wf["has_seed"]:
            ttk.Label(self.form_content, text=f"seed（{SEED_MIN}-{SEED_MAX}，空则随机）:").grid(row=r, column=0, sticky="w", padx=8, pady=4)
            self.var_seed = tk.StringVar(value="")
            ttk.Entry(self.form_content, textvariable=self.var_seed, width=20).grid(row=r, column=1, sticky="w", padx=4)
            ttk.Button(self.form_content, text="随机", command=self._random_seed).grid(row=r, column=2, sticky="w", padx=4)
            r += 1

        # 首尾帧模式
        if wf.get("frame_mode") == "first_last":
            ttk.Label(self.form_content, text="首帧图片 (first_frame):").grid(row=r, column=0, sticky="w", padx=8, pady=4)
            self.var_ff = tk.StringVar(value="未选择")
            btn_row = ttk.Frame(self.form_content)
            btn_row.grid(row=r, column=1, sticky="w", padx=4)
            ttk.Button(btn_row, text="选择...", command=lambda: self._pick_frame("first")).pack(side="left")
            ttk.Button(btn_row, text="取消", width=5, command=lambda: self._clear_frame("first")).pack(side="left", padx=4)
            ttk.Label(self.form_content, textvariable=self.var_ff, foreground="#666").grid(row=r, column=2, sticky="w", padx=4)
            r += 1
            ttk.Label(self.form_content, text="尾帧图片 (last_frame):").grid(row=r, column=0, sticky="w", padx=8, pady=4)
            self.var_lf = tk.StringVar(value="未选择")
            btn_row = ttk.Frame(self.form_content)
            btn_row.grid(row=r, column=1, sticky="w", padx=4)
            ttk.Button(btn_row, text="选择...", command=lambda: self._pick_frame("last")).pack(side="left")
            ttk.Button(btn_row, text="取消", width=5, command=lambda: self._clear_frame("last")).pack(side="left", padx=4)
            ttk.Label(self.form_content, textvariable=self.var_lf, foreground="#666").grid(row=r, column=2, sticky="w", padx=4)
            r += 1
        else:
            # 参考图
            n_img = wf["max_images"]
            if n_img > 0:
                ttk.Label(self.form_content, text=f"参考图片 ref_image_0..{n_img-1}（{n_img}张）：").grid(row=r, column=0, sticky="w", padx=8, pady=4)
                r += 1
                img_frame = ttk.Frame(self.form_content)
                img_frame.grid(row=r, column=0, columnspan=3, sticky="w", padx=8, pady=4)
                self._build_img_upload_grid(img_frame, n_img)
                r += 1

            # 参考音频
            n_aud = wf["max_audios"]
            if n_aud > 0:
                ttk.Label(self.form_content, text=f"参考音频 ref_audio_0..{n_aud-1}（{n_aud}段）：").grid(row=r, column=0, sticky="w", padx=8, pady=4)
                r += 1
                aud_frame = ttk.Frame(self.form_content)
                aud_frame.grid(row=r, column=0, columnspan=3, sticky="w", padx=8, pady=4)
                self._build_audio_upload_grid(aud_frame, n_aud)
                r += 1

        self.form_content.columnconfigure(2, weight=1)

    def _build_img_upload_grid(self, frame, n):
        self.thumb_refs = []
        for i in range(n):
            cell = ttk.Frame(frame)
            cell.grid(row=i // 5, column=i % 5, padx=6, pady=6)
            cell.columnconfigure(0, weight=1)
            btn = ttk.Button(cell, text=f"图片{i}", width=6,
                             command=lambda idx=i: self._pick_image(idx))
            btn.grid(row=0, column=0)
            btn_clear = ttk.Button(cell, text="取消引用", width=8,
                                   command=lambda idx=i: self._clear_image(idx))
            btn_clear.grid(row=1, column=0, pady=1)
            # 固定 84x108 Canvas：缩略图/未选择 尺寸恒定，杜绝布局抖动
            cvs = tk.Canvas(cell, width=84, height=108, bg="#eee", highlightthickness=1,
                            highlightbackground="#ccc", bd=0)
            cvs.grid(row=2, column=0)
            cvs.create_text(42, 54, text="未选择", fill="#999", width=76)
            self._add_thumb_cell(i, cvs)

    def _clear_image(self, idx):
        if idx < len(self.img_paths) and self.img_paths[idx]:
            self.img_paths[idx] = None
            cvs = self._img_labels.get(idx)
            if cvs:
                cvs.delete("all")
                cvs.create_text(42, 54, text="未选择", fill="#999", width=76)
            self.status.config(text=f"已清除引用图片{idx}")

    def _add_thumb_cell(self, idx, cvs):
        self._img_labels = getattr(self, "_img_labels", {})
        self._img_labels[idx] = cvs
        self._img_thumbs = getattr(self, "_img_thumbs", {})
        self._img_thumbs[idx] = None

    def _build_audio_upload_grid(self, frame, n):
        for i in range(n):
            cell = ttk.Frame(frame)
            cell.grid(row=i // 5, column=i % 5, padx=6, pady=6)
            btn = ttk.Button(cell, text=f"音频{i}", width=6, command=lambda idx=i: self._pick_audio(idx))
            btn.pack()
            btn_clear = ttk.Button(cell, text="取消引用", width=8,
                                   command=lambda idx=i: self._clear_audio(idx))
            btn_clear.pack(pady=1)
            lbl = ttk.Label(cell, width=12, text="未选择", foreground="#999")
            lbl.pack(pady=2)
            self._audio_labels = getattr(self, "_audio_labels", {})
            self._audio_labels[i] = lbl

    def _clear_audio(self, idx):
        if idx < len(self.audio_paths) and self.audio_paths[idx]:
            self.audio_paths[idx] = None
            lbl = self._audio_labels.get(idx)
            if lbl:
                lbl.config(text="未选择", foreground="#999")
            self.status.config(text=f"已清除引用音频{idx}")

    # ---------- 文件选择 ----------
    def _pick_image(self, idx):
        path = filedialog.askopenfilename(
            title=f"选择第{idx}张参考图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp"), ("所有文件", "*.*")])
        if not path:
            return
        self.img_paths[idx] = path
        self._update_img_thumb(idx, path)

    def _update_img_thumb(self, idx, path):
        cvs = self._img_labels.get(idx)
        if not cvs:
            return
        try:
            img = Image.open(path)
            img.thumbnail((80, 104))
            photo = ImageTk.PhotoImage(img)
            self._img_thumbs[idx] = photo
            cvs.delete("all")
            cvs.create_image(42, 54, image=photo)
        except Exception:
            cvs.delete("all")
            cvs.create_text(42, 54, text=os.path.basename(path)[:8], fill="#999", width=76)

    def _pick_audio(self, idx):
        path = filedialog.askopenfilename(
            title=f"选择第{idx}段参考音频",
            filetypes=[("音频", "*.wav *.mp3 *.flac *.ogg *.m4a *.aac"), ("所有文件", "*.*")])
        if not path:
            return
        self.audio_paths[idx] = path
        lbl = self._audio_labels.get(idx)
        if lbl:
            lbl.config(text=os.path.basename(path)[:14], foreground="#333")

    def _pick_frame(self, which):
        path = filedialog.askopenfilename(
            title=("选择首帧图片" if which == "first" else "选择尾帧图片"),
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp"), ("所有文件", "*.*")])
        if not path:
            return
        if which == "first":
            self.first_frame_path = path
            self.var_ff.set(os.path.basename(path))
        else:
            self.last_frame_path = path
            self.var_lf.set(os.path.basename(path))

    def _random_seed(self):
        import random as _r
        self.var_seed.set(str(_r.randint(SEED_MIN, SEED_MAX)))

    def _clear_frame(self, which):
        if which == "first":
            self.first_frame_path = ""
            self.var_ff.set("未选择")
        else:
            self.last_frame_path = ""
            self.var_lf.set("未选择")

    # ---------- 提交 ----------
    def _collect_form(self):
        wf = self.workflow
        try:
            duration = int(self.var_duration.get())
        except ValueError:
            raise ValueError("时长必须是整数")
        if duration < 1 or duration > wf["duration_max"]:
            raise ValueError(f"时长需在 1-{wf['duration_max']}s 之间")

        seed = None
        if wf["has_seed"]:
            txt = self.var_seed.get().strip()
            if txt:
                try:
                    seed = int(txt)
                except ValueError:
                    raise ValueError("seed 必须是整数")
                if seed < SEED_MIN or seed > SEED_MAX:
                    raise ValueError(f"seed 需在 {SEED_MIN}-{SEED_MAX} 之间")

        prompt = ""
        if wf["has_prompt"] and self.txt_prompt is not None:
            prompt = self.txt_prompt.get("1.0", "end").strip()

        if wf.get("frame_mode") == "first_last":
            if not self.first_frame_path and not self.last_frame_path:
                raise ValueError("请至少选择一张首帧或尾帧图片")
            for lbl, p in (("首帧", self.first_frame_path), ("尾帧", self.last_frame_path)):
                if p and not os.path.exists(p):
                    raise ValueError(f"{lbl}图片不存在: {p}")
            if wf["has_prompt"] and not prompt:
                raise ValueError("请填写提示词 prompt")
        else:
            n_img = wf["max_images"]
            if n_img > 0:
                for i, p in enumerate(self.img_paths[:n_img]):
                    if p and not os.path.exists(p):
                        raise ValueError(f"图片{i} 不存在: {p}")
                if wf["has_prompt"] and not prompt:
                    raise ValueError("请填写提示词 prompt")

        if wf["has_prompt"] and not prompt:
            raise ValueError("请填写提示词 prompt")

        return duration, seed, prompt

    def _submit(self):
        wf = self.workflow
        api_key = self.var_key.get().strip()
        if not api_key and not self.cfg.get("api_key"):
            messagebox.showerror("错误", "请先填写 API Token 并保存配置")
            return
        api_key = api_key or self.cfg["api_key"]
        try:
            duration, seed, prompt = self._collect_form()
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return

        # 快照表单素材（后台线程只读，避免用户继续编辑表单影响本次提交）
        images = list(self.img_paths)
        audios = list(self.audio_paths)
        first_f = self.first_frame_path
        last_f = self.last_frame_path
        resolution = self.var_resolution.get()

        self.status.config(text="提交中...")
        self.btn_submit.config(state="disabled")

        auto_save = self.var_autosave.get()
        auto_payload = self._build_archive_payload(duration, seed, prompt) if auto_save else None
        wf_short = wf.get("short_name") or wf["id"]  # 快照（后台线程不可读 tkinter）

        def work():
            try:
                # base64 大文件转换也放后台，避免大素材卡 UI
                payload = api_client.build_payload(
                    workflow=wf, prompt=prompt, duration=duration, resolution=resolution,
                    seed=seed, images=images, audios=audios,
                    first_frame=first_f, last_frame=last_f)
                tid = api_client.submit_task(api_key, wf["id"], payload)
                rec = {
                    "payload_file": json.dumps({k: (v[:40] + "..." if isinstance(v, str) and len(v) > 40 else v)
                                                for k, v in payload.items()}, ensure_ascii=False),
                    "workflow": wf["name"],
                    "seed": payload.get("seed"),
                    "video_duration": payload.get("duration") or payload.get("audio_duration") or "-",
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "QUEUED",
                    "duration": 0,
                    "results": [],
                    "files": [],
                }
                if auto_payload is not None:
                    try:
                        saved = self._save_autosave_payload(auto_payload, wf_short)
                        rec["autosave_file"] = os.path.basename(saved)
                        self.registry[tid] = rec
                        save_registry(self.registry)
                        self.msg_queue.put(("ok", f"提交成功，任务ID: {tid}\n已自动保存 payload: {saved}"))
                    except Exception as e:
                        rec["autosave_file"] = None
                        self.registry[tid] = rec
                        save_registry(self.registry)
                        self.msg_queue.put(("ok", f"提交成功，任务ID: {tid}\n（自动保存 payload 失败: {e}）"))
                else:
                    rec["autosave_file"] = None
                    self.registry[tid] = rec
                    save_registry(self.registry)
                    self.msg_queue.put(("ok", f"提交成功，任务ID: {tid}"))
            except Exception as e:
                self.msg_queue.put(("err", f"提交失败: {e}"))

        threading.Thread(target=work, daemon=True).start()

    # ---------- 计划任务 ----------
    def _plan_items(self):
        """返回计划队列（PLAN_ 前缀），按 seq 排序。
        list() 快照迭代：后台线程可能并发增删 registry key，直接迭代会抛 RuntimeError。"""
        items = []
        for k, rec in list(self.registry.items()):
            if k.startswith("PLAN_") and rec.get("type") == "plan":
                items.append((k, rec))
        items.sort(key=lambda x: x[1].get("seq", 0))
        return items

    def _add_to_plan(self):
        """把当前表单任务加入计划队列（payload 落盘 autosave）"""
        wf = self.workflow
        try:
            duration, seed, prompt = self._collect_form()
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return
        # payload 落盘（计划任务引用文件，提交时再组装 base64）
        arc = self._build_archive_payload(duration, seed, prompt)
        try:
            saved = self._save_autosave_payload(arc)
        except Exception as e:
            messagebox.showerror("错误", f"保存 payload 失败: {e}")
            return
        seqs = [r.get("seq", 0) for _, r in self._plan_items()]
        seq = max(seqs) + 1 if seqs else 1
        # key 追加 seq + 计数器保证唯一（同毫秒连续加入不会互相覆盖）
        pid = "PLAN_" + time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time()*1000)%1000:03d}_{seq}"
        self.registry[pid] = {
            "type": "plan",
            "seq": seq,
            "workflow": wf["name"],
            "payload_file": os.path.basename(saved),
            "status": "PLANNED",
            "fail_count": 0,
            "plan_ts_display": "",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_registry(self.registry)
        self._refresh_plan_table()
        # 明显提示（状态栏 + 标签），不弹框不跳转
        total = len([1 for _, r in self._plan_items() if r.get("status") != "DONE"])
        self.status.config(text=f"✅ 已加入计划（第{seq}位）| 当前计划任务共 {total} 个: {os.path.basename(saved)}")

    def _refresh_plan_table(self):
        # 保留当前选中（计划 tick 每秒刷新不打断用户操作）
        sel = self.plan_tree.selection()
        sel_key = sel[0] if sel else None
        self.plan_tree.delete(*self.plan_tree.get_children())
        items = self._plan_items()
        for k, rec in items:
            self.plan_tree.insert("", "end", iid=k, values=(
                rec.get("seq", 0), rec.get("workflow", ""), rec.get("payload_file", ""),
                self._plan_status_text(rec)))
        if sel_key and self.plan_tree.exists(sel_key):
            self.plan_tree.selection_set(sel_key)
        # 标题实时显示待提交数目
        pending = sum(1 for _, r in items if r.get("status") != "DONE")
        done = len(items) - pending
        try:
            self.plan_cfg.config(text=f"计划设置    当前计划任务：{pending} 个待提交" +
                                 (f"（已完成 {done} 个）" if done else ""))
        except Exception:
            pass

    def _plan_status_text(self, rec):
        if rec.get("status") == "DONE":
            return f"已提交成功（{rec.get('tid', '')[:8]}）"
        if rec.get("fail_count", 0) > 0:
            return f"提交失败{rec.get('fail_count')}次，将重试（重排队尾）"
        if rec.get("plan_ts_display"):
            return f"计划在 {rec['plan_ts_display']} 提交"
        return "计划中"

    def _plan_eta_refresh(self):
        """实时换算：显示相对时间对应的绝对时刻"""
        try:
            h = int(float(self.var_plan_hh.get() or 0))
            m = int(float(self.var_plan_mm.get() or 0))
            s = int(float(self.var_plan_ss.get() or 0))
            eta = time.time() + h * 3600 + m * 60 + s
            txt = time.strftime("预计开始时间：%Y-%m-%d %H:%M:%S", time.localtime(eta))
        except Exception:
            txt = ""
        self.var_plan_eta.set(txt)

    def _plan_start(self, start_ts=None):
        """定时启动：按相对时间（H/M/S 后）。运行中再次点击 = 用新时间重新调度"""
        if start_ts is None:
            try:
                h = int(float(self.var_plan_hh.get() or 0))
                m = int(float(self.var_plan_mm.get() or 0))
                s = int(float(self.var_plan_ss.get() or 0))
            except ValueError:
                messagebox.showerror("错误", "时间不合法")
                return
            start_ts = time.time() + h * 3600 + m * 60 + s
        self._plan_launch(start_ts)

    def _plan_start_now(self):
        """立刻按间隔开始提交。运行中再次点击 = 从现在重新按间隔调度"""
        self._plan_launch(time.time())

    def _plan_pause(self):
        """暂停/继续"""
        if self._plan_running:
            self._plan_running = False
            self.btn_plan_pause.config(text="继续")
            self.var_plan_state.set("计划已暂停（点“继续”恢复，或点启动按钮重新调度）")
        else:
            self._plan_running = True
            self._plan_next_ts = max(time.time(), self._plan_next_ts)
            self.btn_plan_pause.config(text="暂停")
            self.var_plan_state.set("计划已继续")
            # 恢复后重算各任务预计提交时刻（原显示已过期）
            self._recalc_plan_eta()

    def _recalc_plan_eta(self):
        """按当前 next_ts + 间隔重算所有未完成计划的预计提交时刻"""
        if not getattr(self, "_plan_interval", 0):
            return
        idx = 0
        for k, rec in self._plan_items():
            if rec.get("status") == "DONE":
                continue
            eta = self._plan_next_ts + idx * self._plan_interval
            rec["plan_ts_display"] = time.strftime("%H:%M:%S", time.localtime(eta))
            idx += 1
        save_registry(self.registry)

    def _plan_clear_all(self):
        """一键清除全部计划任务"""
        items = self._plan_items()
        if not items:
            messagebox.showinfo("提示", "计划队列为空")
            return
        n = len(items)
        if not messagebox.askyesno("确认", f"清除全部 {n} 个计划任务？\n（已提交的任务不受影响）"):
            return
        self._plan_running = False
        self.btn_plan_start.config(text="▶ 定时启动")
        self.btn_plan_pause.config(text="暂停")
        self._delete_plan_records([k for k, _ in items])
        save_registry(self.registry)
        self._refresh_plan_table()
        self.var_plan_state.set(f"已清除 {n} 个计划任务")
        self.status.config(text=f"🧹 已清除全部 {n} 个计划任务")

    def _plan_launch(self, start_ts):
        items = [i for i in self._plan_items() if i[1].get("status") != "DONE"]
        if not items:
            messagebox.showinfo("提示", "计划队列为空")
            return
        try:
            interval = int(float(self.var_plan_interval.get()))
            if interval < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "间隔秒数不合法")
            return
        self._plan_running = True
        self._plan_fail_count = 0
        self._plan_submitted_count = 0
        self._plan_interval = interval
        self._plan_start_ts = start_ts
        self._plan_next_ts = start_ts
        # 记录剩余失败额度
        self._plan_fail_limit = 5
        # 为每个未完成任务推算预计提交时刻（依次间隔）
        idx = 0
        for k, rec in self._plan_items():
            if rec.get("status") == "DONE":
                continue
            eta = start_ts + idx * interval
            rec["plan_ts_display"] = time.strftime("%H:%M:%S", time.localtime(eta))
            idx += 1
        save_registry(self.registry)
        self.btn_plan_start.config(text="▶ 定时启动")
        self.btn_plan_pause.config(text="暂停")
        self.var_plan_state.set(f"计划运行中 | 将于 {time.strftime('%H:%M:%S', time.localtime(start_ts))} 开始 | 间隔 {interval}s | 失败额度 {self._plan_fail_limit}")

    def _plan_tick(self):
        try:
            try:
                if self._plan_running:
                    now = time.time()
                    if now >= self._plan_next_ts:
                        pending = [i for i in self._plan_items()
                                   if i[1].get("status") != "DONE"]
                        if not pending:
                            self._plan_running = False
                            self.btn_plan_start.config(text="▶ 定时启动")
                            self.btn_plan_pause.config(text="暂停")
                            self.var_plan_state.set("全部计划任务已提交")
                        else:
                            key, rec = pending[0]
                            if key not in self._plan_submitting:
                                self._plan_submitting.add(key)
                                self._plan_submit_one(key)
                            self._plan_next_ts = now + self._plan_interval
            except Exception as e:
                self.var_plan_state.set(f"计划调度异常: {e}")
            self._refresh_plan_table()
        finally:
            # finally 保证异常（如 registry 并发修改）不会杀死调度循环
            self.after(1000, self._plan_tick)

    def _plan_submit_one(self, key):
        rec = self.registry.get(key)
        if not rec:
            return
        api_key = self.var_key.get().strip() or self.cfg.get("api_key", "")

        def work():
            try:
                import api_client as ac
                # 从 payload 文件重组（素材为路径/base64，由 load 处理）
                payload_file = os.path.join(APP_DIR, "payload_autosave", rec.get("payload_file", ""))
                payload = json.load(open(payload_file, encoding="utf-8"))
                if "__workflow_id" in payload:
                    payload = {k: v for k, v in payload.items() if k != "__workflow_id"}
                wfw = self.workflow_by_id(rec.get("workflow", ""))
                payload = ac.build_payload(
                    workflow=wfw,
                    prompt=payload.get("prompt", ""),
                    duration=payload.get("duration", 5),
                    resolution=payload.get("resolution", (wfw["resolutions"] or ["736p竖"])[0]),
                    seed=payload.get("seed"),
                    images=self._payload_refs(payload, "ref_image_", wfw),
                    audios=self._payload_refs(payload, "ref_audio_", wfw),
                    first_frame=self._payload_single(payload, "first_frame"),
                    last_frame=self._payload_single(payload, "last_frame"))
                tid = ac.submit_task(api_key, wfw["id"], payload)
                # 提交成功：PLAN 记录移除（由正式任务记录替代，任务列表只显示一条）
                new_rec = {
                    "payload_file": json.dumps({k: (str(v)[:40] + "..." if isinstance(v, str) and len(v) > 40 else v)
                                                for k, v in payload.items()}, ensure_ascii=False),
                    "workflow": rec.get("workflow", ""),
                    "seed": payload.get("seed"),
                    "video_duration": payload.get("duration") or payload.get("audio_duration") or "-",
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "QUEUED",
                    "duration": 0,
                    "results": [],
                    "files": [],
                    "autosave_file": rec.get("payload_file"),
                }
                self.registry[tid] = new_rec
                # 移除已完成使命的 PLAN 记录（key 即 plan key）
                self.registry.pop(key, None)
                _DELETED_TIDS.add(key)  # 防跨进程合并时复活已转正式任务的 PLAN 占位
                save_registry(self.registry)
                self._plan_submitted_count += 1
                self.msg_queue.put(("plan_status", f"已提交第{rec.get('seq')}个: {tid[:8]}"))
            except Exception as e:
                # 注意：本分支运行在后台线程，禁止直接调用任何 tkinter 控件
                rec["fail_count"] = rec.get("fail_count", 0) + 1
                self._plan_fail_count += 1
                # 重排队尾
                seqs = [r.get("seq", 0) for _, r in self._plan_items()]
                rec["seq"] = (max(seqs) + 1) if seqs else 1
                # 若达到失败上限：中断（只改数据，UI 由主线程消息刷新）
                if self._plan_fail_count >= self._plan_fail_limit:
                    self._plan_running = False
                    state_txt = (
                        f"⚠️ 计划中断：累计失败{self._plan_fail_count}次\n"
                        f"已完成{self._plan_submitted_count}个（已移入任务列表）\n"
                        f"剩余 {len(self._plan_items())} 个任务保留在计划队列")
                else:
                    state_txt = (
                        f"第{rec.get('seq')}个提交失败({rec.get('fail_count')}次)，重排队尾 | "
                        f"失败额度剩{self._plan_fail_limit - self._plan_fail_count}")
                save_registry(self.registry)
                self.msg_queue.put(("plan_status", f"{state_txt} | 详情: {e}"))
                self.msg_queue.put(("plan_sync", ""))
            finally:
                self._plan_submitting.discard(key)

        threading.Thread(target=work, daemon=True).start()

    def workflow_by_id(self, name):
        for w in WORKFLOWS:
            if w["name"] == name:
                return w
        return WORKFLOWS[0]

    def _payload_refs(self, payload, prefix, wfw):
        n = wfw.get("max_images", 0) if prefix.startswith("ref_image") else wfw.get("max_audios", 0)
        out = []
        for i in range(n):
            v = payload.get(f"{prefix}{i}")
            if v:
                out.append(v)
        return out

    def _payload_single(self, payload, key):
        v = payload.get(key)
        return v or None

    def _plan_move(self, delta):
        sel = self.plan_tree.selection()
        if not sel:
            return
        items = self._plan_items()
        # 找到选中 item 在列表位置
        idx = next((i for i, (k, r) in enumerate(items) if k == sel[0]), None)
        if idx is None:
            return
        ni = idx + delta
        if ni < 0 or ni >= len(items):
            return
        k1, r1 = items[idx]
        k2, r2 = items[ni]
        r1["seq"], r2["seq"] = r2["seq"], r1["seq"]
        save_registry(self.registry)
        self._refresh_plan_table()

    def _plan_delete(self):
        sel = self.plan_tree.selection()
        if not sel:
            return
        if messagebox.askyesno("确认", "从计划队列移除该任务？"):
            self._delete_plan_records([sel[0]])
            save_registry(self.registry)
            self._refresh_plan_table()

    def _delete_plan_records(self, keys):
        """删除 PLAN 记录 + 清理对应的 payload_autosave 孤儿文件（仅未提交的）"""
        for k in keys:
            rec = self.registry.pop(k, None)
            _DELETED_TIDS.add(k)  # 防跨进程合并时复活已删除的计划记录
            if not rec:
                continue
            af = rec.get("payload_file")
            if af:
                p = os.path.join(APP_DIR, "payload_autosave", af)
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass

    # ---------- 轮询 ----------
    def _poll_tick(self):
        api_key = self.var_key.get().strip() or self.cfg.get("api_key", "")
        if api_key:
            self._poll_once(api_key)
        self.after(5000, self._poll_tick)

    def _poll_once(self, api_key):
        # 上一轮查询线程未结束（网络慢）时跳过本轮，防止多线程并发查询/重复下载
        # Event 保证跨线程同步语义（主线程 set / 后台线程 clear）
        if self._poll_busy.is_set():
            return
        self._poll_busy.set()

        def work():
            try:
                changed = False
                for tid, rec in list(self.registry.items()):
                    try:
                        status = rec.get("status")
                        # 终态任务：SUCCESS 但未下载/下载失败 -> 重试下载；其余跳过
                        if status in ("SUCCESS", "FAILED"):
                            if status == "SUCCESS" and not rec.get("files") and rec.get("results"):
                                try:
                                    label = _workflow_short_name(rec.get("workflow", ""))
                                    files = api_client.download_results(
                                        tid, rec["results"],
                                        self.cfg.get("download_dir", DEFAULT_DOWNLOAD_DIR), label=label)
                                    rec["files"] = files
                                    save_registry(self.registry)
                                    changed = True
                                except Exception as e:
                                    print(f"  [任务 {tid[:8]}] 下载重试失败: {e}")
                            continue
                        data = api_client.query_task(api_key, tid)
                        if data["status"] != status or data.get("duration", 0) != rec.get("duration"):
                            changed = True
                        rec["status"] = data["status"]
                        rec["duration"] = data.get("duration", 0)
                        rec["results"] = data.get("results", [])
                        if data["status"] == "SUCCESS" and not rec.get("files"):
                            try:
                                label = _workflow_short_name(rec.get("workflow", ""))
                                files = api_client.download_results(
                                    tid, rec["results"],
                                    self.cfg.get("download_dir", DEFAULT_DOWNLOAD_DIR), label=label)
                                rec["files"] = files
                            except Exception as e:
                                print(f"  [任务 {tid[:8]}] 首次下载失败，将重试: {e}")
                        save_registry(self.registry)
                    except Exception as e:
                        print(f"  [任务 {tid[:8]}] 轮询异常(隔离): {e}")
                if changed:
                    self.msg_queue.put(("refresh", ""))
            finally:
                self._poll_busy.clear()
        threading.Thread(target=work, daemon=True).start()

    def _process_queue(self):
        try:
            while True:
                msg, payload = self.msg_queue.get_nowait()
                if msg == "ok":
                    self.status.config(text=payload)
                    self.btn_submit.config(state="normal")
                    self._refresh_task_table()
                    self.nb.select(self.tab_tasks)
                elif msg == "err":
                    self.status.config(text=payload)
                    self.btn_submit.config(state="normal")
                    messagebox.showerror("错误", payload)
                elif msg == "refresh":
                    self._refresh_task_table()
                elif msg == "plan_status":
                    self.var_plan_state.set(str(payload))
                    self._refresh_plan_table()
                elif msg == "plan_sync":
                    # 后台线程只改数据，表格刷新统一在主线程做
                    self._refresh_plan_table()
                elif msg == "thumb":
                    tid, img = payload
                    photo = ImageTk.PhotoImage(img)  # 主线程创建
                    self._thumb_cache[tid] = photo
                    # 找到该 tid 对应的行并更新缩略图
                    for iid, k in list(self._thumb_keys.items()):
                        if k == tid:
                            try:
                                self.tree.item(iid, image=photo)
                            except Exception:
                                pass
                            break
        except queue.Empty:
            pass
        except Exception as e:
            # 任何未预期异常不能杀死消息泵
            try:
                self.status.config(text=f"消息处理异常: {e}")
            except Exception:
                pass
        finally:
            self.after(200, self._process_queue)

    # ---------- 任务表格 ----------
    def _refresh_task_table(self, force=False):
        existing = set(self.tree.get_children())
        # 删除已清理的任务行（同步清缩略图缓存，防止删除任务后 PhotoImage 滞留内存）
        for iid in list(existing):
            if iid not in self.registry:
                self.tree.delete(iid)
                self._thumb_cache.pop(iid, None)
                self._thumb_keys.pop(iid, None)

        for tid, rec in sorted(list(self.registry.items()), key=lambda x: x[1].get("created_at", "")):
            status = rec.get("status", "")
            af = rec.get("autosave_file")
            payload_show = af if af else ("没有保存payload文件" if "autosave_file" in rec else "-")
            files = rec.get("files", [])
            file_name = os.path.basename(files[0]) if files else ("未下载/无文件" if status == "SUCCESS" else "")
            created = rec.get("created_at", "")[5:16]  # MM-DD HH:MM
            if rec.get("type") == "plan":
                if status == "DONE":
                    status_show = f"已提交({rec.get('tid', '')[:8]})"
                elif rec.get("fail_count", 0) > 0 and status == "FAILED":
                    status_show = "提交失败待重试"
                elif rec.get("plan_ts_display"):
                    status_show = f"计划在{rec['plan_ts_display']}提交"
                else:
                    status_show = "计划中"
                # 计划任务不显示文件/时长
                values = (created or "-", status_show, "", "", "-", rec.get("payload_file", ""))
            else:
                status_show = STATUS_CN.get(status, status)
                values = (created or "-", status_show, file_name,
                          rec.get("duration", 0), rec.get("video_duration", "-"), payload_show)
            if tid in existing:
                # 已有行：只更新值（避免闪动）
                self.tree.item(tid, values=values)
            else:
                iid = self.tree.insert("", "end", iid=tid, image=self._thumb_cache.get(tid) or "",
                                       values=values)
                self._thumb_keys[iid] = tid
        self._update_status_summary()
        self._load_thumbs_async()

    def _load_thumbs_async(self):
        """后台为有视频文件的任务生成第 2 帧缩略图（串行，仅缺失时）"""
        todo = []
        for tid, rec in list(self.registry.items()):
            files = [f for f in rec.get("files", []) if f.lower().endswith((".mp4", ".mov", ".mkv", ".avi", ".webm"))]
            if files and tid not in self._thumb_cache:
                todo.append((tid, files[0]))
        if not todo:
            return

        def work():
            import cv2
            for tid, path in todo:
                if self._thumb_stop:
                    return
                try:
                    cap = cv2.VideoCapture(path)
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 1)  # 第 2 帧
                    ok, frame = cap.read()
                    cap.release()
                    if not ok:
                        continue
                    h, w = frame.shape[:2]
                    scale = min(96 / w, 54 / h)
                    nh, nw = int(h * scale), int(w * scale)
                    if nh != h or nw != w:
                        frame = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    img = Image.fromarray(rgb)
                    self.msg_queue.put(("thumb", (tid, img)))
                except Exception:
                    continue

        threading.Thread(target=work, daemon=True).start()

    def _update_status_summary(self):
        running = sum(1 for r in list(self.registry.values()) if r.get("status") in ("QUEUED", "RUNNING"))
        success = sum(1 for r in list(self.registry.values()) if r.get("status") == "SUCCESS")
        self.status.config(text=f"共 {len(self.registry)} 个任务 | 生成中 {running} | 已完成 {success}")

    def _selected_files(self):
        item = self.tree.selection()
        if not item:
            return []
        rec = self.registry.get(item[0], {})
        return rec.get("files", [])

    def _on_task_selected(self, event):
        # 选中任务时自动在右侧预览加载该任务第一个视频
        self._open_selected_in_preview()

    def _open_selected_in_preview(self):
        files = [f for f in self._selected_files() if f.lower().endswith((".mp4", ".mov", ".mkv", ".avi", ".webm"))]
        if files:
            self.vplayer.open_video(files[0])

    def _play_selected(self):
        files = self._selected_files()
        if files:
            os.startfile(files[0])

    def _on_frame_extracted(self, frame_path):
        # 提取帧后自动填入当前工作流的第一个参考图槽位（首尾帧则填首帧）
        wf = self.workflow
        if wf.get("frame_mode") == "first_last":
            self.first_frame_path = frame_path
            self.var_ff.set(os.path.basename(frame_path))
            self.status.config(text=f"已填入首帧: {os.path.basename(frame_path)}")
        elif wf.get("max_images", 0) > 0:
            idx = next((i for i, p in enumerate(self.img_paths) if not p), 0)
            if idx < wf["max_images"]:
                self.img_paths[idx] = frame_path
                self._update_img_thumb(idx, frame_path)
                self.status.config(text=f"已填入参考图{idx}: {os.path.basename(frame_path)}")

    def _open_result(self, event):
        self._open_selected_in_preview()

    def _open_result_dir(self):
        item = self.tree.selection()
        if not item:
            return
        tid = item[0]
        rec = self.registry.get(tid, {})
        files = rec.get("files", [])
        if files:
            os.startfile(os.path.dirname(files[0]))

    def _open_download_dir(self):
        d = self.cfg.get("download_dir", DEFAULT_DOWNLOAD_DIR)
        if not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def _delete_task(self):
        item = self.tree.selection()
        if not item:
            return
        if messagebox.askyesno("确认", "删除任务记录？（不会删除已下载文件）"):
            self.registry.pop(item[0], None)
            _DELETED_TIDS.add(item[0])  # 防跨进程合并时复活已删除的任务
            save_registry(self.registry)
            self._refresh_task_table()

    # ---------- 其他 ----------
    def _show_welcome(self):
        """首次启动引导：说明项目结构 + 填写 API Key"""
        dlg = tk.Toplevel(self)
        dlg.title("欢迎使用 AutoDL ComfyUI 视频生成器")
        dlg.geometry("560x420")
        dlg.grab_set()
        dlg.transient(self)

        info = tk.Text(dlg, wrap="word", padx=10, pady=10, height=14, state="normal")
        info.insert("1.0", r"""欢迎！这是 AutoDL ComfyUI 工作流的图形化生成器。

首次使用需要完成两个步骤：

步骤1：获取 API Token
  前往 https://autodl.art/large-model/tokens
  创建令牌，分组选「ComfyUI」，
  复制令牌填入下方输入框，点击【保存并开始】。

步骤2：了解项目将创建的结构
  本程序会在程序所在目录（或启动目录）依次创建：

  config.json      你的 API Token 与下载设置
  tasks.json       任务记录（提交过什么、什么状态、下载到哪）
  outputs\         视频/图片生成结果自动下载到这里
  materials\       素材库——把你的参考图片/音频放进这里
  extracted_frames\  帧库——提取的视频帧自动保存到这里
  imported_cache\  导入 payload 时从 data URI 解出的素材
  payload_autosave\  提交时自动保存的 payload 存档（可勾选开启）
  payloads\        （可选）你自己保存的 payload 模板

说明：
  • 提交任务立即返回，进程间大约每 5 秒自动查询状态
  • 完成后自动下载结果到 outputs\，双击任务可播放
  • 所有的 API Key 只保存在本机 config.json 中
""")
        info.config(state="disabled")
        info.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=10, pady=4)
        ttk.Label(row, text="API Token:").pack(side="left", padx=(0, 4))
        var_setup = tk.StringVar(value=self.var_key.get())
        ent = ttk.Entry(row, textvariable=var_setup, show="*", width=46)
        ent.pack(side="left", fill="x", expand=True)
        ref = ttk.Frame(dlg)
        ref.pack(fill="x", padx=10, pady=(0, 4))
        # 复选"我不愿意保存 token，每次都手动输入"
        self.var_skip_save = tk.BooleanVar(value=False)
        ttk.Checkbutton(ref, text="保存到 config.json（下次启动免输入）",
                        variable=self.var_skip_save).pack(side="left", padx=(0, 12))

        btn_row = ttk.Frame(dlg)
        btn_row.pack(fill="x", padx=10, pady=8)
        ttk.Button(btn_row, text="保存并开始", command=lambda: self._welcome_done(dlg, var_setup)).pack(side="right")

        ent.focus_set()
        dlg.bind("<Return>", lambda e: self._welcome_done(dlg, var_setup))
        dlg.protocol("WM_DELETE_WINDOW", lambda: self._welcome_done(dlg, var_setup, skip=True))

    def _welcome_done(self, dlg, var_setup, skip=False):
        key = var_setup.get().strip()
        if key:
            self.var_key.set(key)
            if self.var_skip_save.get():
                self.cfg["api_key"] = key
                save_config(self.cfg)
            self.status.config(text="API Token 已配置")
        else:
            self.status.config(text="未配置 Token（稍后可在顶部输入）")
        dlg.destroy()

    def _save_key(self):
        # 输入框为空 = 保存空 Token（用户主动清空）
        self.cfg["api_key"] = self.var_key.get().strip()
        save_config(self.cfg)
        self.status.config(text="配置已保存" if self.cfg["api_key"] else "已保存（Token 为空）")

    def _clear_form(self):
        self._on_workflow_change()

    def _import_payload(self):
        """从已保存的 payload JSON 文件导入到表单，并按工作流字段匹配自动切换"""
        path = filedialog.askopenfilename(
            title="选择 payload JSON 文件",
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            data = json.loads(read_text_file(path))
        except Exception as e:
            messagebox.showerror("导入失败", f"无法读取 payload: {e}")
            return
        if not isinstance(data, dict):
            messagebox.showerror("导入失败", "payload 格式错误：根节点必须是对象")
            return

        # 尝试判定工作流：优先用导出的来源标记，否则按字段组合推断
        marked = data.get("__workflow_id")
        if marked and marked in WORKFLOW_BY_ID:
            target = WORKFLOW_BY_ID[marked]
        else:
            candidates = self._detect_workflow_candidates(data)
            if len(candidates) == 1:
                target = candidates[0]
            elif len(candidates) > 1:
                target = self._ask_workflow_choice(candidates)
            else:
                target = None
        if target:
            self.var_workflow.set(target["name"])
            self._on_workflow_change()
            self.lbl_wf_id.config(text=self.workflow["id"])

        wf = self.workflow

        # scale 兼容：文件路径相对于 payload 所在目录解析
        rel_dir = os.path.dirname(os.path.abspath(path))

        # duration / audio_duration
        dur_field = wf.get("duration_field", "duration")
        if dur_field in data and isinstance(data[dur_field], int):
            self.var_duration.set(str(data[dur_field]))

        # resolution
        if data.get("resolution") in wf["resolutions"]:
            self.var_resolution.set(data["resolution"])

        # seed
        if wf["has_seed"] and "seed" in data:
            self.var_seed.set(str(data["seed"]))

        # prompt
        if wf["has_prompt"] and isinstance(data.get("prompt"), str) and self.txt_prompt is not None:
            self.txt_prompt.delete("1.0", "end")
            self.txt_prompt.insert("1.0", data["prompt"])

        # 首尾帧
        if wf.get("frame_mode") == "first_last":
            for key, var, attr in (("first_frame", "var_ff", "first_frame_path"),
                                   ("last_frame", "var_lf", "last_frame_path")):
                val = data.get(key)
                if not val:
                    continue
                local = self._resolve_imported_uri(val, rel_dir)
                if local:
                    setattr(self, attr, local)
                    getattr(self, var).set(os.path.basename(local) if os.path.exists(local) else "已导入")
        else:
            # 参考图
            ref_prefix = wf.get("ref_prefix")
            if ref_prefix:
                for i in range(wf["max_images"]):
                    val = data.get(f"{ref_prefix}{i}")
                    if not val:
                        continue
                    local = self._resolve_imported_uri(val, rel_dir)
                    if local:
                        self.img_paths[i] = local
                        self._update_img_thumb(i, local)

            # 参考音频
            audio_prefix = wf.get("audio_prefix")
            if audio_prefix:
                for i in range(wf["max_audios"]):
                    val = data.get(f"{audio_prefix}{i}")
                    if not val:
                        continue
                    local = self._resolve_imported_uri(val, rel_dir)
                    if local:
                        self.audio_paths[i] = local
                        lbl = self._audio_labels.get(i)
                        if lbl:
                            lbl.config(text=os.path.basename(local)[:14], foreground="#333")

        self.status.config(text=f"已导入: {os.path.basename(path)}")
        messagebox.showinfo("导入完成", f"已导入 payload: {os.path.basename(path)}\n\n"
                            "图片/音频如为本地路径或 data URI，已自动转为可预览的本地文件。")

    def _build_archive_payload(self, duration, seed, prompt):
        """构建"存档版" payload（素材以本地路径写入，含来源工作流标记），不弹窗"""
        wf = self.workflow
        payload = {}
        # 记录来源工作流（仅本地标记，不参与提交）
        payload["__workflow_id"] = wf["id"]
        dur_field = wf.get("duration_field", "duration")
        payload[dur_field] = duration
        if wf["has_prompt"]:
            payload["prompt"] = prompt
        payload["resolution"] = self.var_resolution.get()
        if wf["has_seed"] and seed is not None:
            payload["seed"] = seed

        if wf.get("frame_mode") == "first_last":
            if self.first_frame_path:
                payload["first_frame"] = self.first_frame_path
            if self.last_frame_path:
                payload["last_frame"] = self.last_frame_path
        else:
            ref_prefix = wf.get("ref_prefix")
            if ref_prefix:
                for i, p in enumerate(self.img_paths[: wf["max_images"]]):
                    if p:
                        payload[f"{ref_prefix}{i}"] = p
            audio_prefix = wf.get("audio_prefix")
            if audio_prefix:
                for i, p in enumerate(self.audio_paths[: wf["max_audios"]]):
                    if p:
                        payload[f"{audio_prefix}{i}"] = p
        return payload

    def _save_autosave_payload(self, payload, short_name=None):
        """把存档版 payload 写入 payload_autosave 目录，返回保存路径。
        short_name 由调用方传入可避免后台线程读取 tkinter 变量。"""
        auto_dir = os.path.join(APP_DIR, "payload_autosave")
        os.makedirs(auto_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int((time.time() * 1000) % 1000):03d}"
        base = short_name or (self.workflow.get("short_name") or self.workflow["id"])
        path = os.path.join(auto_dir, f"{ts}_{base}.json")
        n = 1
        while os.path.exists(path):
            path = os.path.join(auto_dir, f"{ts}_{n}_{base}.json")
            n += 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    def _export_payload(self):
        """把当前表单导出为 payload JSON（素材写路径而非 base64，便于阅读/复用）"""
        wf = self.workflow
        try:
            duration, seed, prompt = self._collect_form()
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return

        payload = self._build_archive_payload(duration, seed, prompt)

        default_name = f"payload_{wf['id'].split('_')[-1]}.json"
        path = filedialog.asksaveasfilename(
            title="导出 payload",
            initialdir="payloads" if os.path.isdir("payloads") else "",
            initialfile=default_name,
            defaultextension=".json",
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
            return
        self.status.config(text=f"已导出: {path}")
        messagebox.showinfo("导出完成", f"已导出:\n{path}\n\n"
                            "素材以文件路径写入，可移动到其他位置后用「导入 payload」恢复。")

    def _detect_workflow_candidates(self, data):
        """根据 payload 字段集合反推所有可能的工作流候选（可能多个，需用户确认）"""
        has_audio = any(k.startswith("ref_audio_") for k in data)
        has_first = "first_frame" in data
        has_img = any(k.startswith("ref_image_") for k in data)
        n_img = sum(1 for k in data if k.startswith("ref_image_") and data.get(k))
        dur = data.get("duration", 0)
        res = data.get("resolution", "")
        cands = []

        if has_first:
            # 首尾帧：旧版（无 seed）或 b99_002（有 seed / 736p）
            cands = [WORKFLOW_BY_ID.get("minimax_h3_lightx2v")]
            if "seed" in data or (res and res.startswith("736p")):
                cands.append(WORKFLOW_BY_ID.get("minimax_h3_b99_002"))
        elif not has_img:
            # 纯文生：旧版 no_pic（无 seed）或 b99_001（有 seed / 736p）
            cands = [WORKFLOW_BY_ID.get("minimax_h3_lightx2v_no_pic")]
            if "seed" in data or (res and res.startswith("736p")):
                cands.append(WORKFLOW_BY_ID.get("minimax_h3_b99_001"))
        elif "audio_duration" in data:
            # 对口型：唯一
            cands = [WORKFLOW_BY_ID.get("minimax_h3_image_audio_to_video")]
        elif has_audio:
            # 多音频：v2 系列或 15s 版本，按 duration 区分（可能重叠）
            v2 = WORKFLOW_BY_ID.get("minimax_h3_image_audio_to_video_v2")
            v2_15 = WORKFLOW_BY_ID.get("minimax_h3_image_audio_to_video_v2_15s")
            if dur <= v2["duration_max"]:
                cands.append(v2)
            if dur <= v2_15["duration_max"] and res in v2_15["resolutions"] and n_img <= v2_15["max_images"]:
                cands.append(v2_15)
            if v2["resolutions"] != v2_15["resolutions"]:
                if res not in [w["resolutions"] for w in cands]:
                    pass
        else:
            # 无音频多图：v5 / v5_15s / b99_003（按图数/时长/分辨率交叉过滤）
            v5 = WORKFLOW_BY_ID.get("minimax_h3_lightx2v_v5")
            v5_15 = WORKFLOW_BY_ID.get("minimax_h3_lightx2v_v5_15s")
            b99_003 = WORKFLOW_BY_ID.get("minimax_h3_b99_003_12s")
            for w in (v5, v5_15):
                if n_img > w["max_images"]:
                    continue
                if res and res not in w["resolutions"]:
                    continue
                if dur and dur > w["duration_max"]:
                    continue
                cands.append(w)
            # B99 多图：736p 或带 seed 时才作为候选
            if b99_003 and n_img <= b99_003["max_images"] and dur <= b99_003["duration_max"] \
                    and ((res and res in b99_003["resolutions"]) or ("seed" in data and not res)):
                cands.append(b99_003)

        # 去重 + 过滤 None
        seen, out = set(), []
        for w in cands:
            if w and w["id"] not in seen:
                seen.add(w["id"])
                out.append(w)
        return out

    def _ask_workflow_choice(self, candidates):
        """弹窗让用户从多个候选工作流中选择一个"""
        dlg = tk.Toplevel(self)
        dlg.title("请确认生成方式")
        dlg.geometry("480x320")
        dlg.grab_set()
        dlg.transient(self)
        result = {"value": None}

        ttk.Label(dlg, text="根据该 payload 的字段，可能属于以下生成方式之一：\n"
                            "这个 payload 里没有记录来源工作流，请选择正确的：",
                  wraplength=460).pack(padx=12, pady=(12, 8))

        var_choice = tk.StringVar(value=candidates[0]["name"])
        frame = ttk.Frame(dlg)
        frame.pack(fill="both", expand=True, padx=12)
        for w in candidates:
            ttk.Radiobutton(frame, text=f"{w['name']}\n    ({w['id']} — {w['desc'][:60]}…)",
                            value=w["name"], variable=var_choice,
                            wraplength=440).pack(anchor="w", padx=4, pady=2)

        def confirm():
            for w in candidates:
                if w["name"] == var_choice.get():
                    result["value"] = w
                    break
            dlg.destroy()

        btn = ttk.Frame(dlg)
        btn.pack(fill="x", padx=12, pady=10)
        ttk.Button(btn, text="确定", command=confirm).pack(side="right")
        dlg.wait_window()
        return result["value"]

    def _resolve_imported_uri(self, val, rel_dir):
        """data URI -> 解出临时文件；本地路径 -> 绝对路径；http 直链原样返回或 None"""
        if val.startswith("data:"):
            from io import BytesIO
            import base64 as b64
            try:
                header, _, body = val.partition(",")
                ext = ".bin"
                if "png" in header:
                    ext = ".png"
                elif "jpeg" in header or "jpg" in header:
                    ext = ".jpg"
                elif "webp" in header:
                    ext = ".webp"
                elif "wav" in header:
                    ext = ".wav"
                elif "mp3" in header:
                    ext = ".mp3"
                elif "flac" in header:
                    ext = ".flac"
                tmp_dir = os.path.join(APP_DIR, "imported_cache")
                os.makedirs(tmp_dir, exist_ok=True)
                tmp = os.path.join(tmp_dir, f"imp_{int(time.time())}_{len(os.listdir(tmp_dir))}{ext}")
                with open(tmp, "wb") as f:
                    f.write(b64.b64decode(body))
                return tmp
            except Exception:
                return None
        if val.startswith("http"):
            return None
        # 本地路径（绝对或相对）
        p = val if os.path.isabs(val) else os.path.join(rel_dir, val)
        return p if os.path.exists(p) else None


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
