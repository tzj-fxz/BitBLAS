# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import tvm
from tvm.target import Target
from .arch_base import Arch
from typing import List, Dict


# For LLVM Backend, we do not provide the detailed information of the CPU
# As the LLVM backend do not required tuning, just maintain the consistency
class CPU(Arch):
    def __init__(self, target: Target):
        self.target = target
        device = tvm.runtime.cpu(0)
        if not device.exist:
            raise RuntimeError("Cannot find cpu device 0.")
        self.device: tvm.runtime.Device = device
        self.platform: str = "CPU"