"""轨迹几何计算：距离、线段-遮蔽圆求交、按可见性裁切轨迹。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

EARTH_RADIUS_M = 6_371_008.8  # IUGG 平均地球半径


@dataclass
class TrackPoint:
    lat: float
    lon: float
    ele: Optional[float] = None
    time: Optional[datetime] = None


@dataclass
class MaskCircle:
    lat: float
    lon: float
    radius_m: float


@dataclass
class MaskResult:
    visible_segments: list  # list[list[TrackPoint]]，每个元素导出一个 trkseg
    hidden_segments: list   # list[list[TrackPoint]]，用于前端预览
    hidden_intervals: int   # 隐藏段数
    distance_before_m: float
    distance_after_m: float


def haversine_m(a: TrackPoint, b: TrackPoint) -> float:
    lat1, lon1, lat2, lon2 = (math.radians(v) for v in (a.lat, a.lon, b.lat, b.lon))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def _to_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple:
    """以 (lat0, lon0) 为原点的局部等距圆柱投影（米）。对纬度/经度是线性的。"""
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * EARTH_RADIUS_M
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def point_segment_distance_m(p: TrackPoint, a: TrackPoint, b: TrackPoint) -> float:
    """点 p 到线段 ab 的水平最短距离（米）。

    在以 a 为原点的局部等距圆柱投影中计算；a、b 重合时退化为 p 到 a 的距离。
    """
    px, py = _to_xy(p.lat, p.lon, a.lat, a.lon)
    bx, by = _to_xy(b.lat, b.lon, a.lat, a.lon)
    seg_len2 = bx * bx + by * by
    if seg_len2 == 0.0:
        return math.hypot(px, py)
    t = (px * bx + py * by) / seg_len2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - t * bx, py - t * by)


def _inside(p: TrackPoint, circle: MaskCircle) -> bool:
    x, y = _to_xy(p.lat, p.lon, circle.lat, circle.lon)
    return x * x + y * y < circle.radius_m * circle.radius_m


def _segment_circle_crossings(a: TrackPoint, b: TrackPoint, circle: MaskCircle) -> list:
    """返回线段 ab 与圆周的交点参数 t（0 < t < 1），t 为沿线段的比例。

    在圆心局部投影中求解 |a + t*(b-a)|^2 = r^2；即使 a、b 都在圆外，
    只要线段穿越圆（判别式 > 0）也会返回两个交点。
    """
    ax, ay = _to_xy(a.lat, a.lon, circle.lat, circle.lon)
    bx, by = _to_xy(b.lat, b.lon, circle.lat, circle.lon)
    dx, dy = bx - ax, by - ay
    A = dx * dx + dy * dy
    if A == 0.0:
        return []
    B = 2.0 * (ax * dx + ay * dy)
    C = ax * ax + ay * ay - circle.radius_m * circle.radius_m
    disc = B * B - 4.0 * A * C
    if disc <= 0.0:  # 无交点或相切（相切不进入圆内，忽略）
        return []
    sq = math.sqrt(disc)
    out = []
    for t in ((-B - sq) / (2.0 * A), (-B + sq) / (2.0 * A)):
        if 0.0 < t < 1.0:
            out.append(t)
    return out


def _interp(a: TrackPoint, b: TrackPoint, t: float) -> TrackPoint:
    """按比例 t 线性插值经纬度、海拔与时间（局部投影对经纬度是线性的，
    因此 t 处插值点恰好落在遮蔽圆边界上）。"""
    ele = None
    if a.ele is not None and b.ele is not None:
        ele = a.ele + (b.ele - a.ele) * t
    time = None
    if a.time is not None and b.time is not None:
        time = a.time + (b.time - a.time) * t
    return TrackPoint(
        lat=a.lat + (b.lat - a.lat) * t,
        lon=a.lon + (b.lon - a.lon) * t,
        ele=ele,
        time=time,
    )


def mask_track(segments: list, circles: list) -> MaskResult:
    """按遮蔽圆裁切轨迹。

    - segments: 原始轨迹，list[list[TrackPoint]]，每个子列表对应一个原始 trkseg；
    - 可见部分不会跨原始分段边界合并，每个可见连续段导出一个独立 trkseg；
    - 隐藏段数 = 被裁掉的连续区间个数（按原始分段分别计数）。
    """
    visible_segments: list = []
    hidden_segments: list = []
    distance_before = 0.0
    distance_after = 0.0

    for seg in segments:
        n = len(seg)
        if n == 0:
            continue
        for a, b in zip(seg, seg[1:]):
            distance_before += haversine_m(a, b)

        def is_hidden(p: TrackPoint) -> bool:
            return any(_inside(p, c) for c in circles)

        if n == 1:
            (hidden_segments if circles and is_hidden(seg[0])
             else visible_segments).append([seg[0]])
            continue

        current_visible: list = []
        current_hidden: list = []

        for a, b in zip(seg, seg[1:]):
            ts = set()
            for c in circles:
                ts.update(_segment_circle_crossings(a, b, c))
            bounds = [0.0] + sorted(ts) + [1.0]
            for t0, t1 in zip(bounds, bounds[1:]):
                p0 = a if t0 == 0.0 else _interp(a, b, t0)
                p1 = b if t1 == 1.0 else _interp(a, b, t1)
                mid = _interp(a, b, (t0 + t1) * 0.5)
                if circles and is_hidden(mid):
                    if current_visible:
                        visible_segments.append(current_visible)
                        current_visible = []
                    if not current_hidden:
                        current_hidden = [p0]
                    current_hidden.append(p1)
                else:
                    if current_hidden:
                        hidden_segments.append(current_hidden)
                        current_hidden = []
                    distance_after += haversine_m(p0, p1)
                    if not current_visible:
                        current_visible = [p0]
                    current_visible.append(p1)
        if current_visible:
            visible_segments.append(current_visible)
        if current_hidden:
            hidden_segments.append(current_hidden)

    return MaskResult(
        visible_segments=visible_segments,
        hidden_segments=hidden_segments,
        hidden_intervals=len(hidden_segments),
        distance_before_m=distance_before,
        distance_after_m=distance_after,
    )
