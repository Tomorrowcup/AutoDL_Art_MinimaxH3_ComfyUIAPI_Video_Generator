# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：vendor/ffmpeg+ffprobe 与 imageio-ffmpeg 一并打包

import os

base = os.path.dirname(os.path.abspath(SPEC))

vendor_dir = os.path.join(base, "vendor")
ff_bins = []
for fn in ("ffmpeg.exe", "ffprobe.exe"):
    p = os.path.join(vendor_dir, fn)
    if os.path.exists(p):
        ff_bins.append((p, "vendor"))

# 兜底 imageio_ffmpeg（只在 vendor 缺失时使用）
try:
    import imageio_ffmpeg
    if not any("ffmpeg.exe" in os.path.basename(b[0]) for b in ff_bins):
        ff_bins.append((imageio_ffmpeg.get_ffmpeg_exe(), "imageio_ffmpeg/binaries"))
except Exception:
    pass

from PyInstaller.utils.hooks import copy_metadata

meta_datas = []
for pkg in ("imageio", "moviepy", "imageio_ffmpeg", "pyvidplayer2"):
    try:
        meta_datas += copy_metadata(pkg)
    except Exception:
        pass

a = Analysis(
    ['comfy_gui.py'],
    pathex=[],
    binaries=ff_bins,
    datas=meta_datas,
    hiddenimports=['PIL.Image', 'PIL.ImageTk', 'sounddevice'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='AutoDL_ComfyUI_Generator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
