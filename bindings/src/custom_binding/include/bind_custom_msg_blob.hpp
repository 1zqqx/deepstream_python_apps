/*
 * SPDX-FileCopyrightText: Copyright (c) 2021-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Bindings for NVDS_CUSTOM_MSG_BLOB (NvDsCustomMsgInfo) used with nvmsgconv msg2p-newapi.
 */

#pragma once

#include <pybind11/pybind11.h>

namespace py = pybind11;

namespace pydeepstream {
void bind_custom_msg_blob(py::module& m);
}
