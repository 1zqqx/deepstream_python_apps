#!/usr/bin/env python3

################################################################################
# SPDX-FileCopyrightText: Copyright (c) 2020-2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
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

import argparse
import sys

sys.path.append("../")

# ------------------------------------------------------------------------------
# gi (PyGObject): Python 绑定，用于调用 GObject/GTK/GStreamer 等 C 库
# ------------------------------------------------------------------------------
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")

# GLib: 事件循环、主循环、定时器等基础运行时
# Gst: GStreamer 核心（Pipeline/Element/Pad/Caps/Bus/State 等）
# GstRtspServer: GStreamer RTSP 服务端（RTSPServer/RTSPMediaFactory 等）
from gi.repository import GLib, Gst, GstRtspServer
from common.platform_info import PlatformInfo
from common.bus_call import bus_call

import pyds

PGIE_CLASS_ID_VEHICLE = 0
PGIE_CLASS_ID_BICYCLE = 1
PGIE_CLASS_ID_PERSON = 2
PGIE_CLASS_ID_ROADSIGN = 3
MUXER_BATCH_TIMEOUT_USEC = 33000

# TODO
demux_call_idx: int = 1


# ------------------------------------------------------------------------------
# qtdemux 的 pad-added 回调: MP4 解复用后会动态创建 video_0 等 pad
# 需在此回调中将 qtdemux 的 video pad 连接到 h264/h265 parse
#
# GStreamer 相关:
#   - element.connect("pad-added", callback, user_data): 元素动态创建 pad 时触发
#   - pad: Gst.Pad，代表元素上的输入/输出端点
# ------------------------------------------------------------------------------
def demux_pad_added_cb(demux_element, pad, dec_parser):
    # 参数: demux_element 发出信号的 qtdemux 元素; pad 新创建的 Gst.Pad; dec_parser 要连到的解析器元素
    global demux_call_idx
    print(f"===> demux_call_idx={demux_call_idx}")
    demux_call_idx += 1

    # pad.get_name() -> str: 返回 pad 名称，如 "src_0"、"video_0"
    pad_name = pad.get_name()
    # pad.get_current_caps() -> Gst.Caps or None: 当前协商好的能力集；未协商时可能为 None
    caps = pad.get_current_caps()
    if not caps:
        # pad.query_caps(filter=None) -> Gst.Caps: 查询该 pad 支持的能力集，filter 可限制范围
        caps = pad.query_caps(None)
    # caps.get_structure(index) -> Gst.Structure: 取第 index 个能力结构；get_size() 为结构个数
    struct = caps.get_structure(0) if caps and caps.get_size() > 0 else None
    # struct.get_name() -> str: 能力类型名，如 "video/x-h264"、"video/x-h265"
    name = struct.get_name() if struct else ""

    if name.startswith("video/x-h264"):
        print("===> QtDemux video pad added: %s, linking to h264parse\n" % pad_name)
        # element.get_static_pad(name) -> Gst.Pad or None: 按名称取固定 pad（如 "sink"/"src"）
        sinkpad = dec_parser.get_static_pad("sink")
        # pad.is_linked() -> bool: 该 pad 是否已与对端 pad 连接
        if sinkpad and not sinkpad.is_linked():
            # pad.link(peer_pad) -> Gst.PadLinkReturn: 将两个 pad 连接，协商格式并建立数据路径
            pad.link(sinkpad)
        elif sinkpad and sinkpad.is_linked():
            sys.stderr.write("h264parse sink pad already linked\n")
    elif name.startswith("video/x-h265"):
        print("===> QtDemux video pad added: %s, linking to h265parse\n" % pad_name)
        sinkpad = dec_parser.get_static_pad("sink")
        if sinkpad and not sinkpad.is_linked():
            pad.link(sinkpad)
        elif sinkpad and sinkpad.is_linked():
            sys.stderr.write("h265parse sink pad already linked\n")
    elif name.startswith("audio"):
        print("===> ignore audio")


