#!/usr/bin/env python3
"""
在流服务器中通过不同地址推流多个视频：每个 (视频, RTSP URL) 一路循环推流，多路并行。

用法（单路，兼容旧版）:
    python main.py --video /data/video.mp4 --url rtsp://mediamtx:8554/stream

用法（多路）:
    python main.py --stream /data/cam1.mp4,rtsp://mediamtx:8554/cam1 --stream /data/cam2.mp4,rtsp://mediamtx:8554/cam2
    python main.py --config streams.json

配置文件 streams.json 格式:
    [{"video": "/data/cam1.mp4", "url": "rtsp://mediamtx:8554/cam1"}, ...]

环境变量:
    STREAMS_JSON  - 可选，多路配置 JSON 字符串（同 --config 文件内容）
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time


def parse_args():
    p = argparse.ArgumentParser(description="多路 RTSP 循环推流（单路或多路）")
    p.add_argument(
        "--video",
        default=os.environ.get("VIDEO_PATH"),
        help="单路时：本地视频路径",
    )
    p.add_argument(
        "--url",
        default=os.environ.get("RTSP_PUBLISH_URL"),
        help="单路时：推流目标 RTSP URL",
    )
    p.add_argument(
        "--stream",
        action="append",
        dest="streams",
        metavar="VIDEO_PATH,RTSP_URL",
        help="多路：一路推流，可多次指定。例: --stream /data/a.mp4,rtsp://mediamtx:8554/a",
    )
    p.add_argument(
        "--config",
        metavar="PATH",
        help='多路：从 JSON 文件读取 [{"video": path, "url": rtsp_url}, ...]',
    )
    p.add_argument(
        "--restart-delay",
        type=float,
        default=5.0,
        help="某路 FFmpeg 退出后重启等待秒数",
    )
    return p.parse_args()


def load_streams_from_args(args):
    """返回 [(video_path, rtsp_url), ...]，至少一路。"""
    # 1) 多路：--config
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data:
            raise SystemExit("错误: --config 文件为空或不是数组")
        return [(item["video"], item["url"]) for item in data]

    # 2) 多路：环境变量 STREAMS_JSON
    raw = os.environ.get("STREAMS_JSON", "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SystemExit(f"错误: STREAMS_JSON 不是合法 JSON: {e}")
        if not data:
            raise SystemExit("错误: STREAMS_JSON 为空或不是数组")
        return [(item["video"], item["url"]) for item in data]

    # 3) 多路：重复 --stream
    if args.streams:
        out = []
        for s in args.streams:
            part = s.split(",", 1)
            if len(part) != 2:
                raise SystemExit(
                    f"错误: --stream 格式应为 VIDEO_PATH,RTSP_URL，得到: {s}"
                )
            out.append((part[0].strip(), part[1].strip()))
        return out

    # 4) 单路：--video / --url 或环境变量
    video = args.video or os.environ.get("VIDEO_PATH", "/data/video.mp4")
    url = args.url or os.environ.get("RTSP_PUBLISH_URL", "rtsp://mediamtx:8554/stream")
    return [(video, url)]


def run_ffmpeg_rtsp_push(video_path: str, rtsp_url: str) -> subprocess.Popen:
    """启动 FFmpeg：-stream_loop -1 无限循环该视频，按原速 -re 推流到 RTSP。"""
    cmd = [
        "ffmpeg",
        "-re",
        "-stream_loop",
        "-1",
        "-i",
        video_path,
        "-c",
        "copy",
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        rtsp_url,
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def stream_worker(
    stream_id: int,
    video_path: str,
    rtsp_url: str,
    restart_delay: float,
    stop_event: threading.Event,
    current_procs: list,
    lock: threading.Lock,
):
    """单路推流循环：不断启动 FFmpeg，失败则等待后重启；遇 stop_event 退出。"""
    while not stop_event.is_set():
        if not os.path.isfile(video_path):
            print(
                f"[stream-{stream_id}] 错误: 视频不存在 {video_path}", file=sys.stderr
            )
            break
        proc = run_ffmpeg_rtsp_push(video_path, rtsp_url)
        with lock:
            current_procs.append((stream_id, proc))
        try:
            _, stderr = proc.communicate(timeout=None)
            if stderr and proc.returncode != 0:
                print(f"[stream-{stream_id}] stderr: {stderr[:500]}", file=sys.stderr)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        finally:
            with lock:
                current_procs[:] = [
                    (i, p) for (i, p) in current_procs if i != stream_id or p != proc
                ]
        if stop_event.is_set():
            break
        if proc.returncode != 0:
            print(
                f"[stream-{stream_id}] 退出码 {proc.returncode}，{restart_delay}s 后重启...",
                file=sys.stderr,
            )
        time.sleep(restart_delay)


def main():
    args = parse_args()
    streams = load_streams_from_args(args)

    for i, (video_path, rtsp_url) in enumerate(streams):
        if not os.path.isfile(video_path):
            print(f"错误: 视频不存在: {video_path}", file=sys.stderr)
            sys.exit(1)
        print(f"  [{i}] {video_path} -> {rtsp_url}")
    print("多路推流已启动（Ctrl+C 退出）...")

    stop_event = threading.Event()
    current_procs = []
    lock = threading.Lock()

    def on_sig(signum, frame):
        stop_event.set()
        with lock:
            to_terminate = [(i, p) for i, p in current_procs]
        for _id, proc in to_terminate:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass

    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    threads = []
    for i, (video_path, rtsp_url) in enumerate(streams):
        t = threading.Thread(
            target=stream_worker,
            args=(
                i,
                video_path,
                rtsp_url,
                args.restart_delay,
                stop_event,
                current_procs,
                lock,
            ),
            daemon=True,
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
