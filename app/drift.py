"""GPX 单点漂移（跳点）检测与修复。

判定规则（对每个原始 trkseg 独立进行，不跨分段边界）：

- 只考察中间点 B（其前一相邻点 A、后一相邻点 C）；每个 trkseg 的首尾点
  不参与判定，始终保留；
- 相邻轨迹点时间必须严格递增；遇零时差或倒序立即抛出 ValueError，
  由接口层转为明确提示；
- 同时满足以下三条时，B 判为跳点：
  1. A→B 与 B→C 两段速度均超过 12 米/秒；
  2. 绕过 B 的 A→C 首尾速度不超过 12 米/秒；
  3. B 到线段 AC 的水平最短距离超过 40 米。

修复方式：按原始点顺序逐轮扫描，每轮只删除最早命中的一个跳点，
删除后以剩余点重新计算，直到无命中；被删跳点不参与后续判定。
修复结果仅用于漂移修复与导出，遮蔽裁切与配速统计仍使用原始轨迹。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .geo import haversine_m, point_segment_distance_m

MAX_SPEED_MPS = 12.0  # 速度阈值（米/秒）：进出两段须超过，绕过段不得超过
MIN_OFFSET_M = 40.0   # 跳点到首尾线段的水平最短距离阈值（米）


@dataclass
class DriftHit:
    """一个被删除的跳点及其判定依据。"""

    seg_index: int          # 原始 trkseg 序号（从 1 开始）
    point_index: int        # 在原始 trkseg 中的下标（从 0 开始）
    lat: float
    lon: float
    time: datetime
    speed_in_mps: float     # 前一段（A→B）速度
    speed_out_mps: float    # 后一段（B→C）速度
    speed_bypass_mps: float # 绕过该点的首尾（A→C）速度
    offset_m: float         # 到首尾线段 AC 的水平最短距离


@dataclass
class DriftResult:
    """漂移修复结果；fixed_segments 与输入 segments 结构一致（每段保留首尾点）。"""

    fixed_segments: list            # list[list[TrackPoint]]
    hits: list = field(default_factory=list)  # list[DriftHit]，按删除先后排列
    distance_before_m: float = 0.0
    distance_after_m: float = 0.0


def _check_times(seg_idx: int, seg: list) -> None:
    """校验 trkseg 内相邻时间严格递增；零时差或倒序时抛出 ValueError。"""
    for i, (a, b) in enumerate(zip(seg, seg[1:]), start=1):
        if b.time <= a.time:
            raise ValueError(
                f"第 {seg_idx} 个 trkseg 中第 {i}→{i + 1} 个轨迹点的时间未递增"
                "（零时差或倒序），无法进行漂移修复"
            )


def _first_hit(seg_idx: int, pts: list, orig_indices: list):
    """按原始点顺序扫描当前剩余点，返回最早命中的 (当前下标, DriftHit)，无命中返回 None。

    调用前已校验原始分段时间严格递增；删除中间点只会把两个正时差合并，
    因此此处相邻时差恒为正。
    """
    for i in range(1, len(pts) - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        dt_in = (b.time - a.time).total_seconds()
        dt_out = (c.time - b.time).total_seconds()
        v_in = haversine_m(a, b) / dt_in
        v_out = haversine_m(b, c) / dt_out
        if v_in <= MAX_SPEED_MPS or v_out <= MAX_SPEED_MPS:
            continue
        v_bypass = haversine_m(a, c) / (dt_in + dt_out)
        if v_bypass > MAX_SPEED_MPS:
            continue
        offset = point_segment_distance_m(b, a, c)
        if offset <= MIN_OFFSET_M:
            continue
        return i, DriftHit(
            seg_index=seg_idx,
            point_index=orig_indices[i],
            lat=b.lat,
            lon=b.lon,
            time=b.time,
            speed_in_mps=v_in,
            speed_out_mps=v_out,
            speed_bypass_mps=v_bypass,
            offset_m=offset,
        )
    return None


def fix_drift(segments: list) -> DriftResult:
    """对整条轨迹（若干原始 trkseg）做单点漂移修复。

    每个 trkseg 独立处理：逐轮删除最早命中的一个跳点并重算，直到无命中；
    首尾点始终保留。返回修复后的分段、全部命中点与修复前后里程。
    """
    fixed_segments: list = []
    hits: list = []
    distance_before = 0.0
    distance_after = 0.0

    for seg_idx, seg in enumerate(segments, start=1):
        for a, b in zip(seg, seg[1:]):
            distance_before += haversine_m(a, b)

        _check_times(seg_idx, seg)

        pts = list(seg)                      # 当前剩余点
        orig_indices = list(range(len(seg)))  # 剩余点在原始 trkseg 中的下标
        while True:
            found = _first_hit(seg_idx, pts, orig_indices)
            if found is None:
                break
            i, hit = found
            hits.append(hit)
            del pts[i]
            del orig_indices[i]

        for a, b in zip(pts, pts[1:]):
            distance_after += haversine_m(a, b)
        fixed_segments.append(pts)

    return DriftResult(
        fixed_segments=fixed_segments,
        hits=hits,
        distance_before_m=distance_before,
        distance_after_m=distance_after,
    )