# ------------------------------------------------------------------------------
# OSD sink pad 上的 buffer probe 回调：每帧经过 nvosd 的 sink 时调用，用于读写元数据并画 OSD
#
# GStreamer 相关:
#   - pad.add_probe(mask, callback, user_data): 在 pad 上安装 probe，mask 指定触发类型
#   - 回调参数: pad 安装 probe 的 Gst.Pad; info 为 Gst.PadProbeInfo（含 get_buffer() 等）; u_data 即 user_data
# ------------------------------------------------------------------------------
def osd_sink_pad_buffer_probe(pad, info, u_data):
    frame_number = 0
    # Intiallizing object counter with 0.
    obj_counter = {
        PGIE_CLASS_ID_VEHICLE: 0,
        PGIE_CLASS_ID_PERSON: 0,
        PGIE_CLASS_ID_BICYCLE: 0,
        PGIE_CLASS_ID_ROADSIGN: 0,
    }
    num_rects = 0

    # info.get_buffer() -> Gst.Buffer or None: 取得当前流过 pad 的 Gst.Buffer（一帧数据）
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return

    # Retrieve batch metadata from the gst_buffer
    # Note that pyds.gst_buffer_get_nvds_batch_meta() expects the
    # C address of gst_buffer as input, which is obtained with hash(gst_buffer)
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            # Note that l_frame.data needs a cast to pyds.NvDsFrameMeta
            # The casting is done by pyds.NvDsFrameMeta.cast()
            # The casting also keeps ownership of the underlying memory
            # in the C code, so the Python garbage collector will leave
            # it alone.
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        frame_number = frame_meta.frame_num
        num_rects = frame_meta.num_obj_meta
        l_obj = frame_meta.obj_meta_list
        while l_obj is not None:
            try:
                # Casting l_obj.data to pyds.NvDsObjectMeta
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break
            obj_counter[obj_meta.class_id] += 1
            try:
                l_obj = l_obj.next
            except StopIteration:
                break

        # Acquiring a display meta object. The memory ownership remains in
        # the C code so downstream plugins can still access it. Otherwise
        # the garbage collector will claim it when this probe function exits.
        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
        display_meta.num_labels = 1
        py_nvosd_text_params = display_meta.text_params[0]
        # Setting display text to be shown on screen
        # Note that the pyds module allocates a buffer for the string, and the
        # memory will not be claimed by the garbage collector.
        # Reading the display_text field here will return the C address of the
        # allocated string. Use pyds.get_string() to get the string content.
        py_nvosd_text_params.display_text = "Frame Number={} Number of Objects={} Vehicle_count={} Person_count={}".format(
            frame_number,
            num_rects,
            obj_counter[PGIE_CLASS_ID_VEHICLE],
            obj_counter[PGIE_CLASS_ID_PERSON],
        )

        # Now set the offsets where the string should appear
        py_nvosd_text_params.x_offset = 10
        py_nvosd_text_params.y_offset = 12

        # Font , font-color and font-size
        py_nvosd_text_params.font_params.font_name = "Serif"
        py_nvosd_text_params.font_params.font_size = 10
        # set(red, green, blue, alpha); set to White
        py_nvosd_text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)

        # Text background color
        py_nvosd_text_params.set_bg_clr = 1
        # set(red, green, blue, alpha); set to Black
        py_nvosd_text_params.text_bg_clr.set(0.0, 0.0, 0.0, 1.0)
        # Using pyds.get_string() to get display_text as string
        print("===> ", pyds.get_string(py_nvosd_text_params.display_text))
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    # Gst.PadProbeReturn: 告诉 pipeline 该 buffer 如何处理
    #   OK: 正常放行；DROP: 丢弃；REMOVE_PROBE: 移除本 probe；HANDLED: 已处理，不再交给其他 probe
    return Gst.PadProbeReturn.OK


