# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# NVIDIA CORPORATION & AFFILIATES and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION & AFFILIATES is strictly prohibited.

import math
import contextlib
import os
import sys

import torch
import numpy as np
import wisp._C as _C
from .barycentric_coordinates import barycentric_coordinates
from .sample_tex import sample_tex


def compute_sdf_rgb(
    V: torch.Tensor,
    F: torch.Tensor,
    points: torch.Tensor,
    TV: torch.Tensor,
    TF: torch.Tensor,
    materials,
    split_size : int = 10**6):
    """Computes SDF given point samples and a mesh.

    Args:
        V (torch.FloatTensor): #V, 3 array of vertices
        F (torch.LongTensor): #F, 3 array of indices
        points (torch.FloatTensor): [N, 3] array of points to sample
        TV (torch.FloatTensor):
        TF (torch.FloatTensor):
        materials:
        split_size (int): The batch at which the SDF will be computed. The kernel will break for too large
                          batches; when in doubt use the default.

    Returns:
        (torch.FloatTensor): [N, 1] array of computed SDF values.
        (torch.FloatTensor): [N, 3] array of rgb values
    """
    mesh = V[F]
    TV = TV.to(V.device)
    TF = TF.to(V.device)

    _points = torch.split(points, split_size)
    split_len = len(_points)
    sdfs = []
    rgbs = []

    for split_idx, _p in enumerate(_points):
        # gets sdf and triangle closest to the point _p
        out = _C.external.mesh_to_sdf_triangle_cuda(_p.cuda().contiguous(), mesh.cuda().contiguous())[0]
        out_len = out.shape[0]
        half_len = int(out_len / 2)

        dist = out[:half_len]  # sdf
        hit_tidx = out[half_len:].type(torch.long)  # triangle closest to point _p
        hit_F = F.index_select(dim=0, index=hit_tidx)
        hit_V = V[hit_F]

        # calculate (point on the triangle) which is closest to point _p
        print(f"Processing closest_point_on_triangle()... this may take up few minutes. [{split_idx + 1}/{split_len}]")
        hit_pts = closest_point_on_triangle(mesh.index_select(dim=0, index=hit_tidx).cuda().contiguous(), _p.cuda().contiguous())

        BC = barycentric_coordinates(hit_pts.cuda(), hit_V[:, 0], hit_V[:, 1], hit_V[:, 2])

        hit_TF = TF[hit_tidx]
        hit_TM = hit_TF[..., 3]
        hit_TF = hit_TF[..., :3]

        if TV.shape[0] > 0:
            hit_TV = TV[hit_TF]
            hit_Tp = (hit_TV * BC.unsqueeze(-1)).sum(1)
        else:
            hit_Tp = BC

        rgb = sample_tex(hit_Tp, hit_TM, materials)

        sdfs.append(dist)
        rgbs.append(rgb)

    return torch.cat(sdfs)[..., None], torch.cat(rgbs)


def closest_point_on_triangle(triangles, points):
    """
    The implementation is based on closest_point function of trimesh library (https://github.com/mikedh/trimesh/blob/main/trimesh/triangles.py)
    find mapping between n-th triangle and n-th point.
    number of triangles and number of points should be same.

    Args:
        triangles: (n, 3, 3)
        points: (n, 3)

    Retunrs:
        closest: (n, 3)
          Point on each triangle closest to each point
    """
    device = points.device
    result = torch.zeros_like(points).to(device)
    remain = torch.ones(points.shape[0], dtype=bool).to(device)

    ones = torch.ones(3).to(device)

    a = triangles[:, 0, :]
    b = triangles[:, 1, :]
    c = triangles[:, 2, :]

    ab = b - a
    ac = c - a
    ap = points - a

    # * is dot product
    # @ is matrix multiplication
    d1 = (ab * ap) @ ones  # (10, 3) @ (3) = (10,)
    d2 = (ac * ap) @ ones

    # very small value
    epsilon = torch.finfo(torch.float32).resolution * 100

    is_a = torch.logical_and(d1 < epsilon, d2 < epsilon)
    if any(is_a):
        result[is_a] = a[is_a]
        remain[is_a] = False

    bp = points - b
    d3 = (ab * bp) @ ones
    d4 = (ac * bp) @ ones

    is_b = (d3 > -epsilon) & (d4 <= d3) & remain
    if any(is_b):
        result[is_b] = b[is_b]
        remain[is_b] = False

    vc = (d1 * d4) - (d3 * d2)
    is_ab = ((vc < epsilon) &
             (d1 > -epsilon) &
             (d3 < epsilon) & remain)

    if any(is_ab):
        v = (d1[is_ab] / (d1[is_ab] - d3[is_ab])).reshape((-1, 1))
        result[is_ab] = a[is_ab] + (v * ab[is_ab])
        remain[is_ab] = False

    cp = points - c
    d5 = (ab * cp) @ ones
    d6 = (ac * cp) @ ones
    is_c = (d6 > -epsilon) & (d5 <= d6) & remain
    if any(is_c):
        result[is_c] = c[is_c]
        remain[is_c] = False

    vb = (d5 * d2) - (d1 * d6)
    is_ac = (vb < epsilon) & (d2 > -epsilon) & (d6 < epsilon) & remain
    if any(is_ac):
        w = (d2[is_ac] / (d2[is_ac] - d6[is_ac])).reshape((-1, 1))
        result[is_ac] = a[is_ac] + w * ac[is_ac]
        remain[is_ac] = False

    va = (d3 * d6) - (d5 * d4)
    is_bc = ((va < epsilon) &
             ((d4 - d3) > -epsilon) &
             ((d5 - d6) > -epsilon) & remain)
    if any(is_bc):
        d43 = d4[is_bc] - d3[is_bc]
        w = (d43 / (d43 + (d5[is_bc] - d6[is_bc]))).reshape((-1, 1))
        result[is_bc] = b[is_bc] + w * (c[is_bc] - b[is_bc])
        remain[is_bc] = False

    if any(remain):
        denom = 1.0 / (va[remain] + vb[remain] + vc[remain])
        v = (vb[remain] * denom).reshape((-1, 1))
        w = (vc[remain] * denom).reshape((-1, 1))
        result[remain] = a[remain] + (ab[remain] * v) + (ac[remain] * w)

    return result
