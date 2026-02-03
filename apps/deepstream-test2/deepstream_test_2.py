#!/usr/bin/env python3

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
import configparser

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # type:ignore
from common.platform_info import PlatformInfo
from common.bus_call import bus_call

import pyds

PGIE_CLASS_ID_VEHICLE = 0
PGIE_CLASS_ID_BICYCLE = 1
PGIE_CLASS_ID_PERSON = 2
PGIE_CLASS_ID_ROADSIGN = 3
MUXER_BATCH_TIMEOUT_USEC = 33000


demux_call_idx: int = 1


# ------------------------------------------------------------------------------
# qtdemux 的 pad-added 回调: MP4 解复用后会动态创建 video_0 等 pad
# 需在此回调中将 qtdemux 的 video pad 连接到 h264parse
# ------------------------------------------------------------------------------
def demux_pad_added_cb(demux_element, pad, dec_parser):
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

    print(f"+++ DEBUG: qtdemux pad name: [{name}]")

    # 仅连接 video pad,忽略 audio 等 (结构名如 video/x-h264)
    if name.startswith("video/x-h264"):
        print("===> QtDemux video pad added: %s, linking to h264parse\n" % pad_name)
        sinkpad = dec_parser.get_static_pad("sink")
        if sinkpad and not sinkpad.is_linked():
            pad.link(sinkpad)
        elif sinkpad and sinkpad.is_linked():
            sys.stderr.write("h264parse sink pad already linked\n")
    if name.startswith("video/x-h265"):
        print("===> no impl h265")
    elif name.startswith("audio"):
        print("===> ignore audio")


#
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
        # set(red, green, blue, alpha); set to White 取值范围 [0.0, 1.0] float, alpha: 透明度
        py_nvosd_text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)

        # set_bg_clr: 1: True, 0: False; 1 -> 启用背景色
        py_nvosd_text_params.set_bg_clr = 1
        # set(red, green, blue, alpha); set to Black
        py_nvosd_text_params.text_bg_clr.set(0.0, 0.0, 0.0, 1.0)
        # Using pyds.get_string() to get display_text as string
        print(
            "==> display text to frame: ",
            pyds.get_string(py_nvosd_text_params.display_text),
        )
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
        try:
            l_frame = l_frame.next
        except StopIteration:
            break
    # past tracking meta data
    l_user = batch_meta.batch_user_meta_list
    while l_user is not None:
        try:
            # Note that l_user.data needs a cast to pyds.NvDsUserMeta
            # The casting is done by pyds.NvDsUserMeta.cast()
            # The casting also keeps ownership of the underlying memory
            # in the C code, so the Python garbage collector will leave
            # it alone
            user_meta = pyds.NvDsUserMeta.cast(l_user.data)
        except StopIteration:
            break

        if (
            user_meta
            and user_meta.base_meta.meta_type
            == pyds.NvDsMetaType.NVDS_TRACKER_PAST_FRAME_META
        ):
            try:
                # Note that user_meta.user_meta_data needs a cast to pyds.NvDsTargetMiscDataBatch
                # The casting is done by pyds.NvDsTargetMiscDataBatch.cast()
                # The casting also keeps ownership of the underlying memory
                # in the C code, so the Python garbage collector will leave
                # it alone
                pPastDataBatch = pyds.NvDsTargetMiscDataBatch.cast(
                    user_meta.user_meta_data
                )
            except StopIteration:
                break
            for miscDataStream in pyds.NvDsTargetMiscDataBatch.list(pPastDataBatch):
                print("streamId=", miscDataStream.streamID)
                print("surfaceStreamID=", miscDataStream.surfaceStreamID)
                for miscDataObj in pyds.NvDsTargetMiscDataStream.list(miscDataStream):
                    print("numobj=", miscDataObj.numObj)
                    print("uniqueId=", miscDataObj.uniqueId)
                    print("classId=", miscDataObj.classId)
                    print("objLabel=", miscDataObj.objLabel)
                    for miscDataFrame in pyds.NvDsTargetMiscDataObject.list(
                        miscDataObj
                    ):
                        print("frameNum:", miscDataFrame.frameNum)
                        print("tBbox.left:", miscDataFrame.tBbox.left)
                        print("tBbox.width:", miscDataFrame.tBbox.width)
                        print("tBbox.top:", miscDataFrame.tBbox.top)
                        print("tBbox.right:", miscDataFrame.tBbox.height)
                        print("confidence:", miscDataFrame.confidence)
                        print("age:", miscDataFrame.age)
        try:
            l_user = l_user.next
        except StopIteration:
            break
    return Gst.PadProbeReturn.OK


