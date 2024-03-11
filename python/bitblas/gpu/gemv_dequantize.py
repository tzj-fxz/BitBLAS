# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""A rule for GEMV and DecodeGEMV."""
import re
from functools import reduce
from typing import List, Optional, Union, Dict

from tvm.tir.function import PrimFunc
from tvm import DataType, arith, ir, tir
from tvm.target import Target
import logging
from ..base import (
    normalize_prim_func,
    get_output_blocks,
    get_block,
)
from .base import GPUScheduleRule

logger = logging.getLogger(__name__)


class GEMVWithDequantizeInfo(GPUScheduleRule):
    """A rule for Dequantized GEMV."""

    def sch_inner_reduction_with_config(  # pylint: disable=too-many-locals,too-many-branches,too-many-return-statements
        self,
        func: tir.PrimFunc,
        config,
    ):
        sch = tir.Schedule(func)
        from .intrin.lop3 import get_lop3_intrin_group

        dequantize_info = func.attrs["dequantize_info"]

        def check_dequantize_info(dequantize_info):
            conditions = []
            # currently only support weight only dequantization
            conditions.append(len(dequantize_info) == 1)
            # TODO(@lei) check if the dequantize value name is weight
            return all(conditions)

        if not check_dequantize_info(dequantize_info):
            logger.debug("Dequantize info is not valid")
            return None

        (weight_decode_info,) = list(dequantize_info.values())

        def check_weight_decode_info(weight_decode_info):
            conditions = []
            # check source format in ["int", "fp", "af"]
            conditions.append("source_format" in weight_decode_info)
            conditions.append(
                weight_decode_info["source_format"]["format"]
                in ["uint", "int", "fp", "af"]
            )
            # check source bits in [1, 2, 4, 8]
            conditions.append(
                weight_decode_info["source_format"]["bits"] in [1, 2, 4, 8]
            )
            # check target format in ["float16", "int8"]
            conditions.append("target_format" in weight_decode_info)
            conditions.append(
                weight_decode_info["target_format"] in ["float16", "int8"]
            )
            return all(conditions)

        if not check_weight_decode_info(weight_decode_info):
            logger.debug("Weight Dequantize info is not valid")
            return None

        block_infos = normalize_prim_func(sch)

        if block_infos is None:
            return None

        reduction_block: tir.schedule.BlockRV = None
        for block in block_infos:
            s_loops: List[tir.schedule.LoopRV] = []
            r_loops: List[tir.schedule.LoopRV] = []
            o_loops: List[tir.schedule.LoopRV] = []
            dom_kind = block.dom_kind()
            block = block.block_rv

            if (
                any(
                    [
                        sch.get(loop_rv).thread_binding is not None
                        for loop_rv in sch.get_loops(block)
                    ]
                )
                or len(sch.get_loops(block)) == 0
            ):
                continue

            for loop, iter_type in zip(sch.get_loops(block), dom_kind):
                {"S": s_loops, "R": r_loops, "O": o_loops}[iter_type].append(loop)

            if not s_loops:
                s_loops.append(sch.add_unit_loop(block))
            if len(r_loops) > 0:
                reduction_block = block

        def prod(iterable):
            return reduce(lambda x, y: x * y, iterable, 1)

        def get_vectorize_factor(target_format):
            # coalseced access requires the vectorize factor to be the same as the transaction size
            return config.arch.transaction_size[-1] // DataType(target_format).bits

        vec = get_vectorize_factor(weight_decode_info["target_format"])
        num_warps = int(prod(config.thread))
        warp_size = int(prod(config.reduce_thread))

        block_b = reduction_block
        output_blocks = get_output_blocks(sch, block_infos)
        B_decode_block = get_block(sch, block_infos, weight_decode_info["decode_block"])

        # compute inline
        for block_info in reversed(block_infos):
            block = block_info.block_rv
            if block not in (reduction_block, *output_blocks, B_decode_block):
                sch.compute_inline(block)

        block_decode_B = sch.cache_read(block_b, 1, "local")
        sch.compute_inline(B_decode_block)

        j, k = sch.get_loops(block_b)[-2:]

        # get target dequantize buffer's idx
        def get_idx(weight_decode_info: Dict):
            # for LUT dequantize, the expr is LUT(w), the idx is 1
            # maybe we can use a more general and structual based way
            # to analysis the idx
            if weight_decode_info["source_format"]["format"] == "af":
                return 1
            return 0

        block_shared_local_A = sch.cache_read(block_b, 0, "local")
        block_shared_local_B = sch.cache_read(
            block_decode_B, get_idx(weight_decode_info), "local"
        )
        block_local_C = sch.cache_write(block_b, 0, "local")
        # reverse inline
        if reduction_block != None and reduction_block != output_blocks[0]:
            sch.reverse_compute_inline(output_blocks[0])

        bx, j = sch.split(j, factors=[None, num_warps])
        k, tx, vk = sch.split(k, factors=[None, warp_size, vec])
        # for dp4a/hfma2
        inst_factor = 2 if weight_decode_info["target_format"] == "float16" else 4
        _, vk = sch.split(vk, factors=[None, inst_factor])
        sch.reorder(bx, j, k, tx)

        sch.bind(bx, "blockIdx.x")
        sch.bind(tx, "threadIdx.x")
        sch.bind(j, "threadIdx.y")

        self.block_size = [sch.get(tx).extent, sch.get(j).extent, 1]
        self.grid_size = [sch.get(bx).extent, 1, 1]

        sch.compute_at(block_decode_B, tx, preserve_unit_loops=True)
        sch.compute_at(block_shared_local_A, tx, preserve_unit_loops=True)
        sch.compute_at(block_shared_local_B, tx, preserve_unit_loops=True)
        sch.reverse_compute_at(block_local_C, j, preserve_unit_loops=True)

        block_local_a_v = sch.get_loops(block_shared_local_A)[-1]
        sch.vectorize(block_local_a_v)
        block_local_b_v = sch.get_loops(block_shared_local_B)[-1]
        sch.vectorize(block_local_b_v)
        if (
            "fast_decoding" in weight_decode_info
            and weight_decode_info["fast_decoding"]
        ):
            source_bit = weight_decode_info["source_format"]["bits"]
            out_dtype = weight_decode_info["target_format"]
            intrin_info = get_lop3_intrin_group(
                out_dtype=out_dtype,
                storage_dtype=weight_decode_info["storage_dtype"],
                source_format=weight_decode_info["source_format"]["format"],
                source_bit=source_bit,
                with_scaling=weight_decode_info["with_scaling"],
                with_zeros=weight_decode_info["with_zeros"],
                zeros_type=weight_decode_info["zeros_type"],
            )
            sch.tensorize(sch.get_loops(block_decode_B)[-1], intrin_info["compute"])
            sch.annotate(
                block_b, ann_key="pragma_import_c", ann_val=intrin_info["c_source"]
            )
        return sch

    def apply_config(self, func: PrimFunc, config):
        if any([t > 1 for t in config.reduce_thread]):
            return self.sch_inner_reduction_with_config(func, config)
        else:
            return None