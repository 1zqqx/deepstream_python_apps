#!/usr/bin/env python3
# ==============================================================================
# DeepStream 官方示例: 单路视频的目标检测应用
# 本示例演示如何从 H.264 视频文件中读取,解码,推理,绘制检测框并显示
# 适合作为学习 DeepStream + GStreamer 插件的入门示例
# ==============================================================================

################################################################################
# SPDX-FileCopyrightText: Copyright (c) 2019-2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
################################################################################

import sys

sys.path.append("../")
import os
import gi

# gi 是 PyGObject,用于在 Python 中调用 GObject 库(包括 GStreamer)
# GStreamer 是基于 GObject 的多媒体框架,所有插件都是 GObject
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # GLib: 主循环; Gst: GStreamer 核心
from common.platform_info import PlatformInfo  # 检测平台(如是否为 Jetson,x86)
from common.bus_call import bus_call  # 处理 pipeline 消息(错误,EOS 等)

# pyds: DeepStream Python 绑定,用于访问 NvDsBatchMeta,NvDsFrameMeta 等检测元数据
import pyds


# ------------------------------------------------------------------------------
# 目标类别 ID(与配置文件中 labels.txt 的顺序对应)
# 本示例使用的 resnet18_trafficcamnet 模型可检测 4 类目标
# ------------------------------------------------------------------------------
PGIE_CLASS_ID_VEHICLE = 0  # 车辆
PGIE_CLASS_ID_BICYCLE = 1  # 自行车
PGIE_CLASS_ID_PERSON = 2  # 行人
PGIE_CLASS_ID_ROADSIGN = 3  # 交通标志
MUXER_BATCH_TIMEOUT_USEC = 33000  # 多路复用器批处理超时(微秒),约 33ms


demux_call_idx: int = 1


# ------------------------------------------------------------------------------
# qtdemux 的 pad-added 回调: MP4 解复用后会动态创建 video_0 等 pad
# 需在此回调中将 qtdemux 的 video pad 连接到 h264parse
# ------------------------------------------------------------------------------
def demux_pad_added_cb(demux_element, pad, h264parser):
    # debug
    global demux_call_idx
    print(f"===> demux_call_idx={demux_call_idx}")
    demux_call_idx += 1

    pad_name = pad.get_name()
    caps = pad.get_current_caps()
    if not caps:
        caps = pad.query_caps(None)
    struct = caps.get_structure(0) if caps and caps.get_size() > 0 else None
    name = struct.get_name() if struct else ""

    # 仅连接 video pad,忽略 audio 等 (结构名如 video/x-h264)
    if name.startswith("video"):
        print("===> QtDemux video pad added: %s, linking to h264parse\n" % pad_name)
        sinkpad = h264parser.get_static_pad("sink")
        if sinkpad and not sinkpad.is_linked():
            pad.link(sinkpad)
        elif sinkpad and sinkpad.is_linked():
            sys.stderr.write("h264parse sink pad already linked\n")
    elif name.startswith("audio"):
        print("===> ignore audio")


