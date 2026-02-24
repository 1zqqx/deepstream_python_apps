/*
 * SPDX-FileCopyrightText: Copyright (c) 2021-2025 NVIDIA CORPORATION & AFFILIATES. All rights
 * reserved. SPDX-License-Identifier: Apache-2.0
 *
 * NvDsCustomMsgInfo / NVDS_CUSTOM_MSG_BLOB: attach custom JSON blob to NvDsFrameMeta
 * for nvmsgconv msg2p-newapi (payload lib includes this in payload).
 */

#include "include/bind_custom_msg_blob.hpp"

#include <string>

#include "pyds.hpp"

namespace py = pybind11;

namespace pydeepstream {

/* Layout compatible with DS SDK NvDsCustomMsgInfo: message (gchar*), len (guint). */
struct NvDsCustomMsgInfoCompat {
    gchar* message;
    guint len;
};

static void* custom_msg_blob_copy_func(void* data, void* user_data) {
    (void)user_data;
    NvDsUserMeta* src = (NvDsUserMeta*)data;
    NvDsCustomMsgInfoCompat* src_info = (NvDsCustomMsgInfoCompat*)src->user_meta_data;
    if (!src_info || !src_info->message) return nullptr;
    NvDsCustomMsgInfoCompat* dest =
        (NvDsCustomMsgInfoCompat*)g_malloc(sizeof(NvDsCustomMsgInfoCompat));
    dest->message = g_strdup(src_info->message);
    dest->len = src_info->len;
    return dest;
}

static void custom_msg_blob_release_func(void* data, void* user_data) {
    (void)user_data;
    NvDsUserMeta* src = (NvDsUserMeta*)data;
    NvDsCustomMsgInfoCompat* info = (NvDsCustomMsgInfoCompat*)src->user_meta_data;
    if (info) {
        g_free(info->message);
        info->message = nullptr;
        info->len = 0;
        g_free(info);
    }
    src->user_meta_data = nullptr;
}

void bind_custom_msg_blob(py::module& m) {
    m.def(
        "nvds_add_custom_msg_blob_to_frame",
        [](NvDsFrameMeta* frame_meta, NvDsBatchMeta* batch_meta, const std::string& json_str) {
            if (!frame_meta || !batch_meta) return;
            NvDsUserMeta* user_meta = nvds_acquire_user_meta_from_pool(batch_meta);
            if (!user_meta) return;
            NvDsCustomMsgInfoCompat* info =
                (NvDsCustomMsgInfoCompat*)g_malloc(sizeof(NvDsCustomMsgInfoCompat));
            info->message = g_strdup(json_str.c_str());
            info->len = (guint)(json_str.size() + 1);
            user_meta->user_meta_data = info;
            user_meta->base_meta.meta_type =
                (NvDsMetaType)(g_quark_from_string((gchar*)"NVDS_CUSTOM_MSG_BLOB") +
                               NVDS_START_USER_META);
            user_meta->base_meta.copy_func = (NvDsMetaCopyFunc)custom_msg_blob_copy_func;
            user_meta->base_meta.release_func = (NvDsMetaReleaseFunc)custom_msg_blob_release_func;
            nvds_add_user_meta_to_frame(frame_meta, user_meta);
        },
        "frame_meta"_a, "batch_meta"_a, "json_str"_a,
        "Attach NVDS_CUSTOM_MSG_BLOB user meta to frame (msg2p-newapi: payload lib attaches this "
        "to payload).");
}

}  // namespace pydeepstream
