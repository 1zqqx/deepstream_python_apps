#!/usr/bin/env python3

################################################################################
# SPDX-FileCopyrightText: Copyright (c) 2022 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
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
import os
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # type: ignore

import pyds


def bus_call(bus, message, loop):
    t = message.type
    if t == Gst.MessageType.EOS:
        print("===> End-of-stream", "\n")
        loop.quit()
    elif t == Gst.MessageType.WARNING:
        err, debug = message.parse_warning()
        sys.stdout.write(f"[WARNING] Warning: {err}: {debug}\n")
    elif t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        sys.stderr.write(f"[ERROR] Error: {err}: {debug}\n")
        loop.quit()
    return True


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
    if name.startswith("video/x-h264"):
        print("===> QtDemux video pad added: %s, linking to h264parse\n" % pad_name)
        sinkpad = h264parser.get_static_pad("sink")
        if sinkpad and not sinkpad.is_linked():
            pad.link(sinkpad)
        elif sinkpad and sinkpad.is_linked():
            sys.stderr.write("h264parse sink pad already linked\n")
    elif name.startswith("video/x-h265"):
        sys.stdout.write("===> ignore video/x-h265")
    elif name.startswith("audio"):
        sys.stdout.write("===> ignore audio")


# ------------------------------------------------------------------------------
# Custom Binding 在本样例中的体现:
# 1) 自定义 C++ 结构体 CustomDataStruct(structId / message / sampleInt)通过 PyDS
#    的「自定义绑定」暴露给 Python(见 bindings/src/custom_binding/) , 从而可以在
#    Python 里用 pyds.alloc_custom_struct / pyds.CustomDataStruct.cast 等访问
# 2) 在 streammux 的 src pad 上把该自定义结构「绑定」到每帧的 NvDsUserMeta 上 ,
#    随 GstBuffer 沿 pipeline 传递; 在 fakesink 的 sink pad 上再读取 , 完成「上游
#    挂自定义数据、下游解析同一结构」的 custom binding 流程
# ------------------------------------------------------------------------------
# 访问 Pad Buffer 内存的主要过程(简要):
#   GstBuffer (info.get_buffer) -> hash(buffer) 作为 key -> 取 NvDsBatchMeta
#   -> 加锁 -> 遍历 frame_meta_list -> 每帧上挂 NvDsUserMeta , 其 user_meta_data
#   指向 CustomDataStruct -> 释放锁 下游用同样方式从 buffer 取 batch_meta ,
#   再遍历 frame_user_meta_list 用 CustomDataStruct.cast 解析
# ------------------------------------------------------------------------------
def streammux_src_pad_buffer_probe(pad, info, u_data):
    # ----- 步骤 1:从 Pad 探针拿到当前流经的 GstBuffer -----
    # info 是 Gst.PadProbeInfo , 代表本次 pad 上通过的数据;get_buffer() 取得
    # 承载该次数据的 GstBuffer , 即「访问 pad buffer 内存」的入口对象
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        sys.stderr.write("[ERROR] Unable to get GstBuffer ")
        return None

    # ----- 步骤 2:从 GstBuffer 取得 DeepStream 的批次元数据 -----
    # DeepStream 在 GstBuffer 上挂载 NvDsBatchMeta , C API 用 buffer 指针查找;
    # Python 绑定里用 hash(gst_buffer) 作为 key 查表得到对应的 batch_meta
    # 若该 buffer 不是 DeepStream 分配的(无 batch_meta) , 直接放行
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    if not batch_meta:
        return Gst.PadProbeReturn.OK

    # ----- 步骤 3:对元数据加锁 , 保证多线程下访问安全 -----
    # batch_meta 及其下的 frame_meta/user_meta 可能被其他线程或元素访问 ,
    # 读写前必须 acquire , 用完必须 release , 否则会有竞态或崩溃
    pyds.nvds_acquire_meta_lock(batch_meta)

    # ----- 步骤 4:遍历当前 batch 中的每一帧 -----
    # frame_meta_list 是链表 , 每个节点对应一帧(本样例 batch-size=1 , 通常只有一帧)
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            # 将链表节点的 data 转成 NvDsFrameMeta , 取得帧号等
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            frame_number = frame_meta.frame_num
        except StopIteration:
            continue

        # ----- 步骤 5:从元数据池中申请一块 NvDsUserMeta -----
        # 避免频繁 malloc; user_meta 用来挂载我们自己的 CustomDataStruct
        user_meta = pyds.nvds_acquire_user_meta_from_pool(batch_meta)

        if user_meta:
            print("+++> adding user meta")
            # ----- 步骤 6:分配并填充「自定义结构体」(Custom Binding 的用法) -----
            # alloc_custom_struct 在 C 侧分配 CustomDataStruct , 并与 user_meta 的
            # copy/release 回调关联 , 便于 DeepStream 在复制/释放 buffer 时正确拷贝或释放该内存
            data = pyds.alloc_custom_struct(user_meta)

            data.message = f"test message + {frame_number}"
            # 字符串需经 get_string 转成 C 侧持有的形式 , 以便随 buffer 传递
            data.message = pyds.get_string(data.message)
            data.structId = frame_number
            data.sampleInt = frame_number + 1

            # ----- 步骤 7:把自定义数据挂到 user_meta 并标记类型 -----
            user_meta.user_meta_data = data
            user_meta.base_meta.meta_type = pyds.NvDsMetaType.NVDS_USER_META

            # ----- 步骤 8:将 user_meta 挂到本帧上,随 buffer 流向下游 -----
            pyds.nvds_add_user_meta_to_frame(frame_meta, user_meta)
        else:
            print("failed to acquire user meta")

        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    # ----- 步骤 9:释放元数据锁 -----
    pyds.nvds_release_meta_lock(batch_meta)
    return Gst.PadProbeReturn.OK