# ------------------------------------------------------------------------------
# Probe 回调函数: 在 OSD 的 sink pad 上对每一帧 buffer 进行"探测"
# GStreamer 概念: Pad 是元素的"接口",Probe 让你在数据流过时插入自定义逻辑
# 这里我们读取 nvinfer 产生的检测元数据,并添加自定义显示文本
# ------------------------------------------------------------------------------
def osd_sink_pad_buffer_probe(pad, info, u_data):
    frame_number = 0
    num_rects = 0

    # 从 probe 的 info 中获取当前的 GstBuffer(即一帧视频数据)
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("---> Unable to get GstBuffer ")
        return

    # 获取 DeepStream 附加在 buffer 上的批次元数据
    # nvinfer 会在 buffer 上挂载 NvDsBatchMeta,包含所有帧的检测结果
    # 使用兼容函数以支持不同 pyds 版本(含 DeepStream 8.0)
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    # batch_meta = _get_batch_meta(gst_buffer)
    # 遍历 batch 中的每一帧(本示例 batch-size=1,通常只有一帧)
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            # 将链表节点的 data 转换为 NvDsFrameMeta(帧级元数据)
            # cast() 不复制内存,所有权仍在 C 端,Python GC 不会回收
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        # 初始化各类别目标计数器
        obj_counter = {
            PGIE_CLASS_ID_VEHICLE: 0,
            PGIE_CLASS_ID_PERSON: 0,
            PGIE_CLASS_ID_BICYCLE: 0,
            PGIE_CLASS_ID_ROADSIGN: 0,
        }
        frame_number = frame_meta.frame_num
        num_rects = frame_meta.num_obj_meta  # 该帧检测到的目标总数
        # 遍历该帧中的每个检测目标(车辆,行人等)
        l_obj = frame_meta.obj_meta_list
        while l_obj is not None:
            try:
                # 转换为 NvDsObjectMeta,包含类别,置信度,bbox 等
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break
            obj_counter[obj_meta.class_id] += 1
            # 设置检测框边框颜色: RGBA,此处为蓝色,透明度 0.8
            obj_meta.rect_params.border_color.set(0.0, 0.0, 1.0, 0.8)
            try:
                l_obj = l_obj.next
            except StopIteration:
                break

        # 从元数据池获取 DisplayMeta,用于在画面上叠加文字/图形
        # 内存由 C 端管理,下游 nvdsosd 插件会读取并渲染
        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
        display_meta.num_labels = 1  # 我们要显示 1 段文字
        py_nvosd_text_params = display_meta.text_params[0]
        # 设置要显示的文字内容(帧号,目标数,车辆数,人数)
        # pyds 会分配 C 端字符串内存,需用 pyds.get_string() 读取内容
        py_nvosd_text_params.display_text = "Frame Number={} Number of Objects={} Vehicle_count={} Person_count={}".format(
            frame_number,
            num_rects,
            obj_counter[PGIE_CLASS_ID_VEHICLE],
            obj_counter[PGIE_CLASS_ID_PERSON],
        )

        # 文字在画面上的位置(像素偏移)
        py_nvosd_text_params.x_offset = 10
        py_nvosd_text_params.y_offset = 12

        # 字体与颜色设置
        py_nvosd_text_params.font_params.font_name = "Serif"
        py_nvosd_text_params.font_params.font_size = 10
        # 字体颜色: RGBA,此处为白色
        py_nvosd_text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)

        # 启用文字背景色(便于在复杂画面上阅读)
        py_nvosd_text_params.set_bg_clr = 1

        # 背景色: 黑色
        py_nvosd_text_params.text_bg_clr.set(0.0, 0.0, 0.0, 1.0)

        # 打印显示文字(get_string 将 C 指针转为 Python 字符串) console 太多 注释掉了
        # print("===> Show parameter", pyds.get_string(py_nvosd_text_params.display_text))

        # 将 display_meta 挂到 frame_meta 上, nvdsosd 会据此绘制
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    # 返回 OK 表示 probe 处理完毕,buffer 继续向下游传递
    return Gst.PadProbeReturn.OK