def main(args):
    # Check input arguments
    if len(args) < 2:
        sys.stderr.write(f"usage: {args[0]} <h264_elementary_stream>\n")
        sys.exit(1)

    platform_info = PlatformInfo()
    # Standard GStreamer initialization

    Gst.init(None)

    print("===> Creating Pipeline \n ")
    pipeline = Gst.Pipeline()

    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline \n")
        return 1

    print("===> Creating Source \n ")
    source = Gst.ElementFactory.make("filesrc", "file-source")
    if not source:
        sys.stderr.write(" Unable to create Source \n")
        return 2

    input_uri = args[1]
    print(f"+++ DEBUG: inputuri: {input_uri}")
    use_demux = input_uri.lower().endswith((".mp4", ".mov", ".m4v"))
    dec_demux = None
    if use_demux:
        print("===> Creating QtDemux (MP4/MOV container) \n")
        dec_demux = Gst.ElementFactory.make("qtdemux", "dec_qtdemux")
        if not dec_demux:
            sys.stderr.write(" Unable to create qtdemux parser \n")
            return 2.5

    # Since the data format in the input file is elementary h264 stream,
    # we need a h264parser
    print("===> Creating H264Parser \n")
    h264parser = Gst.ElementFactory.make("h264parse", "h264-parser")
    if not h264parser:
        sys.stderr.write(" Unable to create h264 parser \n")
        return 3

    # Use nvdec_h264 for hardware accelerated decode on GPU
    print("===> Creating Decoder \n")
    decoder = Gst.ElementFactory.make("nvv4l2decoder", "nvv4l2-decoder")
    if not decoder:
        sys.stderr.write(" Unable to create Nvv4l2 Decoder \n")
        return 4

    # Create nvstreammux instance to form batches from one or more sources.
    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    if not streammux:
        sys.stderr.write(" Unable to create NvStreamMux \n")
        return 5

    # Use nvinfer to run inferencing on decoder's output,
    # behaviour of inferencing is set through config file
    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    if not pgie:
        sys.stderr.write(" Unable to create pgie \n")
        return 6

    # nvtracker
    tracker = Gst.ElementFactory.make("nvtracker", "nv_tracker")
    if not tracker:
        sys.stderr.write(" Unable to create tracker \n")
        return 7

    # 两个不同的二级分类任务
    sgie1 = Gst.ElementFactory.make("nvinfer", "secondary1-nvinference-engine")
    if not sgie1:
        sys.stderr.write(" Unable to make sgie1 \n")
        return 8

    sgie2 = Gst.ElementFactory.make("nvinfer", "secondary2-nvinference-engine")
    if not sgie2:
        sys.stderr.write(" Unable to make sgie2 \n")
        return 9

    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "convertor")
    if not nvvidconv:
        sys.stderr.write(" Unable to create nvvidconv \n")
        return 10

    # Create OSD to draw on the converted RGBA buffer
    nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    if not nvosd:
        sys.stderr.write(" Unable to create nvosd \n")
        return 11

    # Finally render the osd output
    if platform_info.is_integrated_gpu():
        print("Creating nv3dsink \n")
        sink = Gst.ElementFactory.make("nv3dsink", "nv3d-sink")
        if not sink:
            sys.stderr.write(" Unable to create nv3dsink \n")
    else:
        if platform_info.is_platform_aarch64():
            print("Creating nv3dsink \n")
            sink = Gst.ElementFactory.make("nv3dsink", "nv3d-sink")
        else:
            print("===> Creating EGLSink \n")
            sink = Gst.ElementFactory.make("nveglglessink", "nvvideo-renderer")
        if not sink:
            sys.stderr.write(" Unable to create egl sink \n")
            return 12

    print(f"===> Playing file {args[1]}")
    source.set_property("location", args[1])
    streammux.set_property("width", 1920)
    streammux.set_property("height", 1080)
    streammux.set_property("batch-size", 1)
    streammux.set_property("batched-push-timeout", MUXER_BATCH_TIMEOUT_USEC)

    # Set properties of pgie(primary gie) and sgie(secondary gie)
    pgie.set_property("config-file-path", "dstest2_pgie_config.txt")
    sgie1.set_property("config-file-path", "dstest2_sgie1_config.txt")
    sgie2.set_property("config-file-path", "dstest2_sgie2_config.txt")

    # Set properties of tracker
    config = configparser.ConfigParser()
    config.read("dstest2_tracker_config.txt")
    # config.sections()
    print(f"+++ DEBUG: ")
    for sec in config.sections():
        print(f"+++ \t [{sec}]")
        for k, v in config.items(sec):
            print(f"+++ \t\t {k}={v}")
    for key in config["tracker"]:
        if key == "tracker-width":
            tracker_width = config.getint("tracker", key)
            tracker.set_property("tracker-width", tracker_width)
        if key == "tracker-height":
            tracker_height = config.getint("tracker", key)
            tracker.set_property("tracker-height", tracker_height)
        if key == "gpu-id":
            tracker_gpu_id = config.getint("tracker", key)
            tracker.set_property("gpu_id", tracker_gpu_id)
        if key == "ll-lib-file":
            tracker_ll_lib_file = config.get("tracker", key)
            # 依赖库 路径
            tracker.set_property("ll-lib-file", tracker_ll_lib_file)
        if key == "ll-config-file":
            tracker_ll_config_file = config.get("tracker", key)
            # 配置文件路径
            tracker.set_property("ll-config-file", tracker_ll_config_file)

    print("===> Adding elements to Pipeline \n")
    pipeline.add(source)
    if use_demux:
        pipeline.add(dec_demux)
    pipeline.add(h264parser)
    pipeline.add(decoder)
    pipeline.add(streammux)
    pipeline.add(pgie)
    pipeline.add(tracker)
    pipeline.add(sgie1)
    pipeline.add(sgie2)
    pipeline.add(nvvidconv)
    pipeline.add(nvosd)
    pipeline.add(sink)

    # we link the elements together
    # file-source -> [qtdemux ->] h264-parser -> nvh264-decoder ->
    # nvinfer -> nvvidconv -> nvosd -> video-renderer
    print("===> Linking elements in the Pipeline \n")
    if use_demux and dec_demux:
        # MP4/MOV: source -> qtdemux；qtdemux 的 video pad 在 pad-added 里再连到 h264parse
        source.link(dec_demux)
        dec_demux.connect("pad-added", demux_pad_added_cb, h264parser)
    else:
        source.link(h264parser)
    h264parser.link(decoder)

    sinkpad = streammux.request_pad_simple("sink_0")
    if not sinkpad:
        sys.stderr.write(" Unable to get the sink pad of streammux \n")
    srcpad = decoder.get_static_pad("src")
    if not srcpad:
        sys.stderr.write(" Unable to get source pad of decoder \n")
    srcpad.link(sinkpad)
    streammux.link(pgie)
    pgie.link(tracker)
    tracker.link(sgie1)
    sgie1.link(sgie2)
    sgie2.link(nvvidconv)
    nvvidconv.link(nvosd)
    nvosd.link(sink)

    # create and event loop and feed gstreamer bus mesages to it
    loop = GLib.MainLoop()

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    # Lets add probe to get informed of the meta data generated, we add probe to
    # the sink pad of the osd element, since by that time, the buffer would have
    # had got all the metadata.
    osdsinkpad = nvosd.get_static_pad("sink")
    if not osdsinkpad:
        sys.stderr.write(" Unable to get sink pad of nvosd \n")
    osdsinkpad.add_probe(Gst.PadProbeType.BUFFER, osd_sink_pad_buffer_probe, 0)

    print("===> Starting pipeline \n")

    # start play back and listed to events
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
python deepstream_test_2.py /home/good/wkspace/deepstream-sdk/ds8samples/streams/sample_1080p_h264.mp4

