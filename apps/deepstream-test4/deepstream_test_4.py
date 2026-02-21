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
import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # type: ignore
import sys
from optparse import OptionParser
from common.platform_info import PlatformInfo
from common.bus_call import bus_call
from common.utils import long_to_uint64
import pyds

MAX_DISPLAY_LEN = 64
MAX_TIME_STAMP_LEN = 32
PGIE_CLASS_ID_VEHICLE = 0
PGIE_CLASS_ID_BICYCLE = 1
PGIE_CLASS_ID_PERSON = 2
PGIE_CLASS_ID_ROADSIGN = 3
MUXER_OUTPUT_WIDTH = 1920
MUXER_OUTPUT_HEIGHT = 1080
MUXER_BATCH_TIMEOUT_USEC = 33000
input_file = None
schema_type = 0
proto_lib = None
conn_str = "localhost;2181;testTopic"
cfg_file = None
topic = None
no_display = False

PGIE_CONFIG_FILE = "dstest4_pgie_config.txt"
MSCONV_CONFIG_FILE = "dstest4_msgconv_config.txt"

pgie_classes_str = ["Vehicle", "TwoWheeler", "Person", "Roadsign"]


# ------------------------------------------------------------------------------
# qtdemux 的 pad-added 回调: MP4 解复用后会动态创建 video_0 等 pad
# 需在此回调中将 qtdemux 的 video pad 连接到 h264parse
# MP4 是容器格式, filesrc 输出容器字节流, h264parse 期望 H.264 裸流, 否则会 not-negotiated
# ------------------------------------------------------------------------------
def demux_pad_added_cb(demux_element, pad, h264parser):
    pad_name = pad.get_name()
    caps = pad.get_current_caps()
    if not caps:
        caps = pad.query_caps(None)

    struct = caps.get_structure(0) if caps and caps.get_size() > 0 else None
    name = struct.get_name() if struct else ""

    if name.startswith("video/x-h264"):
        print(f"===> QtDemux video pad added: {pad_name}, linking to h264parse\n")
        sinkpad = h264parser.get_static_pad("sink")
        if sinkpad and not sinkpad.is_linked():
            pad.link(sinkpad)
        elif sinkpad and sinkpad.is_linked():
            sys.stderr.write("h264parse sink pad already linked\n")
    elif name.startswith("audio"):
        print("===> ignore audio pad\n")


# custom object
def generate_vehicle_meta(data):
    obj = pyds.NvDsVehicleObject.cast(data)
    obj.type = "sedan"
    obj.color = "blue"
    obj.make = "Bugatti"
    obj.model = "M"
    obj.license = "XX1234"
    obj.region = "CA"
    return obj


def generate_person_meta(data):
    obj = pyds.NvDsPersonObject.cast(data)
    obj.age = 45
    obj.cap = "none"
    obj.hair = "black"
    obj.gender = "male"
    obj.apparel = "formal"
    return obj


# how to attach custom objects
def generate_event_msg_meta(data, class_id):
    meta = pyds.NvDsEventMsgMeta.cast(data)
    meta.sensorId = 0
    meta.placeId = 0
    meta.moduleId = 0
    meta.sensorStr = "sensor-0"
    meta.ts = pyds.alloc_buffer(MAX_TIME_STAMP_LEN + 1)
    pyds.generate_ts_rfc3339(meta.ts, MAX_TIME_STAMP_LEN)

    # This demonstrates how to attach custom objects.
    # Any custom object as per requirement can be generated and attached
    # like NvDsVehicleObject / NvDsPersonObject. Then that object should
    # be handled in payload generator library (nvmsgconv.cpp) accordingly.
    if class_id == PGIE_CLASS_ID_VEHICLE:
        meta.type = pyds.NvDsEventType.NVDS_EVENT_MOVING
        meta.objType = pyds.NvDsObjectType.NVDS_OBJECT_TYPE_VEHICLE
        meta.objClassId = PGIE_CLASS_ID_VEHICLE
        obj = pyds.alloc_nvds_vehicle_object()
        obj = generate_vehicle_meta(obj)
        meta.extMsg = obj
        meta.extMsgSize = sys.getsizeof(pyds.NvDsVehicleObject)
    if class_id == PGIE_CLASS_ID_PERSON:
        meta.type = pyds.NvDsEventType.NVDS_EVENT_ENTRY
        meta.objType = pyds.NvDsObjectType.NVDS_OBJECT_TYPE_PERSON
        meta.objClassId = PGIE_CLASS_ID_PERSON
        obj = pyds.alloc_nvds_person_object()
        obj = generate_person_meta(obj)
        meta.extMsg = obj
        meta.extMsgSize = sys.getsizeof(pyds.NvDsPersonObject)
    return meta


