# deepstream-test3 Pipeline 数据流通路径

> 基于 `deepstream_test_3.py` 的 Pipeline 结构绘制，用于理解多路输入 → 批推理 → 宫格显示 的数据流。

---

## 1. Pipeline 数据流总览

```
                    ┌─────────────────────────────────────────────────────────────────────────────┐
                    │                              Pipeline                                      │
                    │                                                                             │
  URI 0 ──────────► │  source-bin-00 (uridecodebin / nvurisrcbin)                                  │
  URI 1 ──────────► │  source-bin-01 (uridecodebin / nvurisrcbin)         sink_0 ──┐               │
  ...              │  ...                                                          │               │
  URI N-1 ────────► │  source-bin-(N-1) (uridecodebin / nvurisrcbin)                │               │
                    │       │                        │              sink_(N-1) ─────┤               │
                    │       └────────────────────────┼──────────────────────────────┼───────────────┤
                    │                                ▼                             ▼               │
                    │                        ┌───────────────┐                                       │
                    │                        │ nvstreammux   │  多路 → 1 个 batch (N 帧 + NvDsBatchMeta) │
                    │                        │ sink_0..N-1   │  width=1920, height=1080, batch-size=N   │
                    │                        └───────┬───────┘                                       │
                    │                                │ 1 个 GstBuffer (N 帧 batch)                    │
                    │                                ▼                                               │
                    │                        ┌───────────────┐                                       │
                    │                        │   queue1      │  缓冲，解耦 muxer 与 pgie             │
                    │                        └───────┬───────┘                                       │
                    │                                ▼                                               │
                    │                        ┌───────────────┐                                       │
                    │                        │  pgie         │  批量推理 (nvinfer / nvinferserver)   │
                    │                        │ nvinfer 等    │  附加 NvDsObjectMeta 到 frame_meta   │
                    │                        └───────┬───────┘                                       │
                    │                                │ ★ pgie src pad 上有 probe: pgie_src_pad_buffer_probe │
                    │                                │   - 遍历 batch_meta.frame_meta_list          │
                    │                                │   - perf_data.update_fps(stream_index)       │
                    │                                ▼                                               │
                    │                        ┌───────────────┐                                       │
                    │                        │   queue2      │  缓冲，解耦 pgie 与 tiler             │
                    │                        └───────┬───────┘                                       │
                    │                                │                                               │
                    │                    [可选]      ▼                                               │
                    │                        ┌───────────────┐                                       │
                    │                        │ nvdslogger    │  仅当 --disable-probe 时加入          │
                    │                        └───────┬───────┘                                       │
                    │                                ▼                                               │
                    │                        ┌───────────────┐                                       │
                    │                        │ nvmultistream │  多路 batch → 1 个宫格图 (2D 拼接)    │
                    │                        │    tiler      │  rows×columns, 1280×720 总输出        │
                    │                        └───────┬───────┘                                       │
                    │                                ▼                                               │
                    │                        ┌───────────────┐                                       │
                    │                        │   queue3      │  缓冲                                 │
                    │                        └───────┬───────┘                                       │
                    │                                ▼                                               │
                    │                        ┌───────────────┐                                       │
                    │                        │ nvvideoconvert│  格式/分辨率转换                      │
                    │                        └───────┬───────┘                                       │
                    │                                ▼                                               │
                    │                        ┌───────────────┐                                       │
                    │                        │   queue4      │  缓冲                                 │
                    │                        └───────┬───────┘                                       │
                    │                                ▼                                               │
                    │                        ┌───────────────┐                                       │
                    │                        │   nvdsosd     │  根据 NvDsObjectMeta 绘制框、文字     │
                    │                        └───────┬───────┘                                       │
                    │                                ▼                                               │
                    │                        ┌───────────────┐                                       │
                    │                        │   queue5      │  缓冲                                 │
                    │                        └───────┬───────┘                                       │
                    │                                ▼                                               │
                    │                        ┌───────────────┐                                       │
                    │                        │    sink       │  nveglglessink / nv3dsink / fakesink  │
                    │                        └───────────────┘                                       │
                    │                                                                             │
                    └─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 各阶段数据形态简述

| 阶段 | 元素 | 输入 | 输出 |
|------|------|------|------|
| 源 | source-bin-XX | URI (file://, rtsp://) | 解码后 video/x-raw(NVMM)，每路独立 GstBuffer |
| 合批 | nvstreammux | N 路 GstBuffer | 1 个 GstBuffer，内含 N 帧 + NvDsBatchMeta，每帧带 pad_index |
| 缓冲 | queue1 | 上述 batch | 透传，用于解耦 |
| 推理 | pgie | 上述 batch | 同一 batch，附加 NvDsObjectMeta (检测框、类别) |
| 缓冲 | queue2 | 上述 batch | 透传 |
| 拼接 | nvmultistreamtiler | 1 个 batch | 1 个 GstBuffer，多路拼成 1280×720 宫格图 |
| 转换 | nvvideoconvert | 宫格图 | 格式/分辨率转换 |
| 绘制 | nvdsosd | 上述 buffer | 绘制检测框、文字叠加 |
| 显示 | sink | 最终 buffer | 屏幕显示 或 fakesink 丢弃 |

---

## 3. source-bin 内部结构

```
source-bin-XX (Gst.Bin)
│
├── uridecodebin / nvurisrcbin ("uri-decode-bin")
│   ├── uri: file://... 或 rtsp://...
│   ├── 动态创建 decoder src pad → pad-added 回调
│   └── cb_newpad: 将 decoder src pad 绑定到 ghost pad
│
└── GhostPad "src"  ← 作为 source-bin 对外出口，后续 link 到 streammux sink_X
```

- **uridecodebin**：通用解码，不循环
- **nvurisrcbin**：`--file-loop` 时使用，支持文件循环
- Ghost Pad 在 `cb_newpad` 中通过 `set_target(decoder_src_pad)` 绑定真实 pad

---

## 4. nvstreammux 合批规则

- 每路输入对应一个 `sink_X` pad
- 按 `batched-push-timeout` (默认 33ms) 或凑满 batch 后推送
- 输出 1 个 GstBuffer，内含 N 路帧，每帧有 `NvDsFrameMeta.pad_index` (0..N-1)
- 统一缩放为 width×height (默认 1920×1080)

---

## 5. Probe 插入位置

```
pgie.src → [probe: pgie_src_pad_buffer_probe] → queue2
```

- 类型：`Gst.PadProbeType.BUFFER`
- 作用：
  - 遍历 `NvDsBatchMeta.frame_meta_list`
  - 统计每路对象数量并打印 (非 silent 时)
  - `perf_data.update_fps(stream_index)` 更新 FPS
  - 若 `NVDS_ENABLE_LATENCY_MEASUREMENT=1`，调用 `nvds_measure_buffer_latency`

---

## 6. nvmultistreamtiler 宫格布局

- `rows` = floor(sqrt(N))
- `columns` = ceil(N / rows)
- 按 stream-id (pad_index) 顺序排布，从左到右、从上到下：

```
N=2:  stream0 | stream1
N=4:  stream0 | stream1
      stream2 | stream3
```

- 总输出：1280×720 (TILED_OUTPUT_WIDTH × TILED_OUTPUT_HEIGHT)

---

## 7. Sink 类型选择

| 条件 | Sink 类型 |
|------|----------|
| `--no-display` | fakesink (不显示，丢弃) |
| 集成 GPU (Jetson) | nv3dsink |
| x86 + AArch64 | nv3dsink |
| x86 + 非 AArch64 | nveglglessink |

---

## 8. 与 test4 (demux) 的对比

| 项目 | deepstream-test3 | deepstream-test4 (demux) |
|------|------------------|---------------------------|
| 推理后 | 保持 batch，进 tiler | 用 nvstreamdemux 拆成 N 路 |
| 显示 | 1 个宫格窗口 | N 个独立窗口 |
| 输出路数 | 1 路 (宫格) | N 路 |
