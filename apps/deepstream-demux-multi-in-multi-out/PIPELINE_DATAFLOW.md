# deepstream-demux-multi-in-multi-out Pipeline 数据流通路径

## 1. Pipeline 数据流总览

```
                    ┌─────────────────────────────────────────────────────────────────┐
                    │                        Pipeline                                  │
                    │                                                                  │
  URI 0 ──────────► │  source-bin-00 (uridecodebin)                                    │
  URI 1 ──────────► │  source-bin-01 (uridecodebin)         sink_0 ──┐                  │
  ...              │  source-bin-02 ...                              │                  │
  URI N-1 ────────► │  source-bin-(N-1) (uridecodebin)               │                  │
                    │       │              │              ...       │                  │
                    │       └──────────────┼─────────────────────────┼──────────────────┤
                    │                      ▼                         ▼                  │
                    │              ┌───────────────┐                                   │
                    │              │ nvstreammux   │  多路 → 单 batch（带 batch meta）   │
                    │              │ sink_0..N-1   │                                   │
                    │              └───────┬───────┘                                   │
                    │                      │ 1 个 GstBuffer（N 帧 batch）               │
                    │                      ▼                                           │
                    │              ┌───────────────┐                                   │
                    │              │    queue1      │                                   │
                    │              └───────┬───────┘                                   │
                    │                      ▼                                           │
                    │              ┌───────────────┐                                   │
                    │              │   nvinfer     │  批量推理，batch meta 透传          │
                    │              │ (primary-inference)                               │
                    │              └───────┬───────┘                                   │
 同一batch+NvDsBatchMeta(含frame_meta_list) │ pige src pad callback -> pgie_src_pad_buffer_probe()│
                    │                      ▼                                           │
                    │              ┌───────────────┐                                   │
                    │              │ nvstreamdemux │  1 个 batch → N 个独立 GstBuffer    │
                    │              │  sink (1个)   │                                   │
                    │              └───────┬───────┘                                   │
                    │         src_0  src_1  ...  src_(N-1)                             │
                    │           │      │            │                                  │
                    │           ▼      ▼            ▼                                  │
                    │        queue0  queue1  ...  queue(N-1)                            │
                    │           │      │            │                                  │
                    │           ▼      ▼            ▼                                  │
                    │     nvvidconv × N → nvosd × N → nv3dsink/nveglglessink × N       │
                    │                                                                  │
                    └─────────────────────────────────────────────────────────────────┘
```

## 2. 各阶段数据形态简述

| 阶段 | 元素 | 输入 | 输出 |
|------|------|------|------|
| 源 | source-bin-XX | URI | 解码后 video/x-raw(NVMM)，每路独立 |
| 合批 | nvstreammux | N 路 GstBuffer | 1 个 GstBuffer，内含 N 帧 + NvDsBatchMeta，每帧带 pad_index / batch_id |
| 推理 | nvinfer | 上述 batch | 同一 batch，附加检测框等 NvDsObjectMeta，batch/frame 元数据不变 |
| 分路 | nvstreamdemux | 上述 batch | N 个 GstBuffer，每个对应一路（按 pad_index → src_%u） |
| 显示 | queue → nvvidconv → nvosd → sink | 单路 buffer | 各窗口独立显示 |

## 3. nvstreamdemux 如何知道“某帧属于哪一路”？

**结论：由 NvDsFrameMeta 里的 `pad_index` 和 `batch_id` 决定，这两个字段从 nvstreammux 起就带上，nvinfer 只做推理不改变它们，nvstreamdemux 据此分路。**

### 3.1 元数据从哪来

- **nvstreammux** 在把多路输入合成一个 batch 时：
  - 为每一帧创建 **NvDsFrameMeta**，并填入：
    - **pad_index**：对应自己在 muxer 上的 **sink pad 下标**（0, 1, …, N-1），即“来自第几路源”。
    - **batch_id**：该帧在 batch 里 **NvBufSurface 的 surface 数组中的下标**，即“在 batch 里第几帧”。
  - 这些 NvDsFrameMeta 挂在 **NvDsBatchMeta.frame_meta_list** 上，并随 GstBuffer 一起往下游推。

### 3.2 推理阶段是否改变“流归属”

- **nvinfer** 只做批量推理：
  - 在 NvDsFrameMeta 上挂 **NvDsObjectMeta**（检测框、类别等）。
  - **不会** 新增/删除/重排 NvDsFrameMeta，也**不会** 修改 **pad_index**、**batch_id**。
  - 因此“这一帧属于哪一路”在推理前后一致。

### 3.3 nvstreamdemux 如何使用这些字段

- **nvstreamdemux** 收到的是“一个 GstBuffer + 一个 NvDsBatchMeta”：
  - 遍历 **batch_meta.frame_meta_list** 里的每个 **NvDsFrameMeta**；
  - 用 **frame_meta.pad_index** 决定从哪个 **src_%u** 输出（例如 pad_index=1 → 从 `src_1` 推 buffer）；
  - 用 **frame_meta.batch_id** 在 **NvBufSurface** 的 surface 数组里取到对应帧的像素数据，封装成单独的 GstBuffer 从该 src pad 推出。

因此：

- **pad_index**：逻辑上标识“属于哪一路源”（对应 nvstreammux 的 sink_0, sink_1, …）。
- **batch_id**：在物理 buffer 上定位“这一帧在 batch 里的第几块 surface”。

代码里用 `pad_index` 做 FPS 统计也印证了这一点（见 `pgie_src_pad_buffer_probe`）：

