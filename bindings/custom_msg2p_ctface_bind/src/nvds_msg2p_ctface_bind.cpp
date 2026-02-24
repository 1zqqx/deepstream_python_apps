/**
 * Custom payload generator for DeepStream nvmsgconv (msg2p-lib).
 * Serializes CTFaceObjectMeta to JSON by reading only frame_user_meta_list
 * (no NvDsEventMsgMeta). Caller must pass NvDsBatchMeta* as the first argument
 * with size=1 (e.g. nvmsgconv with msg2p-newapi or custom build that passes batch).
 *
 * Build: libnvds_msg2p_ctface.so
 * nvmsgconv: payload-type=257 (PAYLOAD_CUSTOM), msg2p-lib=/path/to/libnvds_msg2p_ctface.so
 */

#include <glib.h>
#include <gst/gst.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "gstnvdsmeta.h"
#include "nvdsmeta_schema.h"

/* Match your DS version: nvmsgconv.h or nvmsgconv_mega.h (DS 8.0) */
#if __has_include("nvmsgconv_mega.h")
#include "nvmsgconv_mega.h"
#else
#include "nvmsgconv.h"
#endif

#include "ctmeta_schema.hpp"

static std::string ct_face_object_meta_to_json(const CTFaceObjectMeta* m) {
    if (!m) return "{}";
    auto safe = [](const char* p) { return p ? p : ""; };
    char ts_buf[64] = {};
    if (m->ts) {
        size_t len = strnlen(m->ts, 63);
        if (len) memcpy(ts_buf, m->ts, len);
    }
    char buf[2048];
    int n = snprintf(buf, sizeof(buf),
                     "{\"id\":\"%s\",\"name\":\"%s\",\"confidence\":%.3f,"
                     "\"frameId\":%d,\"sensorid\":%d,"
                     "\"bbox\":{\"left\":%.2f,\"top\":%.2f,\"width\":%.2f,\"height\":%.2f},"
                     "\"ts\":\"%s\",\"objectId\":\"%s\",\"sensorStr\":\"%s\"}",
                     safe(m->id), safe(m->name), m->confidence, m->frameId, m->sensorid,
                     m->bbox.left, m->bbox.top, m->bbox.width, m->bbox.height, ts_buf,
                     safe(m->objectId), safe(m->sensorStr));
    if (n <= 0 || (size_t)n >= sizeof(buf)) return "{}";
    return std::string(buf);
}

static void serialize_frame_user_meta_list(GList* frame_user_meta_list,
                                           std::vector<std::string>* out) {
    for (GList* l = frame_user_meta_list; l != nullptr; l = l->next) {
        NvDsUserMeta* user_meta = static_cast<NvDsUserMeta*>(l->data);
        if (!user_meta || user_meta->base_meta.meta_type != NVDS_USER_META ||
            !user_meta->user_meta_data)
            continue;
        CTFaceObjectMeta* ct = static_cast<CTFaceObjectMeta*>(user_meta->user_meta_data);
        out->push_back(ct_face_object_meta_to_json(ct));
    }
}

static void collect_from_batch_meta(NvDsBatchMeta* batch_meta, std::vector<std::string>* out) {
    if (!batch_meta || !out) return;
    for (GList* l_frame = batch_meta->frame_meta_list; l_frame != nullptr;
         l_frame = l_frame->next) {
        NvDsFrameMeta* frame_meta = static_cast<NvDsFrameMeta*>(l_frame->data);
        if (!frame_meta || !frame_meta->frame_user_meta_list) continue;
        serialize_frame_user_meta_list(frame_meta->frame_user_meta_list, out);
    }
}

/* Build one JSON array string from multiple JSON objects */
static std::string json_array_from_jsons(const std::vector<std::string>& jsons) {
    if (jsons.empty()) return "[]";
    std::string out = "[";
    for (size_t i = 0; i < jsons.size(); i++) {
        if (i) out += ",";
        out += jsons[i];
    }
    out += "]";
    return out;
}

extern "C" {

NvDsMsg2pCtx* nvds_msg2p_ctx_create(const gchar* config_file, NvDsPayloadType type) {
    (void)config_file;
    (void)type;
    return reinterpret_cast<NvDsMsg2pCtx*>(g_malloc0(1));
}

/* Returns one NvDsPayload containing a JSON array of all CTFaceObjectMeta.
 * When size>=1 the plugin passes NvDsEvent[]; we get NvDsBatchMeta* from
 * events[0].metadata (NvDsEventMsgMeta*)->extMsg (8-byte batch pointer set by
 * Python via nvds_event_msg_meta_set_batch_pointer), then collect from
 * frame_user_meta_list. */
NvDsPayload* nvds_msg2p_generate(NvDsMsg2pCtx* ctx, NvDsEvent* events, guint size) {
    (void)ctx;
    std::vector<std::string> jsons;

    if (events && size >= 1) {
        NvDsEventMsgMeta* msg_meta = static_cast<NvDsEventMsgMeta*>(events[0].metadata);
        if (msg_meta && msg_meta->extMsgSize == 8 && msg_meta->extMsg) {
            NvDsBatchMeta* batch = *reinterpret_cast<NvDsBatchMeta**>(msg_meta->extMsg);
            if (batch && batch->frame_meta_list) collect_from_batch_meta(batch, &jsons);
        }
    }

    if (jsons.empty()) return nullptr;
    std::string body = json_array_from_jsons(jsons);
    NvDsPayload* pl = static_cast<NvDsPayload*>(g_malloc0(sizeof(NvDsPayload)));
    pl->payloadSize = body.size() + 1;
    pl->payload = static_cast<char*>(g_malloc(pl->payloadSize));
    memcpy(pl->payload, body.c_str(), pl->payloadSize);
    pl->componentId = 0;
    return pl;
}

void nvds_msg2p_release(NvDsMsg2pCtx* ctx, NvDsPayload* payload) {
    (void)ctx;
    if (!payload) return;
    g_free(payload->payload);
    payload->payload = nullptr;
    payload->payloadSize = 0;
    g_free(payload);
}

void nvds_msg2p_ctx_destroy(NvDsMsg2pCtx* ctx) {
    if (ctx) g_free(ctx);
}

} /* extern "C" */
