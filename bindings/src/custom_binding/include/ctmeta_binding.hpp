#pragma once

#include "../../../docstrings/customdoc.h"
#include "../../../docstrings/functionsdoc.h"
#include "ctmeta_schema.hpp"
#include "pyds.hpp"

namespace py = pybind11;

namespace pyds_usbcamera_test {
void ct_face_obj_bind(py::module& m);
}