# osd_sink_pad_buffer_probe  will extract metadata received on OSD sink pad
# and update params for drawing rectangle, object information etc.
# IMPORTANT NOTE:
# a) probe() callbacks are synchronous and thus holds the buffer
#    (info.get_buffer()) from traversing the pipeline until user return.
# b) loops inside probe() callback could be costly in python.
#    So users shall optimize according to their use-case.
# a) probe() 回调操作是同步的,因此会锁定缓冲区(即(info.get_buffer())),直到用户返回才会继续处理管道中的数据
# b) probe() 回调内部的循环在 Python 中可能会比较耗时. 因此, 用户应根据其使用场景进行优化.
def osd_sink_pad_buffer_probe(pad, info, u_data):
    frame_number = 0
    # Initializing object counter with 0.
    obj_counter = {
        PGIE_CLASS_ID_VEHICLE: 0,
        PGIE_CLASS_ID_PERSON: 0,
        PGIE_CLASS_ID_BICYCLE: 0,
        PGIE_CLASS_ID_ROADSIGN: 0,
    }
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return

    # Retrieve batch metadata from the gst_buffer
    # Note that pyds.gst_buffer_get_nvds_batch_meta() expects the
    # C address of gst_buffer as input, which is obtained with hash(gst_buffer)
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    if not batch_meta:
        return Gst.PadProbeReturn.OK
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
            continue
        is_first_object = True

        # Short example of attribute access for frame_meta:
        # print("Frame Number is ", frame_meta.frame_num)
        # print("Source id is ", frame_meta.source_id)
        # print("Batch id is ", frame_meta.batch_id)
        # print("Source Frame Width ", frame_meta.source_frame_width)
        # print("Source Frame Height ", frame_meta.source_frame_height)
        # print("Num object meta ", frame_meta.num_obj_meta)

        frame_number = frame_meta.frame_num
        l_obj = frame_meta.obj_meta_list
        # TODO slow -> optimize
        while l_obj is not None:
            try:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                continue

            # Update the object text display
            txt_params = obj_meta.text_params

            # XXX Set display_text. Any existing display_text string will be
            # freed by the bindings module.
            txt_params.display_text = pgie_classes_str[obj_meta.class_id]

            obj_counter[obj_meta.class_id] += 1

            # Font , font-color and font-size
            txt_params.font_params.font_name = "Serif"
            txt_params.font_params.font_size = 14
            # set(red, green, blue, alpha); set to White
            txt_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)

            # Text background color
            txt_params.set_bg_clr = 0
            # set(red, green, blue, alpha); set to Black
            txt_params.text_bg_clr.set(0.0, 0.0, 0.0, 1.0)

            # NOTE Ideally ??? NVDS_EVENT_MSG_META should be attached to buffer by the
            # component implementing detection / recognition logic.
            # Here it demonstrates how to use / attach that meta data.
            if is_first_object and (frame_number % 30) == 0:
                # Frequency of messages to be send will be based on use case.
                # Here message is being sent for first object every 30 frames.

                user_event_meta = pyds.nvds_acquire_user_meta_from_pool(batch_meta)
                if user_event_meta:
                    # Allocating an NvDsEventMsgMeta instance and getting
                    # reference to it. The underlying memory is not manged by
                    # Python so that downstream plugins can access it. Otherwise
                    # the garbage collector will free it when this probe exits.
                    msg_meta = pyds.alloc_nvds_event_msg_meta(user_event_meta)
                    msg_meta.bbox.top = obj_meta.rect_params.top
                    msg_meta.bbox.left = obj_meta.rect_params.left
                    msg_meta.bbox.width = obj_meta.rect_params.width
                    msg_meta.bbox.height = obj_meta.rect_params.height
                    msg_meta.frameId = frame_number
                    msg_meta.trackingId = long_to_uint64(obj_meta.object_id)
                    msg_meta.confidence = obj_meta.confidence
                    msg_meta = generate_event_msg_meta(msg_meta, obj_meta.class_id)

                    user_event_meta.user_meta_data = msg_meta
                    user_event_meta.base_meta.meta_type = (
                        pyds.NvDsMetaType.NVDS_EVENT_MSG_META
                    )
                    pyds.nvds_add_user_meta_to_frame(frame_meta, user_event_meta)
                else:
                    print("Error in attaching event meta to buffer\n")

                # NOTE 示例代码 仅对每帧的 第一个 object 生成 event msg meta
                is_first_object = False

            try:
                l_obj = l_obj.next
            except StopIteration:
                break
        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    if frame_number % 30 == 0:
        # NOTE 多个源这样控制有问题
        print(
            "Frame Number =",
            frame_number,
            "Vehicle Count =",
            obj_counter[PGIE_CLASS_ID_VEHICLE],
            "Person Count =",
            obj_counter[PGIE_CLASS_ID_PERSON],
        )
    return Gst.PadProbeReturn.OK


