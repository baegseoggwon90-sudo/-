# 한국 영상 교체기 — 인터넷 서버용 이미지
FROM python:3.12-slim

# ffmpeg 는 imageio-ffmpeg, libmediainfo 는 pymediainfo 패키지에 들어 있어 따로 설치할 것이 없습니다.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kbroll ./kbroll

# 서버 모드: 비밀번호(KBROLL_PASSWORD) 로그인, 작업 파일은 /data 에 저장 (영구 디스크를 연결하세요)
ENV KBROLL_PUBLIC=1 \
    KBROLL_WORKDIR=/data \
    PYTHONUNBUFFERED=1 \
    PORT=8765
EXPOSE 8765
CMD ["python", "-m", "kbroll", "web", "--public"]