def main(args):
    platform_info = PlatformInfo()
    # 1. Standard GStreamer initialization
    Gst.init(None)

    # Create gstreamer elements
    # Create Pipeline element that will form a connection of other elements
    print("===> Creating Pipeline \n ")
    pipeline = Gst.Pipeline()
    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline \n")
        sys.exit(1)

    # Gst.ElementFactory.make(factory_name, name): 按工厂名创建元素
    #   参数: factory_name 如 "filesrc"/"qtdemux"/"h264parse"; name 元素实例名（用于调试）
    #   返回: Gst.Element 或 None（未找到该工厂时）
    print("===> Creating Source \n ")
    source = Gst.ElementFactory.make("filesrc", "file-source")
    if not source:
        sys.stderr.write(" Unable to create Source \n")
        sys.exit(1)

    # 1.5 TODO source -> qtdemux -> h264/5parser
    use_demux = stream_path.lower().endswith((".mp4", ".mov", ".m4v"))
    demux = None
    if use_demux:
        print("===> Creating QtDemux (MP4/MOV container) \n")
        # qtdemux: 解复用 MP4/MOV/M4V 容器，动态创建 video_0/audio_0 等 pad
        demux = Gst.ElementFactory.make("qtdemux", "qt-demux")
        if not demux:
            sys.stderr.write(" Unable to create qtdemux \n")
            sys.exit(1)

    # h264parse/h265parse: 解析 H.264/H.265 码流，定 NAL 边界、设 caps，供解码器使用
    print("===> Creating H264/5Parser \n")
    dec_parse = None
    if codec == "H264":
        dec_parse = Gst.ElementFactory.make("h264parse", "h264-parser")
    elif codec == "H265":
        dec_parse = Gst.ElementFactory.make("h265parse", "h265-parser")
    if not dec_parse:
        sys.stderr.write(" Unable to create h264/5 parser \n")
        sys.exit(1)

    # Use nvdec_h264 for hardware accelerated decode on GPU
    print("===> Creating Decoder \n")
    decoder = Gst.ElementFactory.make("nvv4l2decoder", "nvv4l2-decoder")
    if not decoder:
        sys.stderr.write(" Unable to create Nvv4l2 Decoder \n")
        sys.exit(1)

    # Create nvstreammux instance to form batches from one or more sources.
    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    if not streammux:
        sys.stderr.write(" Unable to create NvStreamMux \n")
        sys.exit(1)

    # Use nvinfer to run inferencing on decoder's output,
    # behaviour of inferencing is set through config file
    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    if not pgie:
        sys.stderr.write(" Unable to create pgie \n")
        sys.exit(1)

    # Use convertor to convert from NV12 to RGBA as required by nvosd
    # osd 在帧上画图时需要使用 RGBA 帧格式
    nvvidconv_beforeosd = Gst.ElementFactory.make(
        "nvvideoconvert", "convertor_beforeosd"
    )
    if not nvvidconv_beforeosd:
        sys.stderr.write(" Unable to create nvvidconv before osd \n")
        sys.exit(1)

    # Create OSD to draw on the converted RGBA buffer
    nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    if not nvosd:
        sys.stderr.write(" Unable to create nvosd \n")
        sys.exit(1)

    # ?
    nvvidconv_postosd = Gst.ElementFactory.make("nvvideoconvert", "convertor_postosd")
    if not nvvidconv_postosd:
        sys.stderr.write(" Unable to create nvvidconv_postosd \n")

    # capsfilter: 限制下游接受的格式；set_property 设置元素属性
    # Gst.Caps.from_string(str): 从字符串解析能力集，如 "video/x-raw, format=I420"
    #   memory:NVMM 表示 GPU 内存，供硬件编码器使用
    caps = Gst.ElementFactory.make("capsfilter", "filter")
    if enc_type == 0:  # hardware encode
        caps.set_property(
            "caps", Gst.Caps.from_string("video/x-raw(memory:NVMM), format=I420")
        )
    else:
        caps.set_property("caps", Gst.Caps.from_string("video/x-raw, format=I420"))

    # Make the encoder
    if codec == "H264":
        if enc_type == 0:
            encoder = Gst.ElementFactory.make("nvv4l2h264enc", "encoder")
            # TODO significant
            encoder.set_property("insert-sps-pps", 1)
        else:
            encoder = Gst.ElementFactory.make("x264enc", "encoder")
        print(
            f"===> Creating H264 Encoder. [{'nvv4l2h264enc' if enc_type == 0 else 'x264enc'}]"
        )
    elif codec == "H265":
        if enc_type == 0:
            encoder = Gst.ElementFactory.make("nvv4l2h265enc", "encoder")
            # H.265 同样需在码流中插入 VPS/SPS/PPS，否则 RTP 解码端会报错；属性名与 H.264 相同
            encoder.set_property("insert-sps-pps", 1)
        else:
            encoder = Gst.ElementFactory.make("x265enc", "encoder")
        print(
            f"===> Creating H265 Encoder. [{'nvv4l2h264enc' if enc_type == 0 else 'x264enc'}]"
        )
    if not encoder:
        sys.stderr.write(" Unable to create encoder")
        sys.exit(1)
    encoder.set_property("bitrate", bitrate)
    if platform_info.is_integrated_gpu() and enc_type == 0:
        encoder.set_property("preset-level", 1)

    # RTP 推流必须让编码器输出 SPS/PPS，否则 ffplay 会报 "non-existing PPS 0 referenced"
    # if codec == "H264" and enc_type == 0:
    #     encoder.set_property("insert-sps-pps", 1)
    #     encoder.set_property("bufapi-version", 1)

    # encoder 与 rtppay 之间加 parse，确保 SPS/PPS 出现在码流中供 rtppay 发送（nvv4l2 可能把参数集放在 caps 里）
    if codec == "H264":
        enc_parse = Gst.ElementFactory.make("h264parse", "h264parse-enc")
        print("===> Creating H264 parse (encoder output)")
    else:
        enc_parse = Gst.ElementFactory.make("h265parse", "h265parse-enc")
        print("===> Creating H265 parse (encoder output)")
    if not enc_parse:
        sys.stderr.write(" Unable to create encoder output parse\n")
        sys.exit(1)
    # -1 = 每个 IDR 前在码流中插入 SPS/PPS，便于 rtppay 检测并随 RTP 发送
    enc_parse.set_property("config-interval", -1)

    # Make the payload-encode video into RTP packets
    if codec == "H264":
        rtppay = Gst.ElementFactory.make("rtph264pay", "rtppay")
        print("===> Creating H264 rtppay")
    elif codec == "H265":
        rtppay = Gst.ElementFactory.make("rtph265pay", "rtppay")
        print("===> Creating H265 rtppay")
    if not rtppay:
        sys.stderr.write(" Unable to create rtppay")
        sys.exit(1)
    # 周期性在 RTP 流中发送 SPS/PPS(H.265 多一个 VPS)，否则 ffplay 会报 "non-existing PPS"
    # 1 = 每 1 秒发一次，保证刚连上的客户端也能很快收到；-1 为每 IDR 前发（依赖码流里已有 SPS/PPS）
    rtppay.set_property("config-interval", 1)

    # Make the UDP sink
    updsink_port_num = 5400
    sink = Gst.ElementFactory.make("udpsink", "udpsink")
    if not sink:
        sys.stderr.write(" Unable to create udpsink")
        sys.exit(1)

    # 组播地址 同网段内 可以访问
    sink.set_property("host", "224.224.255.255")
    sink.set_property("port", updsink_port_num)
    # 关闭异步发送，数据按 pipeline 节奏推送，不额外缓冲。
    sink.set_property("async", False)
    # 开启与 pipeline 时钟同步，避免发送过快或过慢导致时间轴错乱。
    sink.set_property("sync", 1)

    # element.set_property(name, value): 设置元素属性；下面为各元素常用属性
    print("===> Playing file %s " % stream_path)
    source.set_property("location", stream_path)  # filesrc: 文件路径
    streammux.set_property("width", 1920)
    streammux.set_property("height", 1080)
    # 批大小=1,单路输入,与配置文件保持一致
    streammux.set_property("batch-size", 1)
    streammux.set_property("batched-push-timeout", MUXER_BATCH_TIMEOUT_USEC)

    # nvinfer 的 推理 文件 配置 同目录下的 默认配置文件
    pgie.set_property("config-file-path", "dstest1_pgie_config.txt")

    # pipeline.add(element): 将元素加入 pipeline（bin），成为其子元素
    print("===> Adding elements to Pipeline \n")
    pipeline.add(source)
    if demux:
        pipeline.add(demux)
    pipeline.add(dec_parse)
    pipeline.add(decoder)
    pipeline.add(streammux)
    pipeline.add(pgie)
    pipeline.add(nvvidconv_beforeosd)
    pipeline.add(nvosd)
    pipeline.add(nvvidconv_postosd)
    pipeline.add(caps)
    pipeline.add(encoder)
    pipeline.add(enc_parse)
    pipeline.add(rtppay)
    pipeline.add(sink)

    # Link the elements together:
    # file-source -> qtdemux -> h264-parser -> nvh264-decoder ->
    # nvinfer -> nvvidconv_beforeosd -> nvosd -> nvvidconv_postosd ->
    # caps -> encoder -> enc_parse -> rtppay -> udpsink
    # element.connect(signal_name, callback, user_data): 为元素信号绑定回调；"pad-added" 在 qtdemux 动态创建 pad 时触发
    # element.link(dest_element): 按 pad 模板自动选 src/sink pad 并连接
    print("===> Linking elements in the Pipeline \n")
    if demux:
        demux.connect("pad-added", demux_pad_added_cb, dec_parse)
        source.link(demux)
    else:
        source.link(dec_parse)

    dec_parse.link(decoder)
    # bin.request_pad_simple(template_name): 向有时序 pad 的 bin（如 nvstreammux）请求一个 pad；"sink_0" 为第一个 sink
    sinkpad = streammux.request_pad_simple("sink_0")
    if not sinkpad:
        sys.stderr.write(" Unable to get the sink pad of streammux \n")
        sys.exit(1)

    # element.get_static_pad(pad_name): 按名称取固定 pad，如 "src"/"sink"
    srcpad = decoder.get_static_pad("src")
    if not srcpad:
        sys.stderr.write(" Unable to get source pad of decoder \n")
        sys.exit(1)

    # 将 decoder 的 src pad 连接到 streammux 的 sink pad
    srcpad.link(sinkpad)
    streammux.link(pgie)
    pgie.link(nvvidconv_beforeosd)
    nvvidconv_beforeosd.link(nvosd)
    nvosd.link(nvvidconv_postosd)
    nvvidconv_postosd.link(caps)
    caps.link(encoder)
    encoder.link(enc_parse)
    enc_parse.link(rtppay)
    rtppay.link(sink)

    # GLib.MainLoop(): 创建主事件循环，用于处理 GStreamer bus 消息和 GLib 事件
    loop = GLib.MainLoop()
    # pipeline.get_bus() -> Gst.Bus: 取得 pipeline 的消息总线，用于接收 EOS/ERROR/STATE_CHANGED 等
    bus = pipeline.get_bus()
    # bus.add_signal_watch(): 让 bus 在收到消息时发出 "message" 信号（需在主线程处理）
    bus.add_signal_watch()
    # bus.connect("message", callback, user_data): 收到 message 时调用 bus_call，传入 loop 以便 quit
    bus.connect("message", bus_call, loop)

    # ---------- GstRtspServer: RTSP 服务端，让客户端通过 rtsp://host:port/ds-test 拉流 ----------
    rtsp_port_num = 8554

    # GstRtspServer.RTSPServer.new() -> RTSPServer: 创建 RTSP 服务器实例
    server = GstRtspServer.RTSPServer.new()
    # server.props.service: 绑定端口，如 "8554"
    server.props.service = "%d" % rtsp_port_num
    # server.attach(context=None): 将服务器挂到默认 GLib MainContext，开始监听
    server.attach(None)

    # GstRtspServer.RTSPMediaFactory.new() -> RTSPMediaFactory: 创建媒体工厂，描述如何生成媒体流
    factory = GstRtspServer.RTSPMediaFactory.new()
    # factory.set_launch(pipeline_desc): 用 GStreamer 管道描述字符串定义“如何产生流”
    #   此处用 udpsrc 从本机 UDP 端口读取 RTP，再经内部 pay 转为 RTSP 推给客户端
    factory.set_launch(
        '( udpsrc name=pay0 port=%d buffer-size=524288 caps="application/x-rtp, media=video, clock-rate=90000, encoding-name=(string)%s, payload=96 " )'
        % (updsink_port_num, codec)
    )
    # factory.set_shared(True): 多个客户端连接同一 URL 时共享同一路流（不重复拉源）
    factory.set_shared(True)
    # server.get_mount_points().add_factory(uri_path, factory): 将 URL 路径 "/ds-test" 绑定到该工厂
    server.get_mount_points().add_factory("/ds-test", factory)

    print(
        f"\n *** DeepStream: Launched RTSP Streaming at rtsp://localhost:{rtsp_port_num}/ds-test ***\n\n"
    )

    # 在 nvosd 的 sink pad 上挂 probe，在每帧进入 OSD 时读取/写入元数据并画 OSD
    osdsinkpad = nvosd.get_static_pad("sink")
    if not osdsinkpad:
        sys.stderr.write(" Unable to get sink pad of nvosd \n")
        sys.exit(1)

    # pad.add_probe(mask, callback, user_data): 在 pad 上安装 probe
    #   mask: Gst.PadProbeType.BUFFER 表示在 buffer 通过时调用；还有 DOWNSTREAM/UPSTREAM/EVENT 等
    #   callback(pad, Gst.PadProbeInfo, user_data) 需返回 Gst.PadProbeReturn
    osdsinkpad.add_probe(Gst.PadProbeType.BUFFER, osd_sink_pad_buffer_probe, 0)

    # pipeline.set_state(state) -> Gst.StateChangeReturn: 切换 pipeline 状态
    #   Gst.State: NULL/READY/PAUSED/PLAYING；PLAYING 开始推流
    print("===> Starting pipeline \n")
    pipeline.set_state(Gst.State.PLAYING)
    # loop.run(): 进入主循环，处理 bus 消息与 RTSP 请求，直到 bus_call 里调用 loop.quit()
    try:
        loop.run()
    except:
        pass
    # 退出时设为 NULL，释放资源
    pipeline.set_state(Gst.State.NULL)