# ==============================================================================
# 主函数: 构建 GStreamer Pipeline 并运行
# Pipeline 数据流: 文件 -> 解析 -> 解码 -> 复用 -> 推理 -> 转换 -> OSD -> 显示
# ==============================================================================
def main(args):
    if len(args) != 2:
        sys.stderr.write("usage: %s <media file or uri>\n" % args[0])
        sys.exit(1)

    platform_info = PlatformInfo()
    # 标准 GStreamer 初始化(必须在使用任何 Gst  API 前调用)
    Gst.init(None)

    # ==========================================================================
    # 创建 GStreamer 元素(Element)
    # 每个元素是一个"黑盒"插件,有 source pad(输出)和 sink pad(输入)
    # Pipeline 将多个元素串联成一条数据流
    # ==========================================================================
    print("===> Creating Pipeline \n ")
    pipeline = Gst.Pipeline()  # Pipeline 是最顶层的 bin,容纳并管理所有子元素

    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline \n")
        sys.exit(1)

    # ----- 1. filesrc(GStreamer 标准插件)-----
    # 功能: 从本地文件读取原始字节流
    # 常用属性: location(文件路径)
    print("===> Creating Source \n ")
    source = Gst.ElementFactory.make("filesrc", "file-source")
    if not source:
        sys.stderr.write(" Unable to create Source \n")
        sys.exit(1)

    # ----- 1.5 qtdemux(可选,用于 MP4/MOV 容器)-----
    # MP4/MOV 是容器格式,filesrc 输出的是容器字节流,需先用 qtdemux 解复用提取 H.264 裸流
    # 否则 h264parse 会收到 MP4 头部字节,误当作 H.264 解析,导致 "Broken bit stream" 错误
    input_uri = args[1]
    use_demux = input_uri.lower().endswith((".mp4", ".mov", ".m4v"))
    demux = None
    if use_demux:
        print("===> Creating QtDemux (MP4/MOV container) \n")
        demux = Gst.ElementFactory.make("qtdemux", "qt-demux")
        if not demux:
            sys.stderr.write(" Unable to create qtdemux \n")
            sys.exit(1)

    # ----- 2. h264parse(GStreamer 标准插件)-----
    # 功能: 解析 H.264 裸流,提取 NAL 单元,SPS/PPS 等,输出带时间戳的解析后数据
    # 解码器需要这些元信息才能正确解码
    print("===> Creating H264Parser \n")
    h264parser = Gst.ElementFactory.make("h264parse", "h264-parser")
    if not h264parser:
        sys.stderr.write(" Unable to create h264 parser \n")
        sys.exit(1)

    # ----- 3. nvv4l2decoder(NVIDIA DeepStream 插件)-----
    # 功能: 基于 V4L2 的硬件 H.264/H.265 解码器,在 GPU 上解码,输出 NV12 格式
    # 相比 CPU 解码(如 avdec_h264),大幅降低 CPU 占用并提升吞吐
    print("===> Creating NV Decoder \n")
    decoder = Gst.ElementFactory.make("nvv4l2decoder", "nvv4l2-decoder")
    if not decoder:
        sys.stderr.write(" Unable to create Nvv4l2 Decoder \n")
        sys.exit(1)

    # ----- 4. nvstreammux(NVIDIA DeepStream 插件)-----
    # 功能: 将多路视频流合并成 batch, 供 nvinfer 批量推理
    # 即使单路输入也需要它,因为 nvinfer 期望输入来自 nvstreammux
    # 会做格式转换,批处理,时间戳对齐等
    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    if not streammux:
        sys.stderr.write(" Unable to create NvStreamMux \n")
        sys.exit(1)

    # ----- 5. nvinfer(NVIDIA DeepStream 插件)-----
    # 功能: 使用 TensorRT 运行目标检测/分类,支持 Caffe/UFF/ONNX
    # 通过配置文件指定模型,类别数,批大小等,输出会附带 NvDsObjectMeta
    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    if not pgie:
        sys.stderr.write(" Unable to create pgie \n")
        sys.exit(1)

    # ----- 6. nvvideoconvert(NVIDIA DeepStream 插件)-----
    # 功能: GPU 上的视频格式转换, 如 NV12 -> RGBA
    # nvdsosd 需要 RGBA 格式才能正确绘制检测框和文字
    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "nvconvertor")
    if not nvvidconv:
        sys.stderr.write(" Unable to create nvvidconv \n")
        sys.exit(1)

    # ----- 7. nvdsosd(NVIDIA DeepStream 插件)-----
    # 功能: On-Screen Display, 根据 NvDsObjectMeta/DisplayMeta 在画面上绘制
    # 检测框,类别标签,自定义文字等,输出 RGBA 帧
    nvosd = Gst.ElementFactory.make("nvdsosd", "nvonscreendisplay")
    if not nvosd:
        sys.stderr.write(" Unable to create nvosd \n")
        sys.exit(1)

    # ----- 8. Sink(显示输出)-----
    # nv3dsink: 适用于 Jetson/集成 GPU, 使用 3D 合成器显示
    # nveglglessink: 适用于 x86 + 独立 GPU, 使用 EGL/OpenGL 渲染到窗口
    if platform_info.is_integrated_gpu():
        print("===> Creating nv3dsink \n")
        sink = Gst.ElementFactory.make("nv3dsink", "nv3d-sink")
        if not sink:
            sys.stderr.write(" Unable to create nv3dsink \n")
            sys.exit(1)
    else:
        if platform_info.is_platform_aarch64():
            print("===> Creating nv3dsink \n")
            sink = Gst.ElementFactory.make("nv3dsink", "nv3d-sink")
        else:
            print("===> Creating EGLSink \n")
            sink = Gst.ElementFactory.make("nveglglessink", "nvvideo-renderer")
        if not sink:
            sys.stderr.write(" Unable to create egl sink \n")
            sys.exit(1)

    print("===> Playing file %s " % args[1])
    source.set_property("location", args[1])  # filesrc 要读取的文件路径
    # nvstreammux 属性: 若未使用新版 gst-nvstreammux 则需设置
    if os.environ.get("USE_NEW_NVSTREAMMUX") != "yes":
        streammux.set_property("width", 1920)
        streammux.set_property("height", 1080)
        streammux.set_property("batched-push-timeout", MUXER_BATCH_TIMEOUT_USEC)
    streammux.set_property("batch-size", 1)  # 批大小=1,单路输入
    # nvinfer 的 推理 文件 配置 同目录下的 默认配置文件
    pgie.set_property("config-file-path", "dstest1_pgie_config.txt")

    print("===> Adding elements to Pipeline \n")
    pipeline.add(source)
    if demux:
        pipeline.add(demux)
    pipeline.add(h264parser)
    pipeline.add(decoder)
    pipeline.add(streammux)
    pipeline.add(pgie)
    pipeline.add(nvvidconv)
    pipeline.add(nvosd)
    # Python 的 if/else 不会产生新作用域,只有函数,类,模块会.
    pipeline.add(sink)

    # 按数据流顺序连接元素
    # MP4: filesrc -> qtdemux -(pad-added)-> h264parse -> nvv4l2decoder -> ...
    # 裸流: filesrc -> h264parse -> nvv4l2decoder -> ...
    print("Linking elements in the Pipeline \n")
    if demux:
        demux.connect("pad-added", demux_pad_added_cb, h264parser)
        source.link(demux)
    else:
        source.link(h264parser)
    h264parser.link(decoder)
    # nvstreammux 使用 "request pad": 需主动请求 sink_0,sink_1 等
    # 每个输入流接一个 sink pad,本示例只有一路,用 sink_0
    sinkpad = streammux.request_pad_simple("sink_0")
    if not sinkpad:
        sys.stderr.write(" Unable to get the sink pad of streammux \n")
        sys.exit(1)
    srcpad = decoder.get_static_pad("src")
    if not srcpad:
        sys.stderr.write(" Unable to get source pad of decoder \n")
        sys.exit(1)
    srcpad.link(sinkpad)
    streammux.link(pgie)
    pgie.link(nvvidconv)
    nvvidconv.link(nvosd)
    nvosd.link(sink)

    # 创建 GLib 主循环,用于处理 GStreamer bus 消息(错误,EOS,状态变更等)
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    # connect 传递的参数 arg1: 固定 "message/sync-message"
    bus.connect("message", bus_call, loop)

    # 在 nvosd 的 sink pad 上添加 Buffer Probe
    # 此时 buffer 已流经 nvinfer,附带了完整的检测元数据,我们可读取并添加自定义显示
    osdsinkpad = nvosd.get_static_pad("sink")
    if not osdsinkpad:
        sys.stderr.write(" Unable to get sink pad of nvosd \n")
        sys.exit(1)
    osdsinkpad.add_probe(Gst.PadProbeType.BUFFER, osd_sink_pad_buffer_probe, 0)

    # 启动 pipeline 并运行主循环
    print("===> Starting pipeline \n")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except:
        pass
    # cleanup
    pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    sys.exit(main(sys.argv))


"""
python deepstream_test_1.py /home/good/wkspace/deepstream-sdk/ds8samples/streams/sample_1080p_h264.mp4
"""
