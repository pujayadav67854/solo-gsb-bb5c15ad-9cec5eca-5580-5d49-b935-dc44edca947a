"""停留识别的单元测试。

可直接运行：python3 tests/test_stay.py
或使用 pytest：python3 -m pytest tests/
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.geo import TrackPoint, haversine_m
from app.stay import (
    MAX_GAP_SECONDS,
    MIN_STAY_SECONDS,
    STAY_RADIUS_M,
    detect_stays,
)

T0 = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)
LAT0, LON0 = 31.0, 121.0


def make_seg(points):
    """points: [(lat, lon, 秒偏移), ...]"""
    return [
        TrackPoint(lat=la, lon=lo, ele=None, time=T0 + timedelta(seconds=s))
        for la, lo, s in points
    ]


def rest(seconds, step=60, lat=LAT0, lon=LON0, t0=0):
    """原地停留点序列：位置完全相同（距离 0 ≤ 30 米）。"""
    return [(lat, lon, t0 + i * step) for i in range(seconds // step + 1)]


def move_away(n=3, step=60, t0=0):
    """沿经线向北匀速移动，每点约 111 米（> 30 米）。"""
    return [(LAT0 + 0.001 * (i + 1), LON0, t0 + i * step) for i in range(n)]


def test_basic_stay_detected():
    """原地停留 4 分钟（5 个点）：识别为一次停留，位置取锚点。"""
    seg = make_seg(rest(240))
    res = detect_stays([seg])

    assert len(res.stays) == 1
    s = res.stays[0]
    assert s.seg_index == 1
    assert s.start_index == 0 and s.end_index == 4
    assert s.point_count == 5
    assert s.lat == LAT0 and s.lon == LON0
    assert s.start_time == seg[0].time and s.end_time == seg[-1].time
    assert s.duration_s == 240.0


def test_too_short_not_a_stay():
    """连续点都在 30 米内但首末仅 2 分钟：不足 3 分钟，不记为停留。"""
    seg = make_seg(rest(120))
    res = detect_stays([seg])
    assert res.stays == []
    assert res.stay_duration_s == 0.0


def test_min_duration_boundary():
    """首末恰好 3 分钟：记为一次停留（边界含等于）。"""
    seg = make_seg(rest(int(MIN_STAY_SECONDS)))
    res = detect_stays([seg])
    assert len(res.stays) == 1
    assert res.stays[0].duration_s == MIN_STAY_SECONDS


def test_radius_boundary_and_out_of_range():
    """锚点 30 米内（含）仍连续；超过 30 米即越界结算。"""
    inside = LAT0 + STAY_RADIUS_M / 111_320.0
    outside = LAT0 + (STAY_RADIUS_M + 5) / 111_320.0
    assert haversine_m(TrackPoint(LAT0, LON0), TrackPoint(inside, LON0)) <= STAY_RADIUS_M
    assert haversine_m(TrackPoint(LAT0, LON0), TrackPoint(outside, LON0)) > STAY_RADIUS_M

    # 0,60,120,180 秒均在 30 米内 → 停留 3 分钟；240 秒点越界
    seg = make_seg([
        (LAT0, LON0, 0),
        (inside, LON0, 60),
        (inside, LON0, 120),
        (inside, LON0, 180),
        (outside, LON0, 240),
    ])
    res = detect_stays([seg])
    assert len(res.stays) == 1
    assert res.stays[0].end_index == 3


def test_out_of_range_settles_then_move_on():
    """停留后远离移动：只识别前面的停留，移动部分不计入。"""
    seg = make_seg(rest(240) + move_away(t0=300))
    res = detect_stays([seg])
    assert len(res.stays) == 1
    assert res.stays[0].duration_s == 240.0
    assert res.stays[0].end_index == 4


def test_two_non_overlapping_stays():
    """移动后再次停留：两次停留区间互不重叠，时长分别累计。"""
    pts = rest(240, t0=0)
    pts += move_away(t0=300)                      # 360,420,480 秒向北移动
    pts += rest(300, lat=LAT0 + 0.005, t0=600)    # 移动终点之外的新位置
    seg = make_seg(pts)
    res = detect_stays([seg])

    assert len(res.stays) == 2
    s1, s2 = res.stays
    assert s1.start_index == 0 and s1.end_index == 4
    assert abs(s2.lat - (LAT0 + 0.005)) < 1e-12
    assert s2.start_index > s1.end_index     # 区间互不重叠
    assert s1.duration_s == 240.0 and s2.duration_s == 300.0
    assert res.stay_duration_s == 540.0
    # 记录：240 停留 + 4 条移动衔接腿各 60 秒（共 360）+ 300 停留
    assert res.record_duration_s == 900.0
    assert res.moving_duration_s == 360.0


def test_gap_longer_than_10_minutes_not_merged():
    """间隔超过 10 分钟：不拼成一次停留，空档不计入记录与停留时长。"""
    pts = rest(240, t0=0)                                   # 0~240 停留
    pts.append((LAT0, LON0, 240 + int(MAX_GAP_SECONDS) + 300))  # 15 分钟后同点
    seg = make_seg(pts)
    res = detect_stays([seg])

    assert len(res.stays) == 1                 # 中断后的单一点不构成停留
    assert res.stays[0].duration_s == 240.0
    assert res.gap_count == 1
    assert res.gap_duration_s == MAX_GAP_SECONDS + 300
    assert res.record_duration_s == 240.0      # 空档被排除
    assert res.moving_duration_s == 0.0


def test_gap_exactly_10_minutes_is_valid():
    """间隔恰好 10 分钟：不算中断，计入记录时长且可构成停留。"""
    gap = int(MAX_GAP_SECONDS)
    seg = make_seg([
        (LAT0, LON0, 0),
        (LAT0, LON0, gap),
        (LAT0, LON0, 2 * gap),
    ])
    res = detect_stays([seg])
    assert res.gap_count == 0
    assert res.record_duration_s == 2 * MAX_GAP_SECONDS
    assert len(res.stays) == 1
    assert res.stays[0].duration_s == 2 * MAX_GAP_SECONDS


def test_gap_between_two_stays_excluded():
    """两次停留之间有 >10 分钟空档：各算一次停留，空档不计入任何时长。"""
    gap = int(MAX_GAP_SECONDS)
    pts = rest(240, t0=0)                                              # 停留 4 分钟
    pts += [(LAT0 + 0.002, LON0, 240 + gap + i * 60 + 1) for i in range(5)]
    seg = make_seg(pts)
    res = detect_stays([seg])

    assert len(res.stays) == 2
    assert res.stays[0].duration_s == 240.0
    assert res.stays[1].duration_s == 240.0
    assert res.gap_count == 1
    assert res.record_duration_s == 480.0     # 两段各 240 秒，空档被排除
    assert res.stay_duration_s == 480.0
    assert res.moving_duration_s == 0.0


def test_record_and_moving_duration():
    """记录时长=有效相邻时差之和；移动时长=记录时长-停留时长。"""
    pts = rest(240, t0=0)                       # 停留 240 秒（4 条腿）
    pts += move_away(t0=300)                    # 360/420/480 移动（3 条腿）
    seg = make_seg(pts)
    res = detect_stays([seg])

    # 7 条腿各 60 秒 = 420 秒记录时长；停留 240 秒；移动 180 秒
    assert res.record_duration_s == 420.0
    assert res.stay_duration_s == 240.0
    assert res.moving_duration_s == 180.0


def test_no_cross_trkseg_stay():
    """停留不跨原始 trkseg：两段末尾/开头即使同点也不合并；段间空档不计入。"""
    seg1 = make_seg(rest(240))
    seg2 = make_seg([(LAT0, LON0, 3600 + i * 60) for i in range(5)])
    res = detect_stays([seg1, seg2])

    assert len(res.stays) == 2
    assert {s.seg_index for s in res.stays} == {1, 2}
    # 两段各 240 秒；段间 1 小时空档不计入记录时长
    assert res.record_duration_s == 480.0


def test_stay_at_segment_end():
    """停留区间延伸到 trkseg 末点：扫描结束时结算。"""
    pts = move_away(t0=0)
    pts += rest(240, lat=LAT0 + 0.005, t0=300)
    seg = make_seg(pts)
    res = detect_stays([seg])
    assert len(res.stays) == 1
    s = res.stays[0]
    assert s.end_index == len(seg) - 1
    assert s.duration_s == 240.0


def test_anchor_slides_when_candidate_too_short():
    """候选不足 3 分钟时锚点逐点后移：前段短暂路过不会丢掉真正的停留。

    前 4 个点相对首锚点始终在 30 米内但只持续 120 秒（不足 3 分钟），
    第 5 点对首锚点越界；之后在新位置停留 4 分钟。逐点滑动锚点后
    识别后一段停留。
    """
    pts = [
        (LAT0, LON0, 0),
        (LAT0 + 10 / 111_320.0, LON0, 60),
        (LAT0 + 20 / 111_320.0, LON0, 120),   # 距首锚点 20 米：候选仅 120 秒
        (LAT0 + 60 / 111_320.0, LON0, 180),   # 对首锚点越界
        (LAT0 + 60 / 111_320.0, LON0, 240),
        (LAT0 + 60 / 111_320.0, LON0, 300),
        (LAT0 + 60 / 111_320.0, LON0, 360),
        (LAT0 + 60 / 111_320.0, LON0, 420),   # 新位置停留 180~420（4 分钟）
    ]
    seg = make_seg(pts)
    res = detect_stays([seg])
    assert len(res.stays) == 1
    s = res.stays[0]
    assert abs(s.lat - (LAT0 + 60 / 111_320.0)) < 1e-12
    assert s.start_index == 3
    assert s.duration_s == 240.0


def test_zero_time_diff_rejected():
    seg = make_seg([(LAT0, LON0, 0), (LAT0, LON0, 0), (LAT0, LON0, 60)])
    try:
        detect_stays([seg])
    except ValueError as exc:
        assert "未递增" in str(exc) and "第 1 个 trkseg" in str(exc)
    else:
        raise AssertionError("零时差应报明确错误")


def test_reversed_time_rejected():
    seg = make_seg([(LAT0, LON0, 120), (LAT0, LON0, 60), (LAT0, LON0, 180)])
    try:
        detect_stays([seg])
    except ValueError as exc:
        assert "未递增" in str(exc) and "第 1 个 trkseg" in str(exc)
    else:
        raise AssertionError("倒序时间应报明确错误")


def test_error_reports_segment_index():
    """第二个 trkseg 时间倒序：错误提示指出第 2 个分段。"""
    seg1 = make_seg(rest(120))
    seg2 = make_seg([(LAT0, LON0, 60), (LAT0, LON0, 0)])
    try:
        detect_stays([seg1, seg2])
    except ValueError as exc:
        assert "第 2 个 trkseg" in str(exc)
    else:
        raise AssertionError("应报第 2 个 trkseg 的时间错误")


def test_single_point_segment_ignored():
    seg = make_seg([(LAT0, LON0, 0)])
    res = detect_stays([seg])
    assert res.stays == []
    assert res.record_duration_s == 0.0


def test_movement_only_no_stays():
    """全程匀速移动（每点 >30 米）：无停留，记录时长全部为移动时长。"""
    seg = make_seg(move_away(n=6))
    res = detect_stays([seg])
    assert res.stays == []
    assert res.record_duration_s == 300.0   # 5 条腿各 60 秒
    assert res.moving_duration_s == 300.0
    assert res.stay_duration_s == 0.0


def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
