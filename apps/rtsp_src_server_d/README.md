# RTSP 固定视频循环推流（Docker）

在 Docker 内用 **MediaMTX** 作为 RTSP 服务端，由 **Python + FFmpeg** 容器向**多个不同地址**循环推流多路固定视频，供 DeepStream 或其他客户端通过 `rtsp://` 拉流。

## 架构

- **mediamtx**：官方镜像 `bluenviron/mediamtx`，提供 RTSP/RTMP 服务，接收推流并对外提供拉流。
- **publisher**：本仓库镜像，内装 Python + FFmpeg，可为多路 (视频 → RTSP URL) 并行循环推流。

## 使用前准备

1. 准备视频文件（如 H.264/H.265 的 mp4），放到挂载目录（如 `./data.temp/` 或 `./data/`）。
2. **单路**：沿用 `VIDEO_PATH` / `RTSP_PUBLISH_URL` 或 `--video` / `--url`。
3. **多路**：使用配置文件或重复 `--stream`，见下文。

## 运行

```bash
cd apps/rtsp_src_server_d
docker compose up -d
```

拉流地址（在宿主机或同一网络）：

- 单路默认：`rtsp://localhost:8554/stream`
- 多路时每个地址不同，例如：`rtsp://localhost:8554/cam1`、`rtsp://localhost:8554/cam2`

测试拉流示例：

```bash
ffplay rtsp://localhost:8554/stream
ffplay rtsp://localhost:8554/cam1
```

## 多路推流配置

在同一流服务器（MediaMTX）上通过不同路径推多路视频：

**方式一：命令行重复 `--stream`**

```bash
python main.py \
  --stream /data/cam1.mp4,rtsp://mediamtx:8554/cam1 \
  --stream /data/cam2.mp4,rtsp://mediamtx:8554/cam2
```

**方式二：JSON 配置文件（推荐）**

复制并编辑示例：`cp streams.json.example streams.json`，格式：

```json
[
  { "video": "/data/cam1.mp4", "url": "rtsp://mediamtx:8554/cam1" },
  { "video": "/data/cam2.mp4", "url": "rtsp://mediamtx:8554/cam2" }
]
```

运行：

```bash
python main.py --config streams.json
```

```bash
docker compose -f docker-compose.yml up/down [-d]
```

**方式三：环境变量 `STREAMS_JSON`**

在 docker-compose 中可为 `publisher` 设置环境变量 `STREAMS_JSON` 为上述 JSON 数组的字符串（便于多路且不挂载配置文件）。

## 仅本地跑 Python（不跑 Docker）

需本机已安装 FFmpeg，且已有 MediaMTX 或其他 RTSP 服务在运行：

```bash
# 单路
python main.py --video /path/to/video.mp4 --url rtsp://localhost:8554/stream

# 多路
python main.py --config streams.json
python main.py --stream /path/to/a.mp4,rtsp://localhost:8554/a --stream /path/to/b.mp4,rtsp://localhost:8554/b
```

## 环境变量与参数

| 环境变量 / 参数 | 默认值 | 说明 |
|-----------------|--------|------|
| `VIDEO_PATH` / `--video` | `/data/video.mp4` | 单路时本地视频路径 |
| `RTSP_PUBLISH_URL` / `--url` | `rtsp://mediamtx:8554/stream` | 单路时推流目标 RTSP URL |
| `STREAMS_JSON` | - | 多路时 JSON 数组字符串，同 `--config` 文件内容 |
| `--stream` | - | 多路：一路「视频,URL」，可多次指定 |
| `--config` | - | 多路：从 JSON 文件读取流列表 |
| `--restart-delay` | 5.0 | 某路 FFmpeg 异常退出后，该路重启前等待秒数 |

## 说明

- 使用 **MediaMTX** 官方 Docker 镜像；推流端为自定义 Python+FFmpeg 镜像。
- 每路视频在 FFmpeg 内 `-stream_loop -1` 无限循环、`-re` 按原速推流；多路在线程中并行运行，某路异常退出仅该路按 `--restart-delay` 重启。