# 其中一帧的 结果

Frame Number=16 Number of Objects=12 Vehicle_count=8 Person_count=4
streamId= 0
surfaceStreamID= 0
numobj= 7
uniqueId= 8
classId= 0
objLabel= car
frameNum: 9
tBbox.left: 526.3306884765625
tBbox.width: 28.282470703125
tBbox.top: 460.0474548339844
tBbox.right: 22.20534324645996
confidence: 0.6493862867355347
age: 10
frameNum: 10
tBbox.left: 526.8300170898438
tBbox.width: 28.282470703125
tBbox.top: 460.2555236816406
tBbox.right: 22.20534324645996
confidence: 0.6633400917053223
age: 11
frameNum: 11
tBbox.left: 527.1663208007812
tBbox.width: 28.282470703125
tBbox.top: 460.4815979003906
tBbox.right: 22.20534324645996
confidence: 0.6591640114784241
age: 12
frameNum: 12
tBbox.left: 527.4248046875
tBbox.width: 28.282470703125
tBbox.top: 460.6465759277344
tBbox.right: 22.20534324645996
confidence: 0.7213414907455444
age: 13
frameNum: 13
tBbox.left: 527.577880859375
tBbox.width: 28.282470703125
tBbox.top: 460.7742004394531
tBbox.right: 22.20534324645996
confidence: 0.77042555809021
age: 14
frameNum: 14
tBbox.left: 527.6148681640625
tBbox.width: 28.282470703125
tBbox.top: 460.8724670410156
tBbox.right: 22.20534324645996
confidence: 0.8177959322929382
age: 15
frameNum: 15
tBbox.left: 527.6775512695312
tBbox.width: 28.282470703125
tBbox.top: 460.9528503417969
tBbox.right: 22.20534324645996
confidence: 0.8134097456932068
age: 16
"""