```python
stream_index = "stream{0}".format(frame_meta.pad_index)
perf_data.update_fps(stream_index)
```

### 3.4 小结表

| 字段 | 含义 | 谁写入 | 谁使用 |
|------|------|--------|--------|
| **pad_index** | 源在 nvstreammux 上的 sink 下标（流 ID） | nvstreammux | nvstreamdemux（选 src_%u）、probe（统计 per-stream FPS） |
| **batch_id** | 该帧在 batch 的 surface 数组中的下标 | nvstreammux | nvstreamdemux（从 NvBufSurface 取对应帧） |

所以：**“从 nvinfer 出来的、已经做过推理的帧”之所以能正确归到“原始视频的那一路”，是因为每帧的 NvDsFrameMeta 里从一开始就带有 pad_index（和 batch_id），并在整条 pipeline 中保持不变，nvstreamdemux 只是按这两个字段做 1-to-N 的分路。**

---

## 4. Pad Probe：`Gst.PadProbeType` 含义

本示例在 pgie 的 src pad 上添加了 probe：

```python
pgie_src_pad.add_probe(Gst.PadProbeType.BUFFER, pgie_src_pad_buffer_probe, 0)
```

- **第一个参数** `Gst.PadProbeType.BUFFER` 是**触发类型掩码**：表示“当有 **GstBuffer**（数据块）通过该 pad 时，才调用后面的回调”。  
  这样回调里就可以用 `info.get_buffer()` 拿到当前这块 buffer，并用 `pyds.gst_buffer_get_nvds_batch_meta()` 取到 NvDs 的 batch 元数据，做统计或修改。

- **第二个参数**是回调函数，**第三个参数**是传给回调的 user_data（这里为 `0`）。

- 多种触发类型可以用**按位或**组合，例如 `Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM` 表示“buffer 或下游 event 经过时都触发”。

### 4.1 `Gst.PadProbeType` 各类型一览

（对应 GStreamer 的 `GstPadProbeType`，PyGObject 中为 `Gst.PadProbeType`。）

| 类型 | 含义 |
|------|------|
| **INVALID** | 无效，不使用。 |
| **IDLE** | 在 pad **空闲**时触发；若当前已空闲则立即调用。会**阻塞** pad，直到回调返回。常用于动态改 pipeline（如重连）前先等数据流停。 |
| **BLOCK** | **阻塞** pad：数据流经时先进入回调，可决定是否放行。与 IDLE 一样属于“阻塞型” probe。 |
| **BUFFER** | 当 **GstBuffer**（媒体数据块）经过该 pad 时触发。本示例用此类型在 pgie 输出端统计每帧检测结果、FPS。 |
| **BUFFER_LIST** | 当 **GstBufferList**（buffer 列表）经过时触发。 |
| **EVENT_DOWNSTREAM** | **下游方向**的 **GstEvent**（如 EOS、caps、flush）经过时触发。 |
| **EVENT_UPSTREAM** | **上游方向**的 **GstEvent** 经过时触发。 |
| **EVENT_FLUSH** | **Flush 相关事件**经过时触发。需显式启用，不包含在 EVENT_DOWNSTREAM/UPSTREAM 里。 |
| **QUERY_DOWNSTREAM** | **下游方向**的 **GstQuery**（如 caps、allocation）经过时触发。 |
| **QUERY_UPSTREAM** | **上游方向**的 **GstQuery** 经过时触发。 |
| **PUSH** | 在 **push 模式**下数据被推时触发（与调度方式相关）。 |
| **PULL** | 在 **pull 模式**下数据被拉时触发。 |
| **BLOCKING** | IDLE \| BLOCK：在“下一次有机会时”阻塞并触发（可能是数据流经或 pad 变空闲）。 |
| **DATA_DOWNSTREAM** | 组合：BUFFER \| BUFFER_LIST \| EVENT_DOWNSTREAM，即所有**下游数据**（buffer、buffer 列表、下游事件）。 |
| **DATA_UPSTREAM** | 组合：所有**上游数据**（上游事件）。 |
| **DATA_BOTH** | 组合：DATA_DOWNSTREAM \| DATA_UPSTREAM，即双向数据。 |
| **BLOCK_DOWNSTREAM** | 组合：BLOCK \| DATA_DOWNSTREAM，阻塞并探测下游数据。 |
| **BLOCK_UPSTREAM** | 组合：BLOCK \| DATA_UPSTREAM，阻塞并探测上游数据。 |
| **EVENT_BOTH** | 组合：EVENT_DOWNSTREAM \| EVENT_UPSTREAM，双向事件。 |
| **QUERY_BOTH** | 组合：QUERY_DOWNSTREAM \| QUERY_UPSTREAM，双向查询。 |
| **ALL_BOTH** | 组合：DATA_BOTH \| QUERY_BOTH，双向数据与查询。 |
| **SCHEDULING** | 组合：PUSH \| PULL，两种调度模式。 |

### 4.2 小结

- **BUFFER**：只关心“有 buffer 经过”，适合做**每帧统计、读 NvDs 元数据、画框**等，本示例即此用法。
- **EVENT_*** / **QUERY_***：需要拦截或观察 **事件/查询**（如 EOS、caps、flush）时使用。
- **IDLE / BLOCK / BLOCK_***：需要**暂停数据流**或在**空闲时**做操作时使用；回调会阻塞 pad，直到返回。
- 多种类型可**按位或**组合，例如同时探测 buffer 和下游事件：`Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM`。