def main(args):
    platform_info = PlatformInfo()
    Gst.init(None)

    # Deprecated: following meta_copy_func and meta_free_func
    # have been moved to the binding as event_msg_meta_copy_func()
    # and event_msg_meta_release_func() respectively.
    # Hence, registering and unsetting these callbacks in not needed
    # anymore. Please extend the above functions as necessary instead.
    # # registering callbacks
    # pyds.register_user_copyfunc(meta_copy_func)
    # pyds.register_user_releasefunc(meta_free_func)

    print("===> Creating Pipeline \n ")
    pipeline = Gst.Pipeline()
    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline \n")

    print("===> Creating Source \n ")
    source = Gst.ElementFactory.make("filesrc", "file-source")
    if not source:
        sys.stderr.write(" Unable to create Source \n")

    # qtdemux: MP4/MOV 是容器格式, filesrc 输出容器字节流, 需先解复用再给 h264parse
    # 否则 h264parse 收到 MP4 头部会报 not-negotiated
    use_demux = input_file.lower().endswith((".mp4", ".mov", ".m4v"))
    demux = None
    if use_demux:
        print("===> Creating QtDemux (MP4/MOV container) \n")
        demux = Gst.ElementFactory.make("qtdemux", "qt-demux")
        if not demux:
            sys.stderr.write(" Unable to create qtdemux \n")

    print("===> Creating H264Parser \n")
    h264parser = Gst.ElementFactory.make("h264parse", "h264-parser")
    if not h264parser:
        sys.stderr.write(" Unable to create h264 parser \n")

    print("===> Creating Decoder \n")
    decoder = Gst.ElementFactory.make("nvv4l2decoder", "nvv4l2-decoder")
    if not decoder:
        sys.stderr.write(" Unable to create Nvv4l2 Decoder \n")

    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    if not streammux:
        sys.stderr.write(" Unable to create NvStreamMux \n")

    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    if not pgie:
        sys.stderr.write(" Unable to create pgie \n")

    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "convertor")
    if not nvvidconv:
        sys.stderr.write(" Unable to create nvvidconv \n")

    nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    if not nvosd:
        sys.stderr.write(" Unable to create nvosd \n")

    # XXX Transforms buffer meta to schema / payload meta
    msgconv = Gst.ElementFactory.make("nvmsgconv", "nvmsg-converter")
    if not msgconv:
        sys.stderr.write(" Unable to create msgconv \n")

    # XXX Sends payload metadata to remote server
    msgbroker = Gst.ElementFactory.make("nvmsgbroker", "nvmsg-broker")
    if not msgbroker:
        sys.stderr.write(" Unable to create msgbroker \n")

    # 分流
    tee = Gst.ElementFactory.make("tee", "nvsink-tee")
    if not tee:
        sys.stderr.write(" Unable to create tee \n")

    queue1 = Gst.ElementFactory.make("queue", "nvtee-que1")
    if not queue1:
        sys.stderr.write(" Unable to create queue1 \n")

    queue2 = Gst.ElementFactory.make("queue", "nvtee-que2")
    if not queue2:
        sys.stderr.write(" Unable to create queue2 \n")

    if no_display:
        print("===> Creating FakeSink \n")
        sink = Gst.ElementFactory.make("fakesink", "fakesink")
        if not sink:
            sys.stderr.write(" Unable to create fakesink \n")
    else:
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

    print(f"===> Playing file {input_file} ")
    source.set_property("location", input_file)
    streammux.set_property("width", 1920)
    streammux.set_property("height", 1080)
    streammux.set_property("batch-size", 1)
    streammux.set_property("batched-push-timeout", MUXER_BATCH_TIMEOUT_USEC)

    pgie.set_property("config-file-path", PGIE_CONFIG_FILE)

    # XXX set config
    msgconv.set_property("config", MSCONV_CONFIG_FILE)
    msgconv.set_property("payload-type", schema_type)
    msgbroker.set_property("proto-lib", proto_lib)
    msgbroker.set_property("conn-str", conn_str)
    if cfg_file is not None:
        msgbroker.set_property("config", cfg_file)
    if topic is not None:
        msgbroker.set_property("topic", topic)
    msgbroker.set_property("sync", False)  # 同步 False

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
    pipeline.add(tee)
    pipeline.add(queue1)
    pipeline.add(queue2)
    pipeline.add(msgconv)
    pipeline.add(msgbroker)
    pipeline.add(sink)

    # MP4: source -> qtdemux -(pad-added)-> h264parse; 裸流: source -> h264parse
    print("===> Linking elements in the Pipeline \n")
    if demux:
        demux.connect("pad-added", demux_pad_added_cb, h264parser)
        source.link(demux)
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
    pgie.link(nvvidconv)
    nvvidconv.link(nvosd)
    nvosd.link(tee)

    # streammux -> pgie -> nvvidconv -> nvosd -> tee +-->src01(tee_msg_pad)      -> queue1
    #                                                +-->src02(tee_render_pad)   -> queue2
    # queue1 -> msgconv -> msgbroker
    queue1.link(msgconv)
    msgconv.link(msgbroker)
    # queue2 -> video_sink
    queue2.link(sink)
    sink_pad = queue1.get_static_pad("sink")
    tee_msg_pad = tee.request_pad_simple("src_%u")
    tee_render_pad = tee.request_pad_simple("src_%u")
    if not tee_msg_pad or not tee_render_pad:
        sys.stderr.write("Unable to get request pads\n")
    tee_msg_pad.link(sink_pad)
    sink_pad = queue2.get_static_pad("sink")
    tee_render_pad.link(sink_pad)

    # create an event loop and feed gstreamer bus messages to it
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

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

    # pyds.unset_callback_funcs()
    pipeline.set_state(Gst.State.NULL)


