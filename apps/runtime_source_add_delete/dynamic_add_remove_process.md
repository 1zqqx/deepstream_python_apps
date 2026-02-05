# DeepStream / GStreamer 动态添加、删除元素 — 详细过程

本文档基于 `deepstream_rt_src_add_del.py` 中的 `add_sources`、`delete_sources`、`stop_release_source`、`create_uridecode_bin`、`cb_newpad` 等函数，总结 **DeepStream/GStreamer 在运行期动态添加、删除管道元素**的完整流程与要点。

---

## 一、总体思路

- **固定管道**：streammux → pgie → tracker → sgie → tiler → nvvideoconvert → nvosd → sink 在启动时一次性创建并 link，运行时**不增删**。
- **动态部分**：只有**源侧**的若干 **source bin**（每个是 `uridecodebin`）可以运行中动态加入或移出；它们通过 **request pad** 与 streammux 的 `sink_0`、`sink_1`、… 动态连接。
- **槽位设计**：最多 `MAX_NUM_SOURCES`（如 4）个“槽位”，每个槽位对应一个 source_id（0～3）；添加 = 选一个未启用的槽位并创建 bin、连到 streammux；删除 = 选一个已启用的槽位，停掉并释放其 bin 与 streammux 的 pad。

下面分“动态添加”和“动态删除”两条线说明。

---

## 二、动态添加元素（源）的详细过程

### 2.1 入口与调度

- **入口**：`main()` 里在管道进入 PLAYING 且状态稳定后，调用一次  
  `GLib.timeout_add_seconds(10, add_sources, g_source_bin_list)`  
  表示 **10 秒后** 第一次执行 `add_sources`。
- **重复执行**：`add_sources` 若返回 **True**，GLib 会再次安排 10 秒后执行；若返回 **False**（例如已达最大源数），则不再安排。因此效果是“每 10 秒加一路源，直到加满为止”。

### 2.2 `add_sources(data)` 流程

| 步骤 | 代码位置 / 行为 | 说明 |
|------|------------------|------|
| 1 | 在 `[0, MAX_NUM_SOURCES)` 中随机选一个 **未启用** 的槽位 `source_id`（`g_source_enabled[source_id] == False`） | 保证不重复使用同一槽位。 |
| 2 | `g_source_enabled[source_id] = True` | 标记该槽位即将被占用。 |
| 3 | 调用 `create_uridecode_bin(source_id, uri)` 创建 source bin | 见下文 2.3。 |
| 4 | `g_source_bin_list[source_id] = source_bin`；`pipeline.add(source_bin)` | 把新 bin 加入**应用侧列表**和 **GstPipeline**，此时尚未与 streammux 连接。 |
| 5 | `g_source_bin_list[source_id].set_state(Gst.State.PLAYING)` | 让新 bin 进入 PLAYING；内部会开始解码并触发 `pad-added`。 |
| 6 | 若返回 ASYNC，可 `get_state(Gst.CLOCK_TIME_NONE)` 等待状态完成 | 与 GStreamer 异步状态切换一致。 |
| 7 | `g_num_sources += 1` | 当前源数 +1。 |
| 8 | 若 `g_num_sources == MAX_NUM_SOURCES`：注册 `delete_sources` 每 10 秒执行一次，并 **return False** | 加满后停止“加源”定时器，改为“删源”定时器。否则 **return True**，10 秒后再执行一次 `add_sources`。 |

### 2.3 `create_uridecode_bin(index, filename)` — 创建“源 bin”

