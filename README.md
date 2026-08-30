# AutoDL ComfyUI Video Studio

基于 [AutoDL.art ComfyUI API](https://autodl.art/docs/comfyui_api/) 的 MiniMax H3 视频生成工具。

## 功能
- 支持 10 种工作流：多图/文生/首尾帧/多图多音频/对口型/新 B99 系列（736p，支持 seed）
- 自动轮询、完成后自动下载（命名含时间戳毫秒/工作流/任务ID）
- GUI：参考图上传预览、音频上传、seed 控制、任务列表（第2帧缩略图/时长/帧提取）
- 内置视频播放器：帧级浏览/精确跳转/进度条点击跳转/提取帧/静音
- 帧库 + 素材库（图片/音频预览，双击填入引用，右键设首尾帧）
- 计划任务：定时/立刻批量提交，失败重排队尾，累计≥5次中断保留余下

## 使用
1. 在 https://autodl.art/large-model/tokens 创建令牌（分组 ComfyUI）
2. 运行 `python comfy_gui.py`（或打包的 exe）
3. 选择生成方式 → 上传素材 → 填 prompt → 提交
4. [任务列表] 里自动轮询下载；双击视频可在内置播放器逐帧查看

## 编译为 exe

```bash
# 1. 安装依赖
pip install -r requirements.txt
# 2. 安装 PyInstaller
pip install pyinstaller
# 3. 准备 FFmpeg/FFprobe（可选，视频播放/音频需要）
#    下载 ffmpeg.exe 与 ffprobe.exe 放入 vendor/ 目录
#    https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
#    解压后 bin/ 下的两个 exe 复制到 vendor/
# 4. 编译（需已安装 Python 3.10+ 的 Windows）
pyinstaller --noconfirm comfy_gui.spec
# 5. 产物在 dist/AutoDL_ComfyUI_Generator.exe
```

> 不放入 vendor 也可编译运行，程序会回退用 imageio-ffmpeg 的 ffmpeg（但缺 ffprobe，视频播放会受限）。

## 项目结构
```
comfy_gui.py       GUI 主程序（tkinter）
api_client.py      AutoDL ComfyUI API 客户端
workflows.py       工作流参数定义（10 种）
video_player.py    视频播放/帧提取/进度条（cv2）
frame_library.py    帧库/素材库面板（缩略图网格）
ffmpeg_utils.py    ffmpeg/ffprobe 路径解析
manager.py         命令行任务管理（可选）
comfy_gui.spec     PyInstaller 打包配置
vendor/            ffmpeg/ffprobe（自备，可省略）
```

## 依赖
见 `requirements.txt`：requests / pillow / opencv-python / imageio-ffmpeg / sounddevice

## 许可
MIT。