# Parse and validate input arguments
def parse_args():
    parser = OptionParser()
    parser.add_option(
        "-c",
        "--cfg-file",
        dest="cfg_file",
        help="Set the adaptor config file. Optional if "
        "connection string has relevant  details.",
        metavar="FILE",
    )
    parser.add_option(
        "-i",
        "--input-file",
        dest="input_file",
        help="Set the input H264 file",
        metavar="FILE",
    )
    parser.add_option(
        "-p",
        "--proto-lib",
        dest="proto_lib",
        help="Absolute path of adaptor library",
        metavar="PATH",
    )
    parser.add_option(
        "",
        "--conn-str",
        dest="conn_str",
        help="Connection string of backend server. Optional if "
        "it is part of config file.",
        metavar="STR",
    )
    parser.add_option(
        "-s",
        "--schema-type",
        dest="schema_type",
        default="0",
        help="Type of message schema (0=Full, 1=minimal), " "default=0",
        metavar="<0|1>",
    )
    parser.add_option(
        "-t",
        "--topic",
        dest="topic",
        help="Name of message topic. Optional if it is part of "
        "connection string or config file.",
        metavar="TOPIC",
    )
    parser.add_option(
        "",
        "--no-display",
        action="store_true",
        dest="no_display",
        default=False,
        help="Disable display",
    )

    options, args = parser.parse_args()
    print(f"[=] options: {options}" f"args: {args}")

    global cfg_file
    global input_file
    global proto_lib
    global conn_str
    global topic
    global schema_type
    global no_display
    cfg_file = options.cfg_file
    input_file = options.input_file
    proto_lib = options.proto_lib
    conn_str = options.conn_str
    topic = options.topic
    no_display = options.no_display

    if not (proto_lib and input_file):
        print(
            "Usage: python3 deepstream_test_4.py -i <H264 filename> -p "
            "<Proto adaptor library> --conn-str=<Connection string>"
        )
        return 1

    schema_type = 0 if options.schema_type == "0" else 1


