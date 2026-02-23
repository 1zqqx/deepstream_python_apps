#include "include/ctmeta_binding.hpp"

#include "bind_string_property_definitions.h"
#include "include/ct_docs.hpp"

/// @brief
namespace pyds_usbcamera_test {

CTFaceObjectMeta* copy_custom_struct(void* data, void* user_data) {
    NvDsUserMeta* srcMeta = (NvDsUserMeta*)data;
    CTFaceObjectMeta* srcData = (CTFaceObjectMeta*)srcMeta->user_meta_data;
    CTFaceObjectMeta* destData = (CTFaceObjectMeta*)g_malloc0(sizeof(CTFaceObjectMeta));

    destData->confidence = srcData->confidence;
    destData->frameId = srcData->frameId;
    destData->sensorid = srcData->sensorid;
    destData->bbox = srcData->bbox;

    if (srcData->id != nullptr) {
        destData->id = g_strdup(srcData->id);
    }
    if (srcData->name != nullptr) {
        destData->name = g_strdup(srcData->name);
    }
    if (srcData->ts != nullptr) {
        destData->ts = g_strdup(srcData->ts);
    }
    if (srcData->objectId != nullptr) {
        destData->objectId = g_strdup(srcData->objectId);
    }
    if (srcData->sensorStr != nullptr) {
        destData->sensorStr = g_strdup(srcData->sensorStr);
    }

    return destData;
}

void release_custom_struct(void* data, void* user_data) {
    NvDsUserMeta* srcMeta = (NvDsUserMeta*)data;
    if (srcMeta != nullptr) {
        CTFaceObjectMeta* srcData = (CTFaceObjectMeta*)srcMeta->user_meta_data;
        if (srcData != nullptr) {
            if (srcData->id != nullptr) {
                g_free(srcData->id);
                srcData->id = nullptr;
            }
            if (srcData->name != nullptr) {
                g_free(srcData->name);
                srcData->name = nullptr;
            }
            if (srcData->ts != nullptr) {
                g_free(srcData->ts);
                srcData->ts = nullptr;
            }
            if (srcData->objectId != nullptr) {
                g_free(srcData->objectId);
                srcData->objectId = nullptr;
            }
            if (srcData->sensorStr != nullptr) {
                g_free(srcData->sensorStr);
                srcData->sensorStr = nullptr;
            }
            g_free(srcData);
            srcMeta->user_meta_data = nullptr;
        }
    }
}

void ct_face_obj_bind(py::module& m) {
    /* DsCustomBindDataTestStruct bindings to be used with NvDsUserMeta */
    // 修改 TODO pydsdoc::custom::CTFaceObjectMetaDoc
    py::class_<CTFaceObjectMeta>(m, "CTFaceObjectMeta",
                                 ctdocs::CTStruct::CTFaceObjectMetaDoc::descr)
        .def(py::init<>())
        .def_property("id", STRING_PROPERTY(CTFaceObjectMeta, id))
        .def_property("name", STRING_PROPERTY(CTFaceObjectMeta, name))
        .def_readwrite("confidence", &CTFaceObjectMeta::confidence)
        .def_readwrite("frameId", &CTFaceObjectMeta::frameId)
        .def_readwrite("sensorid", &CTFaceObjectMeta::sensorid)
        .def_readwrite("bbox", &CTFaceObjectMeta::bbox)
        .def_property("ts", BUFFER_PROPERTY(CTFaceObjectMeta, ts))
        .def_property("objectId", STRING_PROPERTY(CTFaceObjectMeta, objectId))
        .def_property("sensorStr", STRING_PROPERTY(CTFaceObjectMeta, sensorStr))

        .def(
            "cast", [](void* data) { return (CTFaceObjectMeta*)data; },
            py::return_value_policy::reference, ctdocs::CTStruct::CTFaceObjectMetaDoc::cast);

    m.def(
        "alloc_ct_face_obj_struct",
        [](NvDsUserMeta* meta) {
            auto* mem = (CTFaceObjectMeta*)g_malloc0(sizeof(CTFaceObjectMeta));
            meta->base_meta.copy_func = (NvDsMetaCopyFunc)pyds_usbcamera_test::copy_custom_struct;
            meta->base_meta.release_func =
                (NvDsMetaReleaseFunc)pyds_usbcamera_test::release_custom_struct;
            return mem;
        },
        py::return_value_policy::reference, ctdocs::CTMethordDocs::alloc_ct_face_obj_struct);
}
}  // namespace pyds_usbcamera_test