def parse_args():
    parser = argparse.ArgumentParser(description="RTSP Output Sample Application Help ")
    parser.add_argument(
        "-i", "--input", help="Path to input H264 elementry stream", required=True
    )
    parser.add_argument(
        "-c",
        "--codec",
        default="H264",
        help="RTSP Streaming Codec H264/H265 , default=H264",
        choices=["H264", "H265"],
    )
    parser.add_argument(
        "-b", "--bitrate", default=4000000, help="Set the encoding bitrate ", type=int
    )
    parser.add_argument(
        "-e",
        "--enc_type",
        default=0,
        help="0:Hardware encoder , 1:Software encoder , default=0",
        choices=[0, 1],
        type=int,
    )
    # Check input arguments
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)
    args = parser.parse_args()
    global codec
    global bitrate
    global stream_path
    global enc_type
    codec = args.codec
    bitrate = args.bitrate
    stream_path = args.input
    enc_type = args.enc_type
    return args


if __name__ == "__main__":
    args = parse_args()
    sys.exit(main(args))

"""
python deepstream_test1_rtsp_out.py -i /home/good/wkspace/deepstream-sdk/ds8samples/streams/sample_1080p_h264.mp4 -c H264
python deepstream_test1_rtsp_out.py -i /home/good/wkspace/deepstream-sdk/ds8samples/streams/sample_1080p_h265.mp4 -c H265

# ffplay -protocol_whitelist file,udp,rtp play_udp_h264.sdp # success

# README.md
ffplay rtsp://127.0.0.1:8554/ds-test # success

官方原来的样例代码 因为 h264parse 实际上不支持 h265 视频 deepstream-test1 也不支持 h265 虽然说添加一点点代码就行 同上
"""