# ------------------------------------------------------------------------------
# 下游探针:从同一 GstBuffer 上读取在 streammux 处挂上去的 CustomDataStruct
# 访问路径:GstBuffer -> batch_meta -> frame_meta_list -> frame_user_meta_list
# -> NvDsUserMeta.user_meta_data -> CustomDataStruct.cast(...)(Custom Binding 的读取侧)
# ------------------------------------------------------------------------------
def fake_sink_sink_pad_buffer_probe(pad, info, u_data):
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        sys.stderr.write("[ERROR] Unable to get GstBuffer ")
        return None

    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    if not batch_meta:
        return Gst.PadProbeReturn.OK

    pyds.nvds_acquire_meta_lock(batch_meta)

    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            continue

        # 遍历该帧上挂的所有 user meta , 找出我们挂的 NVDS_USER_META
        l_usr = frame_meta.frame_user_meta_list
        while l_usr is not None:
            try:
                user_meta = pyds.NvDsUserMeta.cast(l_usr.data)
            except StopIteration:
                continue

            if user_meta.base_meta.meta_type == pyds.NvDsMetaType.NVDS_USER_META:
                # Custom Binding:将 user_meta_data 转成 Python 可访问的 CustomDataStruct
                custom_msg_meta = pyds.CustomDataStruct.cast(user_meta.user_meta_data)
                print(
                    f"event msg meta, otherAttrs = {pyds.get_string(custom_msg_meta.message)}"
                )
                print("custom meta structId:: ", custom_msg_meta.structId)
                print("custom meta msg:: ", pyds.get_string(custom_msg_meta.message))
                print("custom meta sampleInt:: ", custom_msg_meta.sampleInt)
            try:
                l_usr = l_usr.next
            except StopIteration:
                break

        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    pyds.nvds_release_meta_lock(batch_meta)
    return Gst.PadProbeReturn.OK


