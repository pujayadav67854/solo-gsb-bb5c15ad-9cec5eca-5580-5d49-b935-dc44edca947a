"""停留识别。

按原始 trkseg 的时间顺序扫描（不跨分段边界），规则：

- 以候选首点（锚点）向后扩展：只要后续点与锚点的 haversine 距离不超过
  30 米，就视为仍在同一候选区间内连续；
- 候选区间首末时间至少相隔 3 分钟才记为一次停留；不足则不记为停留，
  候选首点向后滑动一个点继续扩展；
- 连续点与锚点距离超过 30 米（越界）即结算当前区间；命中停留后从区间末点
  之后继续扫描，因此各停留区间互不重叠；
- 相邻采样间隔超过 10 分钟视为记录中断：中断处结束当前候选区间，
  空档不计入记录时长，也不会把中断前后拼成一次停留；
- 相邻轨迹点时间不递增（相同或倒退）时抛出 ValueError，由接口层转为明确提示。

时长口径：

- 记录时长 = 各 trkseg 内所有"有效相邻采样时差"（时差 > 0 且 ≤ 10 分钟）之和，
  trkseg 之间以及超过 10 分钟的空档均不计入；
- 停留时长 = 各次停留首末时间之差之和（停留区间内部不存在中断空档）；
- 移动时长 = 记录时长 - 停留时长。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .geo import TrackPoint, haversine_m

STAY_RADIUS_M = 30.0      # 停留半径（米）：连续点与锚点距离不超过此值
MIN_STAY_SECONDS = 180.0  # 最短停留时长（秒）：首末至少相隔 3 分钟
MAX_GAP_SECONDS = 600.0   # 记录中断阈值（秒）：相邻采样间隔超过 10 分钟视为中断


@dataclass
class Stay:
    """一次停留；位置取候选首点（锚点）坐标。"""

    seg_index: int            # 原始 trkseg 序号（从 1 开始）
    start_index: int          # 首点在原始 trkseg 中的下标（从 0 开始）
    end_index: int            # 末点在原始 trkseg 中的下标（从 0 开始）
    lat: float                # 停留位置（锚点纬度）
    lon: float                # 停留位置（锚点经度）
    start_time: datetime
    end_time: datetime
    duration_s: float         # 首末时间之差（秒）
    point_count: int          # 区间包含的轨迹点数


@dataclass
class StayResult:
    """停留识别结果与全程时长口径。"""

    stays: list = field(default_factory=list)          # list[Stay]，按时间顺序
    record_duration_s: float = 0.0   # 记录时长（有效相邻采样时差之和）
    stay_duration_s: float = 0.0     # 停留总时长
    moving_duration_s: float = 0.0   # 移动时长 = 记录时长 - 停留时长
    gap_count: int = 0               # 超过 10 分钟的记录中断次数
    gap_duration_s: float = 0.0      # 被排除的中断空档总时长（秒）


def _check_times_and_record(seg_idx: int, seg: list) -> tuple:
    """校验时间严格递增，并统计该 trkseg 的有效记录时长与中断空档。

    返回 (record_duration_s, gap_count, gap_duration_s)；
    遇零时差或倒序立即抛出 ValueError。
    """
    record = 0.0
    gap_count = 0
    gap_duration = 0.0
    for i, (a, b) in enumerate(zip(seg, seg[1:]), start=1):
        if b.time <= a.time:
            raise ValueError(
                f"第 {seg_idx} 个 trkseg 中第 {i}→{i + 1} 个轨迹点的时间未递增"
                "（零时差或倒序），无法识别停留"
            )
        dt = (b.time - a.time).total_seconds()
        if dt <= MAX_GAP_SECONDS:
            record += dt
        else:
            gap_count += 1
            gap_duration += dt
    return record, gap_count, gap_duration


def detect_stays(segments: list) -> StayResult:
    """对整条轨迹（若干原始 trkseg）识别停留并统计全程时长。

    每个 trkseg 独立处理，不跨分段边界；时间未递增时抛出 ValueError。
    """
    stays: list = []
    record_duration = 0.0
    gap_count = 0
    gap_duration = 0.0

    for seg_idx, seg in enumerate(segments, start=1):
        n = len(seg)
        if n < 2:
            continue

        seg_record, seg_gap_count, seg_gap_duration = _check_times_and_record(seg_idx, seg)
        record_duration += seg_record
        gap_count += seg_gap_count
        gap_duration += seg_gap_duration

        # 锚点扩展扫描：i 为候选首点，j 为当前连续区间末点
        i = 0
        while i < n - 1:
            anchor = seg[i]
            j = i
            gap_after = False  # 扩展是否因超过 10 分钟的中断而停止
            while j + 1 < n:
                a, b = seg[j], seg[j + 1]
                if (b.time - a.time).total_seconds() > MAX_GAP_SECONDS:
                    gap_after = True  # 记录中断：空档不计入，区间止于 j
                    break
                if haversine_m(anchor, b) > STAY_RADIUS_M:
                    break             # 越界：区间止于 j，b 成为下一个候选
                j += 1

            duration_s = (seg[j].time - anchor.time).total_seconds()
            if duration_s >= MIN_STAY_SECONDS:
                stays.append(Stay(
                    seg_index=seg_idx,
                    start_index=i,
                    end_index=j,
                    lat=anchor.lat,
                    lon=anchor.lon,
                    start_time=anchor.time,
                    end_time=seg[j].time,
                    duration_s=duration_s,
                    point_count=j - i + 1,
                ))
                i = j + 1                 # 区间互不重叠：从末点之后继续
            elif gap_after:
                i = j + 1                 # 从中断后的首点重新开始
            else:
                i += 1                    # 非停留：候选首点向后滑动一个点

    stay_duration = sum(s.duration_s for s in stays)
    return StayResult(
        stays=stays,
        record_duration_s=record_duration,
        stay_duration_s=stay_duration,
        moving_duration_s=record_duration - stay_duration,
        gap_count=gap_count,
        gap_duration_s=gap_duration,
    )
