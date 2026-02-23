# Custom payload generator for nvmsgconv: serialize CTFaceObjectMeta from
# frame_user_meta_list (or from NvDsEventMsgMeta.extMsg) to JSON and produce NvDsPayload.
# Build: from bindings/ run: mkdir build && cd build && cmake .. && make
# Then build this target from repo root, e.g.:
#   cd bindings/custom_msg2p_ctface && mkdir build && cd build && cmake -DDS_PATH=/opt/nvidia/deepstream/deepstream .. && make

cmake_minimum_required(VERSION 3.12 FATAL_ERROR)
project(nvds_msg2p_ctface CXX)

set(CMAKE_CXX_STANDARD 17)

set(DS_PATH "/opt/nvidia/deepstream/deepstream")

# set(DS_PATH "${DS_PATH}" CACHE PATH "DeepStream install root")

# nvmsgconv.h may be under sources/includes or sources/libs/nvmsgconv
include_directories(
    ${DS_PATH}/sources/includes
    ${DS_PATH}/sources/libs/nvmsgconv
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/custom_binding/include
)

include_directories(
    include/
    ${DS_PATH}/sources/includes/
    ${DS_PATH}/sources/gst-plugins/gst-nvdspreprocess/include
    include/bind
    include/nvds
    3rdparty/pybind11/include/pybind11/
    3rdparty/pybind11/include/
    /usr/include/python${PYTHON_VERSION}
    /usr/include/gstreamer-1.0
    /usr/include/glib-2.0
    /usr/lib/aarch64-linux-gnu/glib-2.0/include/
    /usr/lib/x86_64-linux-gnu/glib-2.0/include
)

add_library(nvds_msg2p_ctface SHARED
    src/nvds_msg2p_ctface_bind.cpp
)

target_link_libraries(nvds_msg2p_ctface
    glib-2.0
    gstreamer-1.0
)

set_target_properties(nvds_msg2p_ctface PROPERTIES
    PREFIX ""
    OUTPUT_NAME "nvds_msg2p_ctface"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
)

# Optional: copy to a known path for msg2p-lib
# install(TARGETS nvds_msg2p_ctface LIBRARY DESTINATION lib)
