@echo off
chcp 65001 > nul
cd /d "%~dp0"
python -c "import imageio_ffmpeg, anthropic" 2>nul || python -m pip install -r requirements.txt
python -m kbroll web
if errorlevel 1 pause
