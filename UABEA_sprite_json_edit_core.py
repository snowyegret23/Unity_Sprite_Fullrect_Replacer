from __future__ import annotations

import argparse
import json
import struct
import sys
import traceback as tb_module
from pathlib import Path
from typing import Any, Literal

from PIL import Image

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

try:
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:
    import mapbox_earcut as earcut  # type: ignore
except Exception:  # pragma: no cover
    earcut = None  # type: ignore[assignment]


Language = Literal["ko", "en"]
Mode = Literal["fullrect", "tightclip", "tightmesh"]


def _msg(lang: Language, ko: str, en: str) -> str:
    return ko if lang == "ko" else en


def to_settings_raw(raw: int, mode: Mode) -> int:
    # bit1: packingMode (Rectangle=1, Tight=0)
    # bit6: meshType   (FullRect=0, Tight=1)
    if mode == "fullrect":
        return (raw | (1 << 1)) & ~(1 << 6)
    return (raw & ~(1 << 1)) | (1 << 6)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _is_close(a: float, b: float, eps: float = 1e-4) -> bool:
    return abs(a - b) <= eps


def _array_view(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        arr = value.get("Array")
        if isinstance(arr, list):
            return arr
    return None


def _is_quad_mesh(rd: dict[str, Any]) -> bool:
    vd = rd.get("m_VertexData")
    if not isinstance(vd, dict):
        return False
    try:
        vcount = int(vd.get("m_VertexCount", 0))
    except Exception:
        return False
    if vcount != 4:
        return False

    idx = _array_view(rd.get("m_IndexBuffer"))
    if idx is None or len(idx) != 12:
        return False

    sub = _array_view(rd.get("m_SubMeshes"))
    if sub is None or not sub:
        return False
    first = sub[0]
    if not isinstance(first, dict):
        return False
    return int(first.get("indexCount", 0)) == 6 and int(first.get("vertexCount", 0)) == 4


def _sync_texture_rect_offset(rd: dict[str, Any], mode: Mode) -> bool:
    rect = rd.get("textureRect")
    offset = rd.get("textureRectOffset")
    if not isinstance(rect, dict) or not isinstance(offset, dict):
        return False

    if mode == "fullrect":
        offset["x"] = 0.0
        offset["y"] = 0.0
    else:
        offset["x"] = _to_float(rect.get("x"))
        offset["y"] = _to_float(rect.get("y"))
    return True


def _force_quad_mesh(rd: dict[str, Any]) -> bool:
    vd = rd.get("m_VertexData")
    if not isinstance(vd, dict):
        return False

    try:
        vcount = int(vd.get("m_VertexCount", 0))
    except Exception:
        return False
    raw_data = vd.get("m_DataSize")
    data = _array_view(raw_data)
    if data is None or vcount < 3:
        return False

    data_bytes = bytes((int(x) & 0xFF) for x in data)
    pos_size = vcount * 12
    uv_size = vcount * 8
    if len(data_bytes) < pos_size + uv_size:
        return False

    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for i in range(vcount):
        x, y, z = struct.unpack_from("<fff", data_bytes, i * 12)
        xs.append(float(x))
        ys.append(float(y))
        zs.append(float(z))

    min_x = min(xs)
    max_x = max(xs)
    min_y = min(ys)
    max_y = max(ys)
    z = sum(zs) / len(zs) if zs else 0.0

    out = bytearray()
    for px, py, pz in (
        (min_x, min_y, z),
        (min_x, max_y, z),
        (max_x, max_y, z),
        (max_x, min_y, z),
    ):
        out.extend(struct.pack("<fff", px, py, pz))
    for _ in range(4):
        out.extend(struct.pack("<ff", 0.0, 0.0))

    vd["m_VertexCount"] = 4
    vd["m_DataSize"] = list(out)
    rd["m_VertexData"] = vd

    quad_idx = [0, 0, 1, 0, 2, 0, 0, 0, 2, 0, 3, 0]
    idx_obj = rd.get("m_IndexBuffer")
    if isinstance(idx_obj, dict):
        idx_obj["Array"] = quad_idx
        rd["m_IndexBuffer"] = idx_obj
    else:
        rd["m_IndexBuffer"] = {"Array": quad_idx}

    sub_obj = rd.get("m_SubMeshes")
    sub_arr = _array_view(sub_obj)
    if sub_arr is None or not sub_arr:
        sub_arr = [{
            "firstByte": 0,
            "indexCount": 6,
            "topology": 0,
            "baseVertex": 0,
            "firstVertex": 0,
            "vertexCount": 4,
            "localAABB": {
                "m_Center": {"x": 0.0, "y": 0.0, "z": 0.0},
                "m_Extent": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
        }]
    else:
        first = sub_arr[0]
        if isinstance(first, dict):
            first["firstByte"] = 0
            first["indexCount"] = 6
            first["baseVertex"] = 0
            first["firstVertex"] = 0
            first["vertexCount"] = 4
            sub_arr[0] = first
    if isinstance(sub_obj, dict):
        sub_obj["Array"] = sub_arr
        rd["m_SubMeshes"] = sub_obj
    else:
        rd["m_SubMeshes"] = {"Array": sub_arr}
    return True


def _read_mesh_bounds(rd: dict[str, Any]) -> tuple[float, float, float, float, float] | None:
    vd = rd.get("m_VertexData")
    if not isinstance(vd, dict):
        return None
    try:
        vcount = int(vd.get("m_VertexCount", 0))
    except Exception:
        return None
    data = _array_view(vd.get("m_DataSize"))
    if data is None or vcount < 3:
        return None
    data_bytes = bytes((int(x) & 0xFF) for x in data)
    if len(data_bytes) < (vcount * 12):
        return None

    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for i in range(vcount):
        x, y, z = struct.unpack_from("<fff", data_bytes, i * 12)
        xs.append(float(x))
        ys.append(float(y))
        zs.append(float(z))
    return min(xs), max(xs), min(ys), max(ys), (sum(zs) / len(zs) if zs else 0.0)


def _pack_vertex_streams_pos_uv(
    positions: list[tuple[float, float, float]],
    uvs: list[tuple[float, float]],
) -> list[int]:
    out = bytearray()
    for px, py, pz in positions:
        out.extend(struct.pack("<fff", float(px), float(py), float(pz)))
    pad = (-len(out)) & 0x0F
    if pad:
        out.extend(b"\x00" * pad)
    for u, v in uvs:
        out.extend(struct.pack("<ff", float(u), float(v)))
    return list(out)


def _set_texture_rect(rd: dict[str, Any], x: int, y: int, w: int, h: int, *, mode: Mode) -> tuple[bool, bool]:
    changed_rect = False
    changed_offset = False
    rect = rd.get("textureRect")
    if isinstance(rect, dict):
        before = (
            _to_float(rect.get("x")),
            _to_float(rect.get("y")),
            _to_float(rect.get("width")),
            _to_float(rect.get("height")),
        )
        after = (float(x), float(y), float(w), float(h))
        rect["x"], rect["y"], rect["width"], rect["height"] = after
        changed_rect = before != after

    offset = rd.get("textureRectOffset")
    if isinstance(offset, dict):
        if mode == "fullrect":
            new_ox, new_oy = 0.0, 0.0
        else:
            new_ox, new_oy = float(x), float(y)
        before = (_to_float(offset.get("x")), _to_float(offset.get("y")))
        after = (new_ox, new_oy)
        offset["x"], offset["y"] = after
        changed_offset = before != after
    return changed_rect, changed_offset


def _rect_to_bounds(data: dict[str, Any], rect_xywh: tuple[int, int, int, int], z: float) -> tuple[float, float, float, float, float] | None:
    x, y, w, h = rect_xywh
    if w <= 0 or h <= 0:
        return None

    ppu = _to_float(data.get("m_PixelsToUnits"), 100.0)
    if ppu <= 0.0:
        ppu = 100.0

    pivot = data.get("m_Pivot")
    if isinstance(pivot, dict):
        pivot_x = _to_float(pivot.get("x"), 0.5)
        pivot_y = _to_float(pivot.get("y"), 0.5)
    else:
        pivot_x = 0.5
        pivot_y = 0.5

    if pivot_x > 1.0:
        pivot_x = pivot_x / float(max(1, w))
    if pivot_y > 1.0:
        pivot_y = pivot_y / float(max(1, h))
    pivot_x = min(max(pivot_x, 0.0), 1.0)
    pivot_y = min(max(pivot_y, 0.0), 1.0)

    m_rect = data.get("m_Rect")
    if isinstance(m_rect, dict):
        full_x = _to_float(m_rect.get("x"), 0.0)
        full_y = _to_float(m_rect.get("y"), 0.0)
        full_w = _to_float(m_rect.get("width"), float(w))
        full_h = _to_float(m_rect.get("height"), float(h))
    else:
        full_x, full_y, full_w, full_h = 0.0, 0.0, float(w), float(h)

    pivot_px = full_x + (full_w * pivot_x)
    pivot_py = full_y + (full_h * pivot_y)
    min_x = (float(x) - pivot_px) / ppu
    max_x = (float(x + w) - pivot_px) / ppu
    min_y = (float(y) - pivot_py) / ppu
    max_y = (float(y + h) - pivot_py) / ppu
    return min_x, max_x, min_y, max_y, z


def _force_quad_mesh_from_rect(data: dict[str, Any], rd: dict[str, Any], rect_xywh: tuple[int, int, int, int]) -> bool:
    bounds = _read_mesh_bounds(rd)
    if bounds is None:
        return False
    z = bounds[4]
    rect_bounds = _rect_to_bounds(data, rect_xywh, z)
    if rect_bounds is None:
        return False
    min_x, max_x, min_y, max_y, z = rect_bounds

    vd = rd.get("m_VertexData")
    if not isinstance(vd, dict):
        return False

    positions = [
        (min_x, min_y, z),
        (min_x, max_y, z),
        (max_x, max_y, z),
        (max_x, min_y, z),
    ]
    uvs = [(0.0, 0.0)] * 4
    vd["m_VertexCount"] = 4
    vd["m_DataSize"] = _pack_vertex_streams_pos_uv(positions, uvs)
    rd["m_VertexData"] = vd

    quad_idx = [0, 0, 1, 0, 2, 0, 0, 0, 2, 0, 3, 0]
    idx_obj = rd.get("m_IndexBuffer")
    if isinstance(idx_obj, dict):
        idx_obj["Array"] = quad_idx
        rd["m_IndexBuffer"] = idx_obj
    else:
        rd["m_IndexBuffer"] = {"Array": quad_idx}

    sub_obj = rd.get("m_SubMeshes")
    sub_arr = _array_view(sub_obj)
    if sub_arr is None or not sub_arr:
        sub_arr = [
            {
                "firstByte": 0,
                "indexCount": 6,
                "topology": 0,
                "baseVertex": 0,
                "firstVertex": 0,
                "vertexCount": 4,
                "localAABB": {
                    "m_Center": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "m_Extent": {"x": 0.0, "y": 0.0, "z": 0.0},
                },
            }
        ]
    else:
        first = sub_arr[0]
        if isinstance(first, dict):
            first["firstByte"] = 0
            first["indexCount"] = 6
            first["baseVertex"] = 0
            first["firstVertex"] = 0
            first["vertexCount"] = 4
            sub_arr[0] = first
    if isinstance(sub_obj, dict):
        sub_obj["Array"] = sub_arr
        rd["m_SubMeshes"] = sub_obj
    else:
        rd["m_SubMeshes"] = {"Array": sub_arr}
    return True


def _polygon_area(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    s = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += (x1 * y2) - (x2 * y1)
    return 0.5 * s


def _triangulate_polygon(points: list[tuple[float, float]]) -> list[int] | None:
    if len(points) < 3:
        return None
    idx = list(range(len(points)))
    tri: list[int] = []

    def _inside(px: float, py: float, ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> bool:
        v0x, v0y = cx - ax, cy - ay
        v1x, v1y = bx - ax, by - ay
        v2x, v2y = px - ax, py - ay
        dot00 = (v0x * v0x) + (v0y * v0y)
        dot01 = (v0x * v1x) + (v0y * v1y)
        dot02 = (v0x * v2x) + (v0y * v2y)
        dot11 = (v1x * v1x) + (v1y * v1y)
        dot12 = (v1x * v2x) + (v1y * v2y)
        den = (dot00 * dot11) - (dot01 * dot01)
        if abs(den) <= 1e-12:
            return False
        inv = 1.0 / den
        u = ((dot11 * dot02) - (dot01 * dot12)) * inv
        v = ((dot00 * dot12) - (dot01 * dot02)) * inv
        return (u >= 0.0) and (v >= 0.0) and (u + v <= 1.0)

    ccw = _polygon_area(points) > 0.0
    guard = 0
    max_guard = len(points) * len(points) * 4
    while len(idx) > 3 and guard < max_guard:
        found = False
        m = len(idx)
        for i in range(m):
            i0 = idx[(i - 1) % m]
            i1 = idx[i]
            i2 = idx[(i + 1) % m]
            ax, ay = points[i0]
            bx, by = points[i1]
            cx, cy = points[i2]
            cross = ((bx - ax) * (cy - ay)) - ((by - ay) * (cx - ax))
            if ccw and cross <= 1e-8:
                continue
            if (not ccw) and cross >= -1e-8:
                continue

            has_inside = False
            for j in idx:
                if j in (i0, i1, i2):
                    continue
                px, py = points[j]
                if _inside(px, py, ax, ay, bx, by, cx, cy):
                    has_inside = True
                    break
            if has_inside:
                continue

            if ccw:
                tri.extend([i0, i1, i2])
            else:
                tri.extend([i0, i2, i1])
            del idx[i]
            found = True
            break
        if not found:
            return None
        guard += 1

    if len(idx) == 3:
        i0, i1, i2 = idx
        if ccw:
            tri.extend([i0, i1, i2])
        else:
            tri.extend([i0, i2, i1])
    return tri if tri else None


def _force_tight_mesh_from_alpha(
    data: dict[str, Any],
    rd: dict[str, Any],
    alpha: Image.Image,
    rect_xywh: tuple[int, int, int, int],
    *,
    dilate_px: int = 5,
) -> bool:
    if cv2 is None or np is None:
        return False
    if alpha.mode != "L":
        alpha = alpha.convert("L")

    arr = np.frombuffer(alpha.tobytes(), dtype=np.uint8).reshape((alpha.height, alpha.width))
    binary = np.where(arr > 0, 255, 0).astype(np.uint8)
    if dilate_px > 0:
        k = (dilate_px * 2) + 1
        kernel = np.ones((k, k), dtype=np.uint8)
        binary = cv2.dilate(binary, kernel, iterations=1)

    found = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    contours = found[0] if len(found) == 2 else found[1]
    if not contours:
        return False

    bounds = _read_mesh_bounds(rd)
    if bounds is None:
        return False
    z = bounds[4]
    rect_bounds = _rect_to_bounds(data, rect_xywh, z)
    if rect_bounds is None:
        return False
    min_x, max_x, min_y, max_y, z = rect_bounds

    tw = float(max(1, alpha.width - 1))
    th = float(max(1, alpha.height - 1))
    span_x = max_x - min_x
    span_y = max_y - min_y

    positions: list[tuple[float, float, float]] = []
    triangles: list[int] = []
    uvs: list[tuple[float, float]] = []

    for cnt in contours:
        if cnt is None or len(cnt) < 3:
            continue
        peri = float(cv2.arcLength(cnt, True))
        eps = max(0.05, peri * 0.00025)
        approx = cv2.approxPolyDP(cnt, eps, True)
        used = approx if approx is not None and len(approx) >= 3 else cnt
        poly = [(float(p[0][0]), float(p[0][1])) for p in used]
        if len(poly) < 3:
            continue

        local_tri: list[int] | None = None
        if earcut is not None and np is not None:
            try:
                verts_np = np.asarray(poly, dtype=np.float64)
                ends_np = np.asarray([len(poly)], dtype=np.uint32)
                local_tri = [int(x) for x in earcut.triangulate_float64(verts_np, ends_np).tolist()]
            except Exception:
                local_tri = None
        if not local_tri:
            local_tri = _triangulate_polygon(poly)
        if not local_tri:
            continue

        base_idx = len(positions)
        for px, py in poly:
            nx = min(max(px / tw, 0.0), 1.0)
            ny = min(max(py / th, 0.0), 1.0)
            vx = min_x + (nx * span_x)
            vy = max_y - (ny * span_y)
            positions.append((vx, vy, z))
            uvs.append((0.0, 0.0))
        for i in local_tri:
            triangles.append(base_idx + int(i))

    if not positions or not triangles:
        return False

    vd = rd.get("m_VertexData")
    if not isinstance(vd, dict):
        return False
    vd["m_VertexCount"] = len(positions)
    vd["m_DataSize"] = _pack_vertex_streams_pos_uv(positions, uvs)
    rd["m_VertexData"] = vd

    idx_bytes = bytearray()
    for idx in triangles:
        idx_bytes.extend(struct.pack("<H", int(idx)))
    idx_list = list(idx_bytes)
    idx_obj = rd.get("m_IndexBuffer")
    if isinstance(idx_obj, dict):
        idx_obj["Array"] = idx_list
        rd["m_IndexBuffer"] = idx_obj
    else:
        rd["m_IndexBuffer"] = {"Array": idx_list}

    sub_obj = rd.get("m_SubMeshes")
    sub_arr = _array_view(sub_obj)
    if sub_arr is None or not sub_arr:
        sub_arr = [
            {
                "firstByte": 0,
                "indexCount": len(triangles),
                "topology": 0,
                "baseVertex": 0,
                "firstVertex": 0,
                "vertexCount": len(positions),
                "localAABB": {
                    "m_Center": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "m_Extent": {"x": 0.0, "y": 0.0, "z": 0.0},
                },
            }
        ]
    else:
        first = sub_arr[0]
        if isinstance(first, dict):
            first["firstByte"] = 0
            first["indexCount"] = len(triangles)
            first["baseVertex"] = 0
            first["firstVertex"] = 0
            first["vertexCount"] = len(positions)
            sub_arr[0] = first
    if isinstance(sub_obj, dict):
        sub_obj["Array"] = sub_arr
        rd["m_SubMeshes"] = sub_obj
    else:
        rd["m_SubMeshes"] = {"Array": sub_arr}
    return True


def _resolve_rect_from_image(
    data: dict[str, Any],
    rd: dict[str, Any],
    image_rgba: Image.Image,
) -> tuple[tuple[int, int, int, int], Image.Image]:
    m_rect = data.get("m_Rect")
    tex_rect = rd.get("textureRect")
    mx = int(round(_to_float(m_rect.get("x")))) if isinstance(m_rect, dict) else 0
    my = int(round(_to_float(m_rect.get("y")))) if isinstance(m_rect, dict) else 0
    mw = int(round(_to_float(m_rect.get("width")))) if isinstance(m_rect, dict) else 0
    mh = int(round(_to_float(m_rect.get("height")))) if isinstance(m_rect, dict) else 0
    tx = int(round(_to_float(tex_rect.get("x")))) if isinstance(tex_rect, dict) else mx
    ty = int(round(_to_float(tex_rect.get("y")))) if isinstance(tex_rect, dict) else my
    tw = int(round(_to_float(tex_rect.get("width")))) if isinstance(tex_rect, dict) else mw
    th = int(round(_to_float(tex_rect.get("height")))) if isinstance(tex_rect, dict) else mh
    if tw <= 0 or th <= 0:
        tw, th = max(1, mw), max(1, mh)

    iw, ih = image_rgba.size
    if mw > 0 and mh > 0 and abs(iw - mw) <= 1 and abs(ih - mh) <= 1:
        base_x, base_y, base_w, base_h = mx, my, mw, mh
        work = image_rgba if image_rgba.size == (base_w, base_h) else image_rgba.resize((base_w, base_h), Image.Resampling.LANCZOS)
    elif abs(iw - tw) <= 1 and abs(ih - th) <= 1:
        base_x, base_y, base_w, base_h = tx, ty, tw, th
        work = image_rgba if image_rgba.size == (base_w, base_h) else image_rgba.resize((base_w, base_h), Image.Resampling.LANCZOS)
    elif mw > 0 and mh > 0:
        base_x, base_y, base_w, base_h = mx, my, mw, mh
        work = image_rgba.resize((base_w, base_h), Image.Resampling.LANCZOS)
    else:
        base_x, base_y, base_w, base_h = tx, ty, tw, th
        work = image_rgba.resize((base_w, base_h), Image.Resampling.LANCZOS)

    alpha = work.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        return (base_x, base_y, base_w, base_h), work

    bx0, by0, bx1, by1 = bbox
    rw = max(1, bx1 - bx0)
    rh = max(1, by1 - by0)
    rx = base_x + int(bx0)
    ry = base_y + int(base_h - by1)
    return (rx, ry, rw, rh), work.crop((bx0, by0, bx1, by1))

def _is_already_mode(data: dict[str, Any], *, mode: Mode, require_rect_expand: bool = True) -> bool:
    rd = data.get("m_RD")
    if not isinstance(rd, dict):
        return False
    raw = rd.get("settingsRaw")
    try:
        raw_int = int(raw)
    except Exception:
        return False
    if raw_int != to_settings_raw(raw_int, mode):
        return False

    if mode == "tightmesh":
        if data.get("m_IsPolygon") is not True:
            return False
        return True

    # fullrect / tightclip
    if data.get("m_IsPolygon") is True:
        return False
    if not _is_quad_mesh(rd):
        return False

    if mode == "tightclip":
        return True

    # fullrect
    if not require_rect_expand:
        return True

    m_rect = data.get("m_Rect")
    tex_rect = rd.get("textureRect")
    if not isinstance(m_rect, dict) or not isinstance(tex_rect, dict):
        return False

    x = _to_float(m_rect.get("x"))
    y = _to_float(m_rect.get("y"))
    w = _to_float(m_rect.get("width"))
    h = _to_float(m_rect.get("height"))

    tx = _to_float(tex_rect.get("x"))
    ty = _to_float(tex_rect.get("y"))
    tw = _to_float(tex_rect.get("width"))
    th = _to_float(tex_rect.get("height"))
    if not (_is_close(x, tx) and _is_close(y, ty) and _is_close(w, tw) and _is_close(h, th)):
        return False

    tex_off = rd.get("textureRectOffset")
    if isinstance(tex_off, dict):
        ox = _to_float(tex_off.get("x"))
        oy = _to_float(tex_off.get("y"))
        if not (_is_close(ox, 0.0) and _is_close(oy, 0.0)):
            return False

    return True


def patch_sprite_json(
    data: dict[str, Any],
    *,
    mode: Mode = "fullrect",
    image_rgba: Image.Image | None = None,
    patch_settings_raw: bool = True,
    expand_to_m_rect: bool = True,
    lang: Language = "ko",
) -> dict[str, Any]:
    if "m_RD" not in data or not isinstance(data["m_RD"], dict):
        raise ValueError(_msg(lang, "유효한 Sprite JSON이 아닙니다. 'm_RD' 키를 찾지 못했습니다.", "Invalid Sprite JSON: missing 'm_RD'."))

    rd = data["m_RD"]
    if "settingsRaw" not in rd:
        raise ValueError(_msg(lang, "유효한 Sprite JSON이 아닙니다. 'm_RD.settingsRaw' 키를 찾지 못했습니다.", "Invalid Sprite JSON: missing 'm_RD.settingsRaw'."))

    try:
        before_raw = int(rd["settingsRaw"])
    except Exception as exc:
        raise ValueError(
            _msg(
                lang,
                f"'settingsRaw' 값을 정수로 변환할 수 없습니다: {rd['settingsRaw']!r}",
                f"Could not parse integer from 'settingsRaw': {rd['settingsRaw']!r}",
            )
        ) from exc

    if patch_settings_raw:
        after_raw = to_settings_raw(before_raw, mode)
        rd["settingsRaw"] = after_raw
    else:
        after_raw = before_raw

    changed_polygon_false = False
    changed_polygon_true = False
    expanded_rect = False
    changed_offset_zero = False
    changed_offset_rect = False
    changed_mesh = False
    mesh_kept = False

    if mode == "fullrect":
        if "m_IsPolygon" in data and data["m_IsPolygon"] is not False:
            data["m_IsPolygon"] = False
            changed_polygon_false = True

        target_rect: tuple[int, int, int, int] | None = None
        if expand_to_m_rect:
            m_rect = data.get("m_Rect")
            if isinstance(m_rect, dict):
                x = int(round(_to_float(m_rect.get("x"))))
                y = int(round(_to_float(m_rect.get("y"))))
                w = int(round(_to_float(m_rect.get("width"))))
                h = int(round(_to_float(m_rect.get("height"))))
                if w > 0 and h > 0:
                    target_rect = (x, y, w, h)
                    expanded_rect, changed_offset_zero = _set_texture_rect(rd, x, y, w, h, mode=mode)
        else:
            changed_offset_zero = _sync_texture_rect_offset(rd, mode)

        if target_rect is not None:
            changed_mesh = _force_quad_mesh_from_rect(data, rd, target_rect)
        else:
            changed_mesh = _force_quad_mesh(rd)
    elif mode == "tightclip":
        if image_rgba is None:
            raise ValueError(_msg(lang, f"{mode} 모드에서는 --image가 필요합니다.", f"--image is required for {mode} mode."))
        if "m_IsPolygon" in data and data["m_IsPolygon"] is not False:
            data["m_IsPolygon"] = False
            changed_polygon_false = True

        clip_rect, _clip_alpha_img = _resolve_rect_from_image(data, rd, image_rgba)
        rx, ry, rw, rh = clip_rect
        _changed_rect, changed_offset_rect = _set_texture_rect(rd, rx, ry, rw, rh, mode=mode)
        changed_mesh = _force_quad_mesh_from_rect(data, rd, clip_rect) or _force_quad_mesh(rd)
    else:
        if image_rgba is None:
            raise ValueError(_msg(lang, f"{mode} 모드에서는 --image가 필요합니다.", f"--image is required for {mode} mode."))
        if "m_IsPolygon" in data and data["m_IsPolygon"] is not True:
            data["m_IsPolygon"] = True
            changed_polygon_true = True

        clip_rect, clip_alpha_img = _resolve_rect_from_image(data, rd, image_rgba)
        rx, ry, rw, rh = clip_rect
        _changed_rect, changed_offset_rect = _set_texture_rect(rd, rx, ry, rw, rh, mode=mode)
        changed_mesh = _force_tight_mesh_from_alpha(data, rd, clip_alpha_img.getchannel("A"), clip_rect, dilate_px=5)
        if not changed_mesh:
            mesh_kept = True

    return {
        "before_raw": before_raw,
        "after_raw": after_raw,
        "changed_polygon_false": changed_polygon_false,
        "changed_polygon_true": changed_polygon_true,
        "expanded_rect": expanded_rect,
        "changed_offset_zero": changed_offset_zero,
        "changed_offset_rect": changed_offset_rect,
        "changed_mesh": changed_mesh,
        "mesh_kept": mesh_kept,
    }


def build_mode_output_path(path: Path, mode: Mode) -> Path:
    return path.with_name(f"{path.stem}.{mode}{path.suffix}")


def patch_payload(
    payload: Any,
    *,
    mode: Mode = "fullrect",
    image_rgba: Image.Image | None = None,
    patch_settings_raw: bool = True,
    expand_to_m_rect: bool = True,
    lang: Language = "ko",
) -> tuple[str, dict[str, Any] | None]:
    if not isinstance(payload, dict):
        return "skip_non_object", None
    if "m_RD" not in payload or not isinstance(payload["m_RD"], dict) or "settingsRaw" not in payload["m_RD"]:
        return "skip_non_sprite", None

    if _is_already_mode(payload, mode=mode, require_rect_expand=expand_to_m_rect):
        return "already_target", payload

    details = patch_sprite_json(
        payload,
        mode=mode,
        image_rgba=image_rgba,
        patch_settings_raw=patch_settings_raw,
        expand_to_m_rect=expand_to_m_rect,
        lang=lang,
    )
    return "modified", {
        "payload": payload,
        **details,
    }


def format_modified_message(details: dict[str, Any], lang: Language) -> str:
    parts: list[str] = []
    if details.get("before_raw") != details.get("after_raw"):
        parts.append(f"settingsRaw {details['before_raw']}->{details['after_raw']}")
    if details.get("changed_polygon_false"):
        parts.append("m_IsPolygon=false")
    if details.get("changed_polygon_true"):
        parts.append("m_IsPolygon=true")
    if details.get("expanded_rect"):
        parts.append("textureRect=m_Rect")
    if details.get("changed_offset_zero"):
        parts.append("textureRectOffset=(0,0)")
    if details.get("changed_offset_rect"):
        parts.append("textureRectOffset=textureRect.xy")
    if details.get("changed_mesh"):
        parts.append("quad mesh rebuilt")
    if details.get("mesh_kept"):
        parts.append("mesh kept (tightmesh JSON mode)")
    return ", ".join(parts) if parts else f"settingsRaw {details['before_raw']}->{details['after_raw']}"


def patch_file_inplace(
    path: Path,
    *,
    mode: Mode = "fullrect",
    image_rgba: Image.Image | None = None,
    patch_settings_raw: bool = True,
    expand_to_m_rect: bool = True,
    lang: Language = "ko",
) -> tuple[str, str]:
    with path.open("r", encoding="utf-8-sig") as f:
        payload = json.load(f)

    status, details = patch_payload(
        payload,
        mode=mode,
        image_rgba=image_rgba,
        patch_settings_raw=patch_settings_raw,
        expand_to_m_rect=expand_to_m_rect,
        lang=lang,
    )
    if status != "modified":
        return status, ""

    patched = details["payload"]
    with path.open("w", encoding="utf-8") as f:
        json.dump(patched, f, ensure_ascii=False, indent=2)
    return status, format_modified_message(details, lang)


def convert_file_to_copy(
    path: Path,
    *,
    mode: Mode = "fullrect",
    image_rgba: Image.Image | None = None,
    patch_settings_raw: bool = True,
    expand_to_m_rect: bool = True,
    lang: Language = "ko",
) -> tuple[str, str, Path | None]:
    with path.open("r", encoding="utf-8-sig") as f:
        payload = json.load(f)

    status, details = patch_payload(
        payload,
        mode=mode,
        image_rgba=image_rgba,
        patch_settings_raw=patch_settings_raw,
        expand_to_m_rect=expand_to_m_rect,
        lang=lang,
    )
    if status in {"skip_non_object", "skip_non_sprite"}:
        return status, "", None

    output_path = build_mode_output_path(path, mode)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_payload = details["payload"] if details is not None else payload
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(write_payload, f, ensure_ascii=False, indent=2)

    if status == "modified":
        return status, format_modified_message(details, lang), output_path
    return status, "", output_path


def collect_targets(inputs: list[str], base_dir: Path, recursive: bool, *, exclude_generated_suffixes: bool) -> list[Path]:
    if inputs:
        return [Path(value).expanduser().resolve() for value in inputs]

    pattern = "**/*.json" if recursive else "*.json"
    targets = sorted(p.resolve() for p in base_dir.glob(pattern) if p.is_file())
    if exclude_generated_suffixes:
        suffixes = (".fullrect.json", ".tightclip.json", ".tightmesh.json")
        targets = [p for p in targets if not p.name.lower().endswith(suffixes)]
    return targets


def main_cli(lang: Language = "ko") -> None:
    if lang == "ko":
        desc = (
            "UABEA Sprite JSON을 mode별로 보정합니다.\n"
            "- 파일 인자를 주면 '<이름>.<mode>.json' 파일을 생성합니다.\n"
            "- 파일 인자 없이 실행하면 보정 JSON(.fullrect/.tightclip/.tightmesh) 제외 파일을 직접 수정합니다."
        )
        inputs_help = "변환할 JSON 파일 경로(여러 개 가능)"
        dir_help = "배치 대상 폴더(기본: 현재 폴더)"
        recursive_help = "하위 폴더까지 재귀 탐색"
        mode_help = "목표 모드 (fullrect|tightclip|tightmesh)"
        image_help = "tightclip/tightmesh에서 사용할 PNG 이미지 경로"
        expand_help = "fullrect에서 textureRect를 m_Rect 전체로 확장 (기본은 확장 안 함)"
    else:
        desc = (
            "Patch UABEA Sprite JSON by target mode.\n"
            "- With input file(s): create '<name>.<mode>.json'.\n"
            "- Without input: modify JSON files in-place excluding patched JSON (.fullrect/.tightclip/.tightmesh)."
        )
        inputs_help = "JSON file paths to convert (multiple allowed)"
        dir_help = "Target directory for batch mode (default: current directory)"
        recursive_help = "Search recursively in subdirectories"
        mode_help = "Target mode (fullrect|tightclip|tightmesh)"
        image_help = "PNG image path used by tightclip/tightmesh"
        expand_help = "For fullrect: expand textureRect to m_Rect (default: no expansion)"

    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument("inputs", nargs="*", help=inputs_help)
    parser.add_argument("--dir", default=".", help=dir_help)
    parser.add_argument("--recursive", action="store_true", help=recursive_help)
    parser.add_argument("--mode", choices=["fullrect", "tightclip", "tightmesh"], default="fullrect", help=mode_help)
    parser.add_argument("--image", default=None, help=image_help)
    parser.add_argument("--expand-rect", action="store_true", help=expand_help)
    parser.add_argument("--no-expand-rect", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    base_dir = Path(args.dir).expanduser().resolve()
    if not base_dir.exists():
        if lang == "ko":
            raise SystemExit(f"[오류] 폴더를 찾을 수 없습니다: {base_dir}")
        raise SystemExit(f"[Error] Directory not found: {base_dir}")

    mode = args.mode
    image_rgba: Image.Image | None = None
    if mode in ("tightclip", "tightmesh"):
        if not args.image:
            if lang == "ko":
                raise SystemExit(f"[오류] {mode} 모드에서는 --image가 필요합니다.")
            raise SystemExit(f"[Error] --image is required for {mode} mode.")
        image_path = Path(args.image).expanduser().resolve()
        try:
            image_rgba = Image.open(image_path).convert("RGBA")
        except Exception:
            if lang == "ko":
                raise SystemExit(f"[오류] 이미지를 열 수 없습니다: {image_path}")
            raise SystemExit(f"[Error] Could not open image: {image_path}")

    inputs_mode = len(args.inputs) > 0
    targets = collect_targets(
        args.inputs,
        base_dir,
        args.recursive,
        exclude_generated_suffixes=(not inputs_mode),
    )
    if not targets:
        if lang == "ko":
            print("[완료] 처리할 JSON 파일이 없습니다.")
        else:
            print("[Done] No JSON files to process.")
        return

    patch_settings_raw = True
    expand_to_m_rect = (mode == "fullrect") and bool(args.expand_rect) and (not args.no_expand_rect)

    modified = 0
    generated = 0
    skipped_full = 0
    skipped_non_sprite = 0
    errors = 0

    for target in targets:
        if not target.exists():
            if lang == "ko":
                print(f"[오류] 파일 없음: {target}")
            else:
                print(f"[Error] File not found: {target}")
            errors += 1
            continue

        if inputs_mode:
            try:
                status, msg, output_path = convert_file_to_copy(
                    target,
                    mode=mode,
                    image_rgba=image_rgba,
                    patch_settings_raw=patch_settings_raw,
                    expand_to_m_rect=expand_to_m_rect,
                    lang=lang,
                )
            except Exception as e:
                if lang == "ko":
                    print(f"[오류] {target.name}: {e}")
                else:
                    print(f"[Error] {target.name}: {e}")
                errors += 1
                continue

            if status in {"skip_non_object", "skip_non_sprite"}:
                skipped_non_sprite += 1
                if lang == "ko":
                    print(f"[스킵] {target.name}: Sprite JSON 아님")
                else:
                    print(f"[Skip] {target.name}: Not a Sprite JSON")
            elif status == "already_target":
                skipped_full += 1
                generated += 1
                if lang == "ko":
                    print(f"[생성] {output_path.name}: 이미 {mode} (내용 복사)")
                else:
                    print(f"[Created] {output_path.name}: Already {mode} (copied content)")
            elif status == "modified":
                modified += 1
                generated += 1
                if lang == "ko":
                    print(f"[생성] {output_path.name}: {msg}")
                else:
                    print(f"[Created] {output_path.name}: {msg}")
            else:
                errors += 1
                if lang == "ko":
                    print(f"[오류] {target.name}: 알 수 없는 상태({status})")
                else:
                    print(f"[Error] {target.name}: Unknown status ({status})")
        else:
            try:
                status, msg = patch_file_inplace(
                    target,
                    mode=mode,
                    image_rgba=image_rgba,
                    patch_settings_raw=patch_settings_raw,
                    expand_to_m_rect=expand_to_m_rect,
                    lang=lang,
                )
            except Exception as e:
                if lang == "ko":
                    print(f"[오류] {target.name}: {e}")
                else:
                    print(f"[Error] {target.name}: {e}")
                errors += 1
                continue

            if status in {"skip_non_object", "skip_non_sprite"}:
                skipped_non_sprite += 1
                if lang == "ko":
                    print(f"[스킵] {target.name}: Sprite JSON 아님")
                else:
                    print(f"[Skip] {target.name}: Not a Sprite JSON")
            elif status == "already_target":
                skipped_full += 1
                if lang == "ko":
                    print(f"[스킵] {target.name}: 이미 {mode}")
                else:
                    print(f"[Skip] {target.name}: Already {mode}")
            elif status == "modified":
                modified += 1
                if lang == "ko":
                    print(f"[수정] {target.name}: {msg}")
                else:
                    print(f"[Modified] {target.name}: {msg}")
            else:
                errors += 1
                if lang == "ko":
                    print(f"[오류] {target.name}: 알 수 없는 상태({status})")
                else:
                    print(f"[Error] {target.name}: Unknown status ({status})")

    if inputs_mode:
        if lang == "ko":
            print(
                f"[완료] 총 {len(targets)}개 | 생성 {generated} | 실제 수정 {modified} | "
                f"이미 목표 모드 {skipped_full} | Sprite 아님 {skipped_non_sprite} | 오류 {errors}"
            )
        else:
            print(
                f"[Done] Total {len(targets)} | Created {generated} | Actually modified {modified} | "
                f"Already target mode {skipped_full} | Not Sprite {skipped_non_sprite} | Errors {errors}"
            )
    else:
        if lang == "ko":
            print(
                f"[완료] 총 {len(targets)}개 | 수정 {modified} | 이미 목표 모드 {skipped_full} | "
                f"Sprite 아님 {skipped_non_sprite} | 오류 {errors}"
            )
        else:
            print(
                f"[Done] Total {len(targets)} | Modified {modified} | Already target mode {skipped_full} | "
                f"Not Sprite {skipped_non_sprite} | Errors {errors}"
            )


def run_main_ko() -> None:
    try:
        main_cli(lang="ko")
    except Exception as e:
        print(f"\n예상치 못한 오류가 발생했습니다: {e}")
        tb_module.print_exc()
        sys.exit(1)


def run_main_en() -> None:
    try:
        main_cli(lang="en")
    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")
        tb_module.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    run_main_ko()
