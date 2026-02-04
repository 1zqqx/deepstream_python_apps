# deepstream_test_2 Pipeline 数据流通路径

本文档描述 `deepstream_test_2.py` 中 GStreamer pipeline 的数据流与各元件作用。

---

## 1. 整体数据流（两种输入形式）

### 1.1 输入为 MP4/MOV/M4V 时（容器格式，需解复用）

```
┌─────────────┐     ┌───────────┐     ┌─────────────┐     ┌──────────────┐     ┌────────────┐
│  filesrc    │────▶│  qtdemux  │────▶│  h264parse  │────▶│ nvv4l2decoder│────▶│ nvstreammux│
│ (file-source)│     │(dec_qtdemux)│   │(h264-parser)│     │(nvv4l2-decoder)│   │(Stream-muxer)│
└─────────────┘     └───────────┘     └─────────────┘     └──────────────┘     └──────┬─────┘
        │                    │                  ▲                    │                   │
        │ 本地文件路径         │ pad-added 动态连   │                    │ 解码后 NV12       │ sink_0
        │ .mp4/.mov/.m4v      │ 到 h264parse      │                    │ (memory:NVMM)     │
        │                     │ (仅 video/x-h264) │                    │                   │
        ▼                     ▼                   │                    ▼                   ▼
   读文件字节流           解复用出音视频流           │             GPU 解码后帧             批成 batch
                              只连 video pad       │                                      (batch-size=1)
```

### 1.2 输入为裸 H.264 流时（无 demux）

```
┌─────────────┐     ┌─────────────┐     ┌──────────────┐     ┌────────────┐
│  filesrc    │────▶│  h264parse  │────▶│ nvv4l2decoder│────▶│ nvstreammux│
│ (file-source)│     │(h264-parser)│     │(nvv4l2-decoder)│   │(Stream-muxer)│
└─────────────┘     └─────────────┘     └──────────────┘     └──────┬─────┘
        │                    │                    │                   │
        │ 直接 link          │ 编码字节流          │ 解码后 NV12       │ 同上
        ▼                    ▼                    ▼                   ▼
```

---

## 2. 从 streammux 到 sink 的完整路径（两种输入共用）

```
                    ┌────────────┐
                    │ nvstreammux│
                    │(Stream-muxer)
                    └──────┬─────┘
                           │ video/x-raw(memory:NVMM), 1920x1080, batch=1
                           ▼
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 推理与跟踪链                                                                               │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│  ┌─────────┐    ┌──────────┐    ┌─────────┐    ┌─────────┐                               │
│  │  pgie   │───▶│  tracker │───▶│  sgie1  │───▶│  sgie2  │                               │
│  │(primary-│    │(nv_tracker)   │(secondary1)   │(secondary2)                             │
│  │inference)   │            │   │nvinfer) │    │nvinfer) │                               │
│  └─────────┘    └──────────┘    └─────────┘    └────┬────┘                               │
│       │               │               │               │                                   │
│       │ 整帧检测       │ 多目标跟踪     │ 车型分类       │ 车辆类型分类                      │
│       │ 车/人/自行车/路牌 │ track_id    │ (VehicleMake) │ (VehicleTypes)                   │
│       │ bbox+class_id │ 同物体跨帧ID   │ 对每个 bbox crop│ 对同一批 bbox crop                │
└──────────────────────────────────────────────────────────────────────────────────────────┘
                           │ 帧 + 全部 metadata（检测框、track_id、车型、类型）
                           ▼
                    ┌─────────────┐
                    │ nvvideoconvert│
                    │ (convertor)  │
                    └──────┬──────┘
                           │ 格式/色彩转换，如 NV12 → RGBA（供 OSD 绘制）
                           ▼
                    ┌─────────────┐
                    │   nvdsosd   │
                    │(onscreendisplay)
                    └──────┬──────┘
                           │ 在帧上画框、文字、track_id 等（buffer 仍为 RGBA）
                           ▼
                    ┌─────────────┐
                    │    sink     │
                    │nveglglessink│ 或 nv3dsink（按平台）
                    │(nvvideo-    │
                    │ renderer)   │
                    └─────────────┘
                           │ 显示到屏幕
                           ▼
                        [ 显示输出 ]
```

---

## 3. 元件与链接注释表