| 步骤 | 代码 / 行为 | 说明 |
|------|-------------|------|
| 1 | `bin = Gst.ElementFactory.make("uridecodebin", bin_name)` | 创建一个 **uridecodebin**，名称如 `source-bin-00`。 |
| 2 | `bin.set_property("uri", filename)` | 设置输入 URI（文件或 RTSP 等）。 |
| 3 | `bin.connect("pad-added", cb_newpad, source_id)` | **关键**：当 uridecodebin 内部解码出视频并产生 pad 时，会触发 `cb_newpad`，在回调里把该 pad 接到 streammux。 |
| 4 | `bin.connect("child-added", decodebin_child_added, source_id)` | 可选：观察内部子元素创建（如 nvv4l2decoder），用于打日志或设置属性。 |
| 5 | `g_source_enabled[index] = True` | 槽位已启用。 |
| 6 | return bin | 返回的 bin 尚未加入 pipeline，由 `add_sources` 负责 `pipeline.add(source_bin)`。 |

### 2.4 `cb_newpad(decodebin, pad, data)` — 把新源接到 streammux

该回调在 **pad-added** 时被调用；`data` 即 `source_id`。

| 步骤 | 代码 / 行为 | 说明 |
|------|-------------|------|
| 1 | 用 `pad.get_current_caps()` 等判断是否为 **video** pad | 通常只连接视频，不连音频。 |
| 2 | `pad_name = "sink_%u" % source_id`（如 `sink_0`、`sink_1`） | streammux 的 request pad 命名规则。 |
| 3 | `sinkpad = streammux.request_pad_simple(pad_name)` | **动态向 streammux 请求一个 sink pad**；streammux 会创建对应 pad。 |
| 4 | `pad.link(sinkpad) == Gst.PadLinkReturn.OK` | 把 uridecodebin 的（解码后）视频 pad **link 到** streammux 的该 sink pad。 |

至此，新源的数据流路径为：**uridecodebin → streammux → 后续固定管道**。  
**要点**：添加元素时，**先 `pipeline.add(bin)` 并 set_state(PLAYING)**，**连接是在 pad-added 回调里通过 request_pad + link 完成的**，而不是在 add 之前手动 link（因为 pad 在 decode 后才出现）。

---

## 三、动态删除元素（源）的详细过程

### 3.1 入口与调度

- **入口**：当 `add_sources` 某次执行后 `g_num_sources == MAX_NUM_SOURCES` 时，会执行  
  `GLib.timeout_add_seconds(10, delete_sources, g_source_bin_list)`  
  表示 **10 秒后** 第一次执行 `delete_sources`。
- **重复执行**：`delete_sources` 若返回 **True**，则每 10 秒再执行；若返回 **False**（例如已无源，或主动退出），则停止。

### 3.2 `delete_sources(data)` 流程

| 步骤 | 代码 / 行为 | 说明 |
|------|-------------|------|
| 1 | 遍历所有槽位，若某路 **已 EOS** 且当前启用，则先对该路调用 `stop_release_source(source_id)` 并置 `g_source_enabled[source_id] = False` | 优先释放“已经播完”的源。 |
| 2 | 若 `g_num_sources == 0`，则 `loop.quit()` 并 **return False** | 没有源了，退出主循环。 |
| 3 | 在 `[0, MAX_NUM_SOURCES)` 中随机选一个 **已启用** 的槽位 `source_id`。 | 本示例用随机选；实际可改为按业务规则选。 |
| 4 | `g_source_enabled[source_id] = False` | 先标记为禁用，再释放，避免重复操作。 |
| 5 | 调用 `stop_release_source(source_id)` | 真正执行“停掉并从管道移除”的逻辑。 |
| 6 | 若 `g_num_sources == 0`，同样 `loop.quit()` 并 **return False**；否则 **return True**。 | 决定是否继续“每 10 秒删一路”。 |

### 3.3 `stop_release_source(source_id)` — 释放单路源（核心）

这是 **GStreamer/DeepStream 动态删除元素** 的标准顺序：状态置 NULL → 发 flush_stop → 释放 request pad → 从 pipeline 移除元素。