if __name__ == "__main__":
    ret = parse_args()
    # If argument parsing fails, returns failure (non-zero)
    if ret == 1:
        sys.exit(1)
    sys.exit(main(sys.argv))

"""
python deepstream_test_4.py \
    -c cfg_redis.txt \
    -i /home/good/wkspace/deepstream-sdk/ds8samples/streams/sample_1080p_h264.mp4 \
    -p /opt/nvidia/deepstream/deepstream/lib/libnvds_redis_proto.so \
    -t redis_topic \
    -s 1 \
    --conn-str="127.0.0.1;6399"

-t topic                # Only kafka needs it -> no
-s 0 or 1 -> readme
--conn-str "127.0.0.1;6399"  # Docker: 宿主机端口 6399 -> 容器 6379
--no-display to disable display.
"""

# TODO: 标记代码中需要实现的功能或任务
# FIXME: 标记代码中需要修复的问题或缺陷
# NOTE: 提供额外的注释或提示信息, 帮助理解代码意图或设计决策
# BUG: 标记已知的Bug或错误
# XXX: 标记需要警惕或需要重点关注的代码块
# HACK: 标记临时性修复或不优雅的解决方案

"""
# full message
{
    "messageid": "75205036-04f0-4147-9ebd-6045b4f3ec24",
    "mdsversion": "1.0",
    "@timestamp": "2026-02-21T03:26:33.414Z",
    "place": {
        "id": "1",
        "name": "XYZ",
        "type": "garage",
        "location": {"lat": 30.32, "lon": -40.55, "alt": 100},
        "entrance": {
            "name": "walsh",
            "lane": "lane1",
            "level": "P2",
            "coordinate": {"x": 1, "y": 2, "z": 3},
        },
    },
    "sensor": {
        "id": "CAMERA_ID",
        "type": "Camera",
        "description": '\\"Entrance of Garage Right Lane\\"',
        "location": {"lat": 45.293701447, "lon": -75.8303914499, "alt": 48.1557479338},
        "coordinate": {"x": 5.2, "y": 10.1, "z": 11.2},
    },
    "analyticsModule": {
        "id": "XYZ",
        "description": '\\"Vehicle Detection and License Plate Recognition\\"',
        "source": "OpenALR",
        "version": "1.0",
    },
    "object": {
        "id": "18446744073709551615",
        "speed": 0,
        "direction": 0,
        "orientation": 0,
        "person": {
            "age": 45,
            "gender": "male",
            "hair": "black",
            "cap": "none",
            "apparel": "formal",
            "confidence": 0.499267578125,
        },
        "bbox": {
            "topleftx": 123,
            "toplefty": 485,
            "bottomrightx": 204,
            "bottomrighty": 765,
        },
        "location": {"lat": 0, "lon": 0, "alt": 0},
        "coordinate": {"x": 0, "y": 0, "z": 0},
        "pose": {},
    },
    "event": {"id": "c471e8f5-7acd-4daf-8067-aa2b82d93331", "type": "entry"},
    "videoPath": "",
}
# minimal message
{
    "version": "4.0",
    "id": "0",
    "@timestamp": "2026-02-21T07:29:03.166Z",
    "sensorId": "sensor-0",
    "objects": [
        "18446744073709551615|123.095|485.434|204.537|765.614|Person|#|male|45|black|none|formal|0.499268"
        # id                 |     bbox position             |classify|    | people info...
    ],
}
"""