| 序号 | 元件 (Gst 名称) | 代码中变量 | 作用简述 | 主要输入/输出 |
|------|------------------|------------|----------|----------------|
| 0 | filesrc | source | 从文件读数据 | 输入：文件路径；输出：字节流 |
| 1 | qtdemux | dec_demux | 仅 MP4 路径存在；解复用容器，输出 video/audio pad | 输入：容器字节流；输出：video pad 动态连到 h264parse |
| 2 | h264parse | h264parser | 解析 H.264 流，输出符合解码器要求的 caps | 输入：H.264 字节流；输出：parsed H.264 |
| 3 | nvv4l2decoder | decoder | GPU 硬件解码 H.264 | 输入：parsed H.264；输出：video/x-raw(memory:NVMM), NV12 |
| 4 | nvstreammux | streammux | 多路输入合成 batch（本例 1 路，sink_0） | 输入：decoder src → sink_0；输出：batch 帧，1920x1080 |
| 5 | nvinfer | pgie | 主检测：整帧检测车/人/自行车/路牌 | 输入：NV12 帧；输出：帧 + NvDs 检测 metadata（bbox, class_id） |
| 6 | nvtracker | tracker | 多目标跟踪，为每个检测分配 track_id | 输入：帧 + 检测 metadata；输出：帧 + 检测 + track_id |
| 7 | nvinfer | sgie1 | 二级分类：对“车”做车型（VehicleMake） | 输入：帧 + 物体列表；对每个 bbox crop 推理；输出：帧 + 车型属性 |
| 8 | nvinfer | sgie2 | 二级分类：对“车”做类型（VehicleTypes） | 输入：帧 + 同一批物体；对同一 bbox crop 推理；输出：帧 + 类型属性 |
| 9 | nvvideoconvert | nvvidconv | 色彩/格式转换（如 NV12→RGBA） | 输入：推理链输出的帧格式；输出：OSD/sink 所需格式 |
| 10 | nvdsosd | nvosd | 在画面上绘制框、文字、track_id 等 | 输入：RGBA 帧 + 全部 metadata；输出：带绘制的 RGBA 帧 |
| 11 | nveglglessink / nv3dsink | sink | 显示到窗口/屏幕 | 输入：最终视频帧；输出：无（显示） |

---

## 4. 关键链接说明（代码对应）

```python
# 源 → 解析 → 解码（MP4 时中间多一层 qtdemux，pad-added 连到 h264parse）
if use_demux:
    source.link(dec_demux)
    dec_demux.connect("pad-added", demux_pad_added_cb, h264parser)
else:
    source.link(h264parser)
h264parser.link(decoder)

# 解码器 → streammux（请求 sink_0 再 link）
sinkpad = streammux.request_pad_simple("sink_0")
srcpad = decoder.get_static_pad("src")
srcpad.link(sinkpad)

# 推理与显示链（顺序固定）
streammux.link(pgie)
pgie.link(tracker)
tracker.link(sgie1)
sgie1.link(sgie2)
sgie2.link(nvvidconv)
nvvidconv.link(nvosd)
nvosd.link(sink)
```

- **唯一动态链接**：MP4 时 `qtdemux` 的 `pad-added` 将 video pad 连到 `h264parse` 的 sink。
- **其余均为静态 link**，顺序即数据流顺序。

---

## 5. 配置文件与元件对应关系

| 元件 | 配置文件 | 说明 |
|------|----------|------|
| pgie | dstest2_pgie_config.txt | 主检测模型（如 resnet18_trafficcamnet）、类别数等 |
| sgie1 | dstest2_sgie1_config.txt | 车型分类模型、operate-on-gie-id=1、operate-on-class-ids=0 |
| sgie2 | dstest2_sgie2_config.txt | 车辆类型分类模型、同上 |
| tracker | dstest2_tracker_config.txt | 跟踪库、分辨率、ll-lib-file、ll-config-file 等 |

---

## 6. Probe 插入点（元数据读取）

- **位置**：`nvosd` 的 **sink pad**（`osd_sink_pad_buffer_probe`）。
- **时机**：推理与跟踪已全部完成，OSD 尚未绘制；可安全读取 batch_meta、frame_meta、obj_meta、tracker past frame 等。
- **用途**：示例中用于打印帧号、目标数、车辆/人数，以及 tracker 的 past frame 信息。

---

*对应代码：`apps/deepstream-test2/deepstream_test_2.py`*