| 步骤 | 代码 / 行为 | 说明 |
|------|-------------|------|
| 1 | `g_source_bin_list[source_id].set_state(Gst.State.NULL)` | 把要删除的 source bin 设为 **NULL**，停止拉流、释放内部资源。可能返回 SUCCESS / FAILURE / **ASYNC**。 |
| 2 | **若 SUCCESS**：直接做下面 3～5；**若 ASYNC**：先 `get_state(Gst.CLOCK_TIME_NONE)` 等待状态切完，再做 3～5。 | 保证元素已真正进入 NULL 再动 pad 和 pipeline。 |
| 3 | `pad_name = "sink_%u" % source_id`；`sinkpad = streammux.get_static_pad(pad_name)` | 取得 streammux 上该路对应的 **sink pad**（当初 request 出来的）。 |
| 4 | `sinkpad.send_event(Gst.Event.new_flush_stop(False))` | 向该 pad 发 **flush_stop**，清空该路在 streammux 内的缓冲，避免残留数据影响下游。 |
| 5 | `streammux.release_request_pad(sinkpad)` | **释放** 当初通过 `request_pad_simple` 申请的 pad；streammux 不再接收该路数据。 |
| 6 | `pipeline.remove(g_source_bin_list[source_id])` | 从 **GstPipeline** 中移除该 source bin；GStreamer 会解链并释放 bin。 |
| 7 | `g_num_sources -= 1` | 应用侧源计数减 1。 |

**注意**：`source_id -= 1` 在示例代码里只改了局部变量，对全局逻辑无影响；真正体现“少了一路”的是 `g_num_sources -= 1`。

---

## 四、关键 API 与概念小结

| 操作 | GStreamer/DeepStream 用法 | 说明 |
|------|---------------------------|------|
| **动态添加“源”元素** | 1）`Gst.ElementFactory.make(...)` 创建 bin；2）`pipeline.add(bin)`；3）`bin.set_state(PLAYING)`；4）在 **pad-added** 回调里 `streammux.request_pad_simple("sink_%u")` + `pad.link(sinkpad)` | 源端 pad 往往延迟产生，所以 link 必须在 pad-added 里做。 |
| **动态删除“源”元素** | 1）`bin.set_state(Gst.State.NULL)`（必要时 get_state 等待）；2）`sinkpad.send_event(flush_stop)`；3）`streammux.release_request_pad(sinkpad)`；4）`pipeline.remove(bin)` | 顺序不能颠倒：先停元素，再清缓冲、释 pad，最后从 pipeline 移除。 |
| **request pad** | `streammux.request_pad_simple("sink_0")` 等 | nvstreammux 支持按名称请求 sink pad，用于多路输入。 |
| **release request pad** | `streammux.release_request_pad(sinkpad)` | 必须与 request 成对，否则 streammux 仍认为该路存在。 |
| **flush_stop** | `sinkpad.send_event(Gst.Event.new_flush_stop(False))` | 在释放 pad 前发，避免下游堆积旧数据。 |

---

## 五、流程简图（文字）

**添加一路源：**

```text
add_sources
  → 选未启用槽位 source_id
  → create_uridecode_bin(source_id, uri)  得到 source_bin
  → pipeline.add(source_bin)
  → source_bin.set_state(PLAYING)
       ↓ (内部解码后产生 pad，触发 pad-added)
  → cb_newpad: streammux.request_pad_simple("sink_%u") → pad.link(sinkpad)
  → g_num_sources += 1
  → 若未满则 return True（10 秒后再加一路），否则 return False 并注册 delete_sources
```

**删除一路源：**

```text
delete_sources
  → 可选：先删已 EOS 的源
  → 选已启用槽位 source_id，g_source_enabled[source_id] = False
  → stop_release_source(source_id):
        source_bin.set_state(NULL)  [必要时 get_state 等待]
        sinkpad = streammux.get_static_pad("sink_%u")
        sinkpad.send_event(flush_stop)
        streammux.release_request_pad(sinkpad)
        pipeline.remove(source_bin)
        g_num_sources -= 1
  → 若 g_num_sources==0 则 loop.quit() 并 return False，否则 return True
```

---

*文档基于 `deepstream_rt_src_add_del.py` 中的 add_sources、delete_sources、stop_release_source、create_uridecode_bin、cb_newpad 等函数整理。*
