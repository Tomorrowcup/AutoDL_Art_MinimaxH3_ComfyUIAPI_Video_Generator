@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 启动监测进程 (watch --daemon)...
echo 提示: 此窗口会保持开启运行监测, 关闭本窗口即停止监测。
echo.
python manager.py watch --daemon
pause