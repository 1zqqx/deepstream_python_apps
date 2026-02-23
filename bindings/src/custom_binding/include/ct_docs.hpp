
#pragma once

namespace ctdocs {
namespace CTStruct {

namespace CTFaceObjectMetaDoc {
// TODO update docs
constexpr const char* descr = R"pyds(
                Holds custom struct data.

                :ivar structId: *int*, ID for this struct.
                :ivar message: *str*, Message embedded in this structure.
                :ivar sampleInt: *int*, Sample int data)pyds";

constexpr const char* cast =
    R"pyds(cast given object/data to :class:`DsCustomBindTestDataStruct`, call pyds.DsCustomBindTestDataStruct.cast(data))pyds";

}  // namespace CTFaceObjectMetaDoc

}  // namespace CTStruct
namespace CTMethordDocs {

constexpr const char* alloc_ct_face_obj_struct = R"pyds(
    alloc_ct_face_obj_struct docs.)pyds";

}
}  // namespace ctdocs