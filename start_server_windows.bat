@echo off
chcp 65001 > nul
cd /d "%~dp0"
python -c "import imageio_ffmpeg, anthropic, pycapcut" 2>nul || python -m pip install -r requirements.txt
echo 내 컴퓨터를 서버로 켭니다. 이 창을 닫으면 인터넷 접속도 끊깁니다.
python -m kbroll web --tunnel
pause
