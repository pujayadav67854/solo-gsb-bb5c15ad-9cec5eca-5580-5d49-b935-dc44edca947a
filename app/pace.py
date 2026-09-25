"""分段配速统计：按固定里程把每个原始 trkseg 切成若干段。

- 距离按 haversine 球面距离逐采样线段累计，每个原始 trkseg 单独从 0 起算；
- 采样线段跨越一个或多个里程边界时，边界时间按距离比例线性插值；
- 末尾不足一段的剩余部分也列为一段；
- 相邻轨迹点时间不递增（相同或倒退）时抛出 ValueError，由接口层转为提示；
- 距离为 0 的段配速为 None（不显示无穷配速）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .geo import haversine_m

MIN_SPLIT_KM = 0.1
MAX_SPLIT_KM = 10.0
DEFAULT_SPLIT_KM = 1.0

_EPS_M = 1e-6  # 距离累计的浮点噪声阈值（米），低于此值视为恰好落在边界上


@dataclass
class Split:
    """一个配速段；起止时间中至少一端可能是按距离比例插值出的边界时间。"""

    seg_index: int       # 原始 trkseg 序号（从 1 开始）
    split_index: int     # 在该 trkseg 内的段序号（从 1 开始）
    distance_m: float
    duration_s: float
    start_time: datetime
    end_time: datetime

    @property
    def pace_min_per_km(self) -> Optional[float]:
        """分/公里配速；零里程段返回 None（避免无穷大）。"""
        if self.distance_m <= 0.0:
            return None
        return self.duration_s / 60.0 / (self.distance_m / 1000.0)


def pace_splits(segments: list, split_km: float = DEFAULT_SPLIT_KM) -> list:
    """对每个原始 trkseg 分别按 split_km 公里切段。

    返回 list[list[Split]]，与输入 segments 一一对应。
    时间未递增或 split_km 超出范围时抛出 ValueError。
    """
    if not (MIN_SPLIT_KM <= split_km <= MAX_SPLIT_KM):
        raise ValueError(f"每段里程须在 {MIN_SPLIT_KM}～{MAX_SPLIT_KM} 公里之间")
    split_m = split_km * 1000.0
    return [
        _split_segment(seg, seg_idx, split_m)
        for seg_idx, seg in enumerate(segments, start=1)
    ]


def _split_segment(seg: list, seg_idx: int, split_m: float) -> list:
    if not seg:
        return []
    for i, (a, b) in enumerate(zip(seg, seg[1:]), start=1):
        if b.time <= a.time:
            raise ValueError(
                f"第 {seg_idx} 个 trkseg 中第 {i}→{i + 1} 个轨迹点的时间未递增，无法计算配速"
            )

    splits: list = []
    split_start_time = seg[0].time
    acc = 0.0  # 当前段已累计的距离（米）
    for a, b in zip(seg, seg[1:]):
        d = haversine_m(a, b)
        if d <= 0.0:  # 原地不动的采样线段：不累计距离，停留时间自然计入所在段
            continue
        span = b.time - a.time
        pos = 0.0  # 本采样线段内已处理到的位置（距 a 的米数）
        # 一条采样线段可能跨越一个或多个里程边界；
        # 插值参数 t 始终相对于线段起点 a 计算
        while acc + (d - pos) >= split_m:
            pos += split_m - acc
            boundary_time = a.time + span * (pos / d)
            splits.append(Split(
                seg_index=seg_idx,
                split_index=len(splits) + 1,
                distance_m=split_m,
                duration_s=(boundary_time - split_start_time).total_seconds(),
                start_time=split_start_time,
                end_time=boundary_time,
            ))
            split_start_time = boundary_time
            acc = 0.0
        acc += d - pos
    # 末尾不足一段的剩余部分也列出；整段零里程的 trkseg 列出一个零里程段
    if acc > _EPS_M or not splits:
        splits.append(Split(
            seg_index=seg_idx,
            split_index=len(splits) + 1,
            distance_m=acc if acc > _EPS_M else 0.0,
            duration_s=(seg[-1].time - split_start_time).total_seconds(),
            start_time=split_start_time,
            end_time=seg[-1].time,
        ))
    return splits
