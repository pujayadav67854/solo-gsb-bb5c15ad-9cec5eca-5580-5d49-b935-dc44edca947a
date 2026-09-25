"""海拔剖面与累计爬升分析。

- 基于上传时保存的原始 GPX 轨迹（各原始 trkseg）计算，与遮蔽裁切、配速统计互不影响；
- 每个 trkseg 内，把 ele 非空的极大连续子序列作为"连续有效海拔区间"：
  缺失海拔处断开，不跨原始分段或缺口插值；
- 区间内按水平里程（haversine 逐段累计）每 20 米线性重采样海拔，区间端点也保留；
- 对采样海拔做三点中位数滤波抑制孤立尖峰（序列端点保持原值）；
- 再以 5 米反向变化确认峰谷：趋势极值被反向变化超过 5 米时确认一个峰/谷，
  累计已确认的上升与下降；区间末尾未确认的变化也计入；
- 全程无任何有效海拔时 has_ele=False，爬升/下降为 None（页面显示"无海拔数据"而非 0）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .geo import haversine_m

RESAMPLE_STEP_M = 20.0      # 重采样间隔（水平里程，米）
REVERSAL_THRESHOLD_M = 5.0  # 确认峰谷所需的反向变化（米）
_EPS_M = 1e-6               # 里程端点判定的浮点噪声阈值（米）


@dataclass
class ProfileRun:
    """一个连续有效海拔区间的剖面（重采样并平滑后）。"""

    seg_index: int        # 原始 trkseg 序号（从 1 开始）
    start_m: float        # 区间起点在全程水平里程中的位置（米）
    points: list = field(default_factory=list)  # [(全程里程, 平滑海拔), ...]


@dataclass
class ElevationResult:
    """海拔分析结果；无任何有效海拔时 has_ele=False，爬升/下降为 None。"""

    has_ele: bool
    distance_m: float           # 全程水平里程（含无海拔部分）
    ascent_m: Optional[float]   # 总爬升（米）
    descent_m: Optional[float]  # 总下降（米）
    ele_min: Optional[float]    # 平滑后采样海拔的最小/最大值（绘图量程）
    ele_max: Optional[float]
    runs: list = field(default_factory=list)  # list[ProfileRun]


def _valid_runs(seg: list) -> list:
    """把 trkseg 切成若干连续有效海拔区间（ele 非 None 的极大连续子序列）。

    返回 list[list[(下标, TrackPoint)]]，下标为该点在 trkseg 中的位置。
    """
    runs = []
    current = []
    for i, p in enumerate(seg):
        if p.ele is not None:
            current.append((i, p))
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _resample(dists: list, eles: list, step: float = RESAMPLE_STEP_M) -> list:
    """把一个连续有效海拔区间按水平里程每 step 米线性重采样，区间端点也保留。

    dists: 区间内各点距区间起点的累计水平里程（米），从 0 开始、单调不减；
    返回 [(区间内里程, 插值海拔), ...]，相邻采样间隔不超过 step，末点为区间终点。
    """
    total = dists[-1]
    if total <= 0.0:  # 区间无水平位移（单点或原地）：只保留起点
        return [(0.0, eles[0])]
    marks = []
    m = 0.0
    while m < total:
        marks.append(m)
        m += step
    if total - marks[-1] < _EPS_M:
        marks[-1] = total  # 浮点噪声下终点与采样点重合：合并，不多出一个点
    else:
        marks.append(total)  # 区间终点也保留
    out = []
    j = 0
    for m in marks:
        while j + 1 < len(dists) and dists[j + 1] < m:
            j += 1
        if j + 1 >= len(dists):
            out.append((m, eles[-1]))
            continue
        d0, d1 = dists[j], dists[j + 1]
        if d1 <= d0:  # 原地不动的采样线段：海拔取线段起点
            out.append((m, eles[j]))
        else:
            t = (m - d0) / (d1 - d0)
            out.append((m, eles[j] + (eles[j + 1] - eles[j]) * t))
    return out


def _median3(eles: list) -> list:
    """三点中位数滤波：抑制孤立尖峰；序列端点保持原值。"""
    n = len(eles)
    if n < 3:
        return list(eles)
    out = [eles[0]]
    for i in range(1, n - 1):
        out.append(sorted((eles[i - 1], eles[i], eles[i + 1]))[1])
    out.append(eles[-1])
    return out


def _confirmed_climb(eles: list, threshold: float = REVERSAL_THRESHOLD_M):
    """按反向变化阈值确认峰谷，返回 (累计上升, 累计下降)。

    - 上升/下降趋势中的极值被反向变化超过 threshold 时确认一个峰/谷，
      累计从上一个谷/峰到该峰/谷的变化量；
    - 序列末尾尚未被反向变化确认的趋势段，其变化量也计入；
    - 方向始终未确认（全程变化不足 threshold）时按首尾净变化计入。
    """
    ascent = descent = 0.0
    n = len(eles)
    if n < 2:
        return ascent, descent
    trend = 0                  # +1 上升段，-1 下降段，0 方向未确认
    base = eles[0]             # 当前趋势起点（谷或峰）的海拔
    extreme = eles[0]          # 当前趋势中的极值
    high = low = eles[0]       # 方向未确认时跟踪的两端极值及其出现先后
    high_i = low_i = 0
    for i in range(1, n):
        e = eles[i]
        if trend == 0:
            if e > high:
                high, high_i = e, i
            if e < low:
                low, low_i = e, i
            if high - low >= threshold:
                if low_i < high_i:   # 先谷后峰 → 确认谷，转入上升段
                    descent += base - low
                    trend, base, extreme = 1, low, high
                else:                # 先峰后谷 → 确认峰，转入下降段
                    ascent += high - base
                    trend, base, extreme = -1, high, low
        elif trend == 1:             # 上升段：跟踪最高点
            if e > extreme:
                extreme = e
            elif extreme - e >= threshold:   # 回落超阈值 → 确认峰
                ascent += extreme - base
                trend, base, extreme = -1, extreme, e
        else:                        # 下降段：跟踪最低点
            if e < extreme:
                extreme = e
            elif e - extreme >= threshold:   # 回升超阈值 → 确认谷
                descent += base - extreme
                trend, base, extreme = 1, extreme, e
    # 尾端未确认的变化也计入
    if trend == 1:
        ascent += extreme - base
    elif trend == -1:
        descent += base - extreme
    else:                            # 方向始终未确认：按首尾净变化计入
        delta = eles[-1] - eles[0]
        if delta > 0:
            ascent += delta
        elif delta < 0:
            descent -= delta
    return ascent, descent


def elevation_profile(segments: list) -> ElevationResult:
    """对整条轨迹（若干原始 trkseg）计算海拔剖面与累计爬升/下降。

    爬升/下降在每个连续有效海拔区间内独立累计后求和，不跨分段或缺口；
    剖面里程为全程水平里程（含无海拔部分），缺口处在图上自然断开。
    """
    runs: list = []
    ascent = descent = 0.0
    distance = 0.0       # 全程水平里程（含无海拔部分）
    any_ele = False
    ele_min: Optional[float] = None
    ele_max: Optional[float] = None

    for seg_idx, seg in enumerate(segments, start=1):
        cum = [0.0]      # 该 trkseg 内各点距其起点的累计水平里程
        for a, b in zip(seg, seg[1:]):
            cum.append(cum[-1] + haversine_m(a, b))

        for run in _valid_runs(seg):
            any_ele = True
            first = run[0][0]
            dists = [cum[i] - cum[first] for i, _ in run]
            eles = [p.ele for _, p in run]
            sampled = _resample(dists, eles)
            smoothed = _median3([e for _, e in sampled])
            a, d = _confirmed_climb(smoothed)
            ascent += a
            descent += d
            for e in smoothed:
                ele_min = e if ele_min is None else min(ele_min, e)
                ele_max = e if ele_max is None else max(ele_max, e)
            offset = distance + cum[first]  # 区间起点的全程里程
            runs.append(ProfileRun(
                seg_index=seg_idx,
                start_m=offset,
                points=[(offset + m, e) for (m, _), e in zip(sampled, smoothed)],
            ))
        distance += cum[-1]

    if not any_ele:
        return ElevationResult(
            has_ele=False, distance_m=distance,
            ascent_m=None, descent_m=None,
            ele_min=None, ele_max=None, runs=[],
        )
    return ElevationResult(
        has_ele=True, distance_m=distance,
        ascent_m=ascent, descent_m=descent,
        ele_min=ele_min, ele_max=ele_max, runs=runs,
    )
