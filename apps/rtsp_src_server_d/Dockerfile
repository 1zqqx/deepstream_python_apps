# 推流端：Python + FFmpeg，循环向 MediaMTX 推 RTSP
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY main.py .
COPY streams.json .

# 默认把视频放在 /data，由运行时的 volume 挂载
RUN mkdir -p /data

ENV VIDEO_PATH=/data/video.mp4
ENV RTSP_PUBLISH_URL=rtsp://mediamtx:8554/stream

CMD ["python", "main.py", "--config", "streams.json"]
