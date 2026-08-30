# -*- coding: utf-8 -*-
"""ffmpeg / ffprobe 路径解析与配置。

查找顺序：
1. 程序目录下 vendor/ffmpeg.exe、vendor/ffprobe.exe（打包分发用）
2. imageio_ffmpeg 自带的 ffmpeg（仅 ffmpeg，无 ffprobe）
3. 系统 PATH
找到后自动告知 pyvidplayer2（通过 PATH 注入）。
"""

import os
import subprocess as _subprocess
import sys

APP_DIR_FUNC = None  # 由 comfy_gui 注入（避免循环导入）

# ---- windowed exe 下禁用子进程弹出控制台窗口 ----
_CREATE_NO_WINDOW = 0x08000000
_orig_popen = _subprocess.Popen


def _popen_noconsole(*args, **kwargs):
    if os.name == "nt" and kwargs.get("creationflags", 0) == 0:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    return _orig_popen(*args, **kwargs)


_subprocess.Popen = _popen_noconsole


def set_app_dir(func):
    global APP_DIR_FUNC
    APP_DIR_FUNC = func


def _candidates():
    """返回可能的 app 目录（frozen 后 exe 目录 + _MEIPASS 临时目录）"""
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
        if getattr(sys, "_MEIPASS", None):
            dirs.append(sys._MEIPASS)
    if APP_DIR_FUNC is not None:
        d = APP_DIR_FUNC()
        if d not in dirs:
            dirs.append(d)
    else:
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
    return dirs


def _find(name):
    for d in _candidates():
        p = os.path.join(d, "vendor", name)
        if os.path.exists(p):
            return p
    return None


def setup():
    """在入口调用一次：收集可用二进制并把 vendor 路径加入 PATH"""
    found_any = False
    for d in _candidates():
        vendor = os.path.join(d, "vendor")
        if (os.path.exists(os.path.join(vendor, "ffmpeg.exe"))
                or os.path.exists(os.path.join(vendor, "ffprobe.exe"))):
            if vendor not in os.environ["PATH"].split(os.pathsep):
                os.environ["PATH"] = vendor + os.pathsep + os.environ.get("PATH", "")
            found_any = True
            break  # 第一个有的即可

    # 没有 vendor 时回退到 imageio_ffmpeg 的 ffmpeg（仍缺 ffprobe）
    if not _find("ffmpeg.exe"):
        try:
            import imageio_ffmpeg
            img_ff = os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())
            if img_ff and img_ff not in os.environ["PATH"].split(os.pathsep):
                os.environ["PATH"] = img_ff + os.pathsep + os.environ.get("PATH", "")
                found_any = True
        except Exception:
            pass
    return found_any


def ffmpeg_path():
    p = _find("ffmpeg.exe")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def ffprobe_path():
    p = _find("ffprobe.exe")
    return p if p else "ffprobe"
