"""停留识别与记录/移动时长统计。

判定规则（对每个原始 trkseg 独立进行，不跨分段边界）：

- 按时间顺序以候选首点为锚向后扩展：从锚点开始逐一纳入后续点，
  只要新点到锚点的水平距离不超过 30 米就继续扩展；
- 越界即结算：遇到首个超出 30 米的点时，当前候选区间（锚到前一个点）立即结算，
  越界点成为下一个候选首点，区间互不重叠；分段末尾同样结算；
- 候选区间首末时间相隔至少 3 分钟才记为一次停留，否则丢弃；
- 相邻采样时间不递增（相同或倒退）时抛出 ValueError，由接口层转为明确提示；
- 相邻采样间隔超过 10 分钟视为记录中断：空档不计入任何时长，
  但不结算、不打断当前候选区间（停留判定只看空间范围与首末时间）。

时长口径：

- 全程记录时长 = 各 trkseg 内有效相邻采样时差（不超过 10 分钟）之和，
  分段间隔不计入（与分段配速口径一致）；
- 停留时长 = 各停留区间内有效相邻采样时差之和（区间互不重叠，可安全求和）；
- 移动时长 = 记录时长 − 停留时长。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .geo import haversine_m

STAY_RADIUS_M = 30.0   # 停留判定半径（米）：各点到候选首点的水平距离上限
MIN_STAY_S = 180.0     # 最短停留（秒）：候选区间首末至少相隔 3 分钟
MAX_GAP_S = 600.0      # 记录中断阈值（秒）：相邻采样间隔超过 10 分钟不计入时长


@dataclass
class Stay:
    """一次停留：同一 trkseg 内连续若干点构成的互不重叠区间。"""

    seg_index: int        # 原始 trkseg 序号（从 1 开始）
    start_index: int      # 停留区间首点在该 trkseg 中的下标（从 0 开始）
    end_index: int        # 停留区间末点下标（含）
    lat: float            # 停留位置（区间内各点经纬度的算术平均）
    lon: float
    start_time: datetime
    end_time: datetime
    duration_s: float     # 停留时长（不含区间内超过 10 分钟的记录中断空档）
    points: int           # 区间内轨迹点数


@dataclass
class StayResult:
    """停留识别结果与全程时长统计。"""

    stays: list = field(default_factory=list)  # list[Stay]，按时间先后排列
    stay_total_s: float = 0.0   # 全程停留时长
    recording_s: float = 0.0    # 全程记录时长（有效相邻采样时差之和）
    moving_s: float = 0.0       # 全程移动时长 = 记录时长 − 停留时长


def _check_times(seg_idx: int, seg: list) -> None:
    """校验 trkseg 内相邻时间严格递增；零时差或倒序时抛出 ValueError。"""
    for i, (a, b) in enumerate(zip(seg, seg[1:]), start=1):
        if b.time <= a.time:
            raise ValueError(
                f"第 {seg_idx} 个 trkseg 中第 {i}→{i + 1} 个轨迹点的时间未递增"
                "（零时差或倒序），无法进行停留识别"
            )


def _valid_duration(points: list) -> float:
    """有效相邻采样时差之和：超过 10 分钟的记录中断空档不计入。"""
    total = 0.0
    for a, b in zip(points, points[1:]):
        dt = (b.time - a.time).total_seconds()
        if dt <= MAX_GAP_S:
            total += dt
    return total


def detect_stays(segments: list) -> StayResult:
    """对整条轨迹（若干原始 trkseg）识别停留并统计记录/停留/移动时长。

    每个 trkseg 独立处理：以候选首点为锚向后扩展，越界即结算，区间互不重叠；
    首末相隔不足 3 分钟的候选区间不记为停留。
    """
    stays: list = []
    recording = 0.0

    for seg_idx, seg in enumerate(segments, start=1):
        _check_times(seg_idx, seg)
        recording += _valid_duration(seg)

        n = len(seg)
        i = 0
        while i < n:
            anchor = seg[i]
            j = i + 1
            while j < n and haversine_m(anchor, seg[j]) <= STAY_RADIUS_M:
                j += 1
            # 候选区间 [i, j-1]：首末相隔至少 3 分钟才记为停留
            last = j - 1
            span = (seg[last].time - anchor.time).total_seconds()
            if span >= MIN_STAY_S:
                pts = seg[i:j]
                stays.append(Stay(
                    seg_index=seg_idx,
                    start_index=i,
                    end_index=last,
                    lat=sum(p.lat for p in pts) / len(pts),
                    lon=sum(p.lon for p in pts) / len(pts),
                    start_time=anchor.time,
                    end_time=seg[last].time,
                    duration_s=_valid_duration(pts),
                    points=len(pts),
                ))
            i = j  # 越界即结算：越界点成为下一个候选首点，区间互不重叠

    stay_total = sum(s.duration_s for s in stays)
    return StayResult(
        stays=stays,
        stay_total_s=stay_total,
        recording_s=recording,
        moving_s=recording - stay_total,
    )
