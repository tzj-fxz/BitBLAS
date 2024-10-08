# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
from bitblas.gpu.matmul_analysis import (
    get_propagate_map,
    get_ladder_stage3_map,
)
from typing import Literal
from tvm import te, IRModule, DataType
from tvm.tir import IndexMap


def select_implementation(
    M: int,
    N: int,
    datatype: Literal["float16", "bfloat16", "int8", "e4m3_float8", "e5m2_float8"] = "float16",
    dequantize_bits: int = -1,
    storage_dtype: Literal["float16", "int8", "uint8", "int32", "uint32"] = "float16",
    propagate_kind: Literal["A", "B"] = "B",
    transpose_matrix: bool = False,
    transform_kind: int = 0,
    target_instruction: Literal["nvidia-mma"] = "nvidia-mma",
):
    if target_instruction != "nvidia-mma":
        raise ValueError("Currently only support nvidia-mma instruction")

    # This is trick to get the basic tile size for the current datatype
    # as for nvidia tensorcore instruction, the basic tile size is 16x16/16x32 for float16/int8
    l = r = 16  # noqa: E741
    if datatype in ["int8", "e4m3_float8", "e5m2_float8"]:
        l, r = 16, 32  # noqa: E741
    intra_index_map, _ = get_propagate_map(
        transpose_matrix, dtype=datatype, matrix_name=propagate_kind)

    ladder_stage3_map, _ = get_ladder_stage3_map(dtype=datatype)

    target_dtype = DataType(datatype)
    scaling_factor = 1
    if dequantize_bits > 0 and dequantize_bits < target_dtype.bits:
        scaling_factor = ((target_dtype.bits // dequantize_bits) * DataType(storage_dtype).bits //
                          target_dtype.bits)
        r = r // scaling_factor
        initial_indices = intra_index_map.initial_indices
        scaling_final_indices = intra_index_map.map_indices(initial_indices[:-1] +
                                                            [initial_indices[-1] * scaling_factor])
        scaling_final_indices = scaling_final_indices[:-1] + [
            scaling_final_indices[-1] // scaling_factor
        ]
        intra_index_map = IndexMap(
            initial_indices,
            scaling_final_indices,
            None,
        )

        initial_indices = ladder_stage3_map.initial_indices
        scaling_final_indices = ladder_stage3_map.map_indices(
            initial_indices[:-1] + [initial_indices[-1] * scaling_factor])
        scaling_final_indices = scaling_final_indices[:-1] + [
            scaling_final_indices[-1] // scaling_factor
        ]
        ladder_stage3_map = IndexMap(
            initial_indices,
            scaling_final_indices,
            None,
        )

    inp = te.placeholder((M, N // scaling_factor), name="inp", dtype=storage_dtype)
    args = [inp]

    assert transform_kind != 0, "Permute only apply when transform_kind >= 1"
    if transform_kind >= 1:
        arg = args[-1]

        inter_warp = te.compute(
            (M // l, (N // scaling_factor) // r, l, r),
            lambda i, j, ii, jj: arg[i * l + ii, j * r + jj],
            name="inter_warp_permutate",
        )
        args.append(inter_warp)
    if transform_kind >= 2:
        arg = args[-1]

        def fcompute(*args):
            warp_i, warp_j = args[-2:]
            spatial_args = args[:-2]
            permutate_i, permutate_j = intra_index_map.map_indices([warp_i, warp_j])
            new_index = (*spatial_args, permutate_i, permutate_j)
            return arg[new_index]

        intra_warp = te.compute(
            (M // l, (N // scaling_factor) // r, l, r),
            fcompute,
            name="intra_warp_permutate",
        )
        args.append(intra_warp)
    if transform_kind >= 3:
        arg = args[-1]

        ladder_stage3_map_inverse = ladder_stage3_map.inverse([l, r])

        def fcompute(*args):
            warp_i, warp_j = args[-2:]
            spatial_args = args[:-2]
            permutate_i, permutate_j = ladder_stage3_map_inverse.map_indices([warp_i, warp_j])
            new_index = (*spatial_args, permutate_i, permutate_j)
            return arg[new_index]

        out = te.compute(
            (M // l, (N // scaling_factor) // r, l, r),
            fcompute,
            name="permutate",
        )
        args.append(out)
    args = [args[0], args[-1]]

    func = te.create_prim_func(args)

    return IRModule.from_expr(func)