def main(args):
    # Check input arguments
    if len(args) != 2:
        sys.stderr.write("usage: %s <h264 stream file or uri>\n" % args[0])
        sys.exit(1)

    Gst.init(None)

    pipeline = Gst.Pipeline()
    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline")

    source = Gst.ElementFactory.make("filesrc", "file-source")
    if not source:
        sys.stderr.write(" Unable to create Source")

    dec_demux = Gst.ElementFactory.make("qtdemux", "dec_demux")
    if not dec_demux:
        sys.stderr.write(" Unable to create qtdemux")

    h264parser = Gst.ElementFactory.make("h264parse", "h264-parser")
    if not h264parser:
        sys.stderr.write(" Unable to create h264 parser")

    decoder = Gst.ElementFactory.make("nvv4l2decoder", "nvv4l2-decoder")
    if not decoder:
        sys.stderr.write(" Unable to create Nvv4l2 Decoder")

    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    if not streammux:
        sys.stderr.write(" Unable to create NvStreamMux")

    queue = Gst.ElementFactory.make("queue", "queue")
    if not queue:
        sys.stderr.write(" Unable to create queue")
    queue1 = Gst.ElementFactory.make("queue", "queue1")
    if not queue1:
        sys.stderr.write(" Unable to create queue")

    sink = Gst.ElementFactory.make("fakesink", "fakesink")
    if not sink:
        sys.stderr.write(" Unable to create fake sink \n")

    print("===> reading input")
    print("===> Playing file %s " % args[1])
    source.set_property("location", args[1])

    streammux.set_property("width", 1280)
    streammux.set_property("height", 720)
    streammux.set_property("batch-size", 1)

    print("===> Adding elements to Pipeline")
    pipeline.add(source)
    pipeline.add(dec_demux)
    pipeline.add(h264parser)
    pipeline.add(decoder)
    pipeline.add(streammux)
    pipeline.add(queue)
    pipeline.add(queue1)
    pipeline.add(sink)

    print("===> Linking elements in the Pipeline")
    source.link(dec_demux)
    dec_demux.connect("pad-added", demux_pad_added_cb, h264parser)
    h264parser.link(decoder)

    sinkpad = streammux.request_pad_simple("sink_0")
    if not sinkpad:
        sys.stderr.write(" Unable to get the sink pad of streammux")

    srcpad = decoder.get_static_pad("src")
    if not srcpad:
        sys.stderr.write(" Unable to get source pad of decoder(source)")

    srcpad.link(sinkpad)
    streammux.link(queue)
    queue.link(queue1)  # TODO 怎么连了 2 个 queue
    queue1.link(sink)

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    streammux_src_pad = streammux.get_static_pad("src")
    if not streammux_src_pad:
        sys.stderr.write(" Unable to get src pad of streammux")
    streammux_src_pad.add_probe(
        Gst.PadProbeType.BUFFER,
        # 有数据经过 pad 就会调用 回调函数
        streammux_src_pad_buffer_probe,
        0,
    )

    fakesink_sink_pad = sink.get_static_pad("sink")
    if not fakesink_sink_pad:
        sys.stderr.write(" Unable to get sink pad of fakesink")
    fakesink_sink_pad.add_probe(
        Gst.PadProbeType.BUFFER, fake_sink_sink_pad_buffer_probe, 0
    )

    # 将当前 pipeline 的拓扑导出为 Graphviz DOT 文件 , 便于调试/画图
    # 仅当设置了环境变量 GST_DEBUG_DUMP_DOT_DIR 时才会真正写文件 , 例如:
    #   export GST_DEBUG_DUMP_DOT_DIR=/tmp  # 输出文件为 /tmp/graph.dot
    # 生成图:SVG 可缩放更清晰 , PNG 适合贴文档:
    #   dot -Tsvg -o graph.svg /tmp/graph.dot
    #   dot -Tpng -o graph.png /tmp/graph.dot
    # 参数: (要导出的 Gst.Bin, 图中包含的细节级别, 输出文件名不含路径与后缀)
    Gst.debug_bin_to_dot_file(pipeline, Gst.DebugGraphDetails.ALL, "graph")

    print("===> Starting pipeline")
    pipeline.set_state(Gst.State.PLAYING)
    print("===> pipeline playing")

    try:
        loop.run()
    except:
        pass
    # cleanup
    pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    print(f"[DEBUG] sys.argv: {sys.argv}", "\n")
    sys.exit(main(sys.argv))

"""
export GST_DEBUG_DUMP_DOT_DIR=/home/good/wkspace/deepstream-sdk/deepstream_python_apps/apps/deepstream-custom-binding-test/

dot -Tpng -o graph.png graph.dot

python deepstream_custom_binding_test.py \
    /home/good/wkspace/deepstream-sdk/ds8samples/streams/sample_1080p_h264_q20.mp4 \
    >> log.log 2>&1
"""
