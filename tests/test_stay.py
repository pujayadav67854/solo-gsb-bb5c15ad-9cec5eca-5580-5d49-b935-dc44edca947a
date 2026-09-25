"""停留识别与记录/移动时长统计的单元测试。

可直接运行：python3 tests/test_stay.py
或使用 pytest：python3 -m pytest tests/
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.geo import TrackPoint, haversine_m
from app.stay import MAX_GAP_S, MIN_STAY_S, STAY_RADIUS_M, detect_stays

T0 = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)

# 测试轨迹围绕 (31.0, 121.0)：0.0001° 纬度 ≈ 11.12 m，0.0001° 经度 ≈ 9.53 m
HOME = (31.0, 121.0)
NEAR = (31.0001, 121.0001)   # 距 HOME 约 14.6 m，在 30 m 半径内
FAR = (31.001, 121.0)        # 距 HOME 约 111.2 m，超出 30 m 半径


def make_seg(points):
    """points: [(lat, lon, 秒偏移), ...]"""
    return [
        TrackPoint(lat=la, lon=lo, ele=500.0, time=T0 + timedelta(seconds=s))
        for la, lo, s in points
    ]


def tp(latlon):
    return TrackPoint(lat=latlon[0], lon=latlon[1])


def test_single_stay_detected():
    """围绕一点徘徊 6 分钟：记为一次停留，分段末尾结算，位置为区间内各点均值。"""
    seg = make_seg([
        (HOME[0], HOME[1], 0),
        (NEAR[0], NEAR[1], 120),
        (HOME[0], HOME[1], 240),
        (NEAR[0], NEAR[1], 360),
    ])
    res = detect_stays([seg])

    assert len(res.stays) == 1
    s = res.stays[0]
    assert s.seg_index == 1
    assert (s.start_index, s.end_index) == (0, 3)
    assert s.points == 4
    assert s.start_time == T0
    assert s.end_time == T0 + timedelta(seconds=360)
    assert abs(s.duration_s - 360.0) < 1e-9
    assert abs(s.lat - (HOME[0] + NEAR[0]) / 2) < 1e-9
    assert abs(s.lon - (HOME[1] + NEAR[1]) / 2) < 1e-9

    # 全程都在停留：记录时长 = 停留时长，移动时长为 0
    assert abs(res.recording_s - 360.0) < 1e-9
    assert abs(res.stay_total_s - 360.0) < 1e-9
    assert abs(res.moving_s - 0.0) < 1e-9


def test_short_cluster_not_stay():
    """徘徊不足 3 分钟：不记为停留，但时间仍计入记录时长。"""
    seg = make_seg([
        (HOME[0], HOME[1], 0),
        (NEAR[0], NEAR[1], 60),
        (HOME[0], HOME[1], 120),   # 首末仅 2 分钟
        (FAR[0], FAR[1], 180),
    ])
    res = detect_stays([seg])
    assert res.stays == []
    assert abs(res.recording_s - 180.0) < 1e-9
    assert abs(res.moving_s - 180.0) < 1e-9


def test_out_of_bounds_settles_and_reanchors():
    """越界即结算：越界点成为下一个候选首点，两段停留区间互不重叠。"""
    seg = make_seg([
        (HOME[0], HOME[1], 0),          # P0 停留1 锚
        (NEAR[0], NEAR[1], 120),        # P1
        (HOME[0], HOME[1], 240),        # P2 停留1 末点（首末 240 s）
        (FAR[0], FAR[1], 300),          # P3 越界 → 结算停留1，P3 成为新锚
        (31.0011, 121.0001, 420),       # P4 距 P3 约 14.6 m
        (FAR[0], FAR[1], 540),          # P5 停留2 末点（首末 240 s）
        (31.002, 121.0, 600),           # P6 越界 → 结算停留2
    ])
    res = detect_stays([seg])

    assert len(res.stays) == 2
    s1, s2 = res.stays
    assert (s1.start_index, s1.end_index) == (0, 2)
    assert (s2.start_index, s2.end_index) == (3, 5)  # 越界点 P3 是新候选首点
    assert s1.end_index < s2.start_index             # 区间互不重叠
    assert abs(s1.duration_s - 240.0) < 1e-9
    assert abs(s2.duration_s - 240.0) < 1e-9
    assert abs(res.recording_s - 600.0) < 1e-9
    assert abs(res.moving_s - 120.0) < 1e-9


def test_anchor_not_chained():
    """半径相对锚点判定，而非相邻点链式：缓慢漂移累计超 30 m 即结算。"""
    # 相邻两步各约 20 m（< 30 m），但 P2 距锚点 P0 约 40 m（> 30 m）
    assert 20.0 < haversine_m(tp(HOME), tp((31.00018, 121.0))) < STAY_RADIUS_M
    assert haversine_m(tp(HOME), tp((31.00036, 121.0))) > STAY_RADIUS_M
    seg = make_seg([
        (31.0, 121.0, 0),        # P0 锚
        (31.00018, 121.0, 120),  # P1 距 P0 约 20 m
        (31.00036, 121.0, 240),  # P2 距 P0 约 40 m → 越界结算 [P0,P1]
        (31.00036, 121.0, 360),  # P3 新锚，原地
    ])
    res = detect_stays([seg])
    # 链式判定会把 P0~P2 连成 4 分钟停留；锚点判定下 [P0,P1] 仅 2 分钟，不算停留
    assert res.stays == []


def test_exactly_three_minutes_counts():
    """首末恰好相隔 3 分钟：满足最短停留条件。"""
    seg = make_seg([
        (HOME[0], HOME[1], 0),
        (NEAR[0], NEAR[1], 90),
        (HOME[0], HOME[1], MIN_STAY_S),
    ])
    res = detect_stays([seg])
    assert len(res.stays) == 1
    assert abs(res.stays[0].duration_s - 180.0) < 1e-9


def test_radius_boundary():
    """30 米边界：约 28.9 米纳入区间，约 31.1 米越界结算。"""
    in_lat = 31.0 + 0.00026   # 距 HOME 约 28.9 m
    out_lat = 31.0 + 0.00028  # 距 HOME 约 31.1 m
    assert haversine_m(tp(HOME), tp((in_lat, 121.0))) < STAY_RADIUS_M
    assert haversine_m(tp(HOME), tp((out_lat, 121.0))) > STAY_RADIUS_M
    seg = make_seg([
        (HOME[0], HOME[1], 0),
        (in_lat, 121.0, 200),    # 边界内
        (out_lat, 121.0, 400),   # 越界 → 结算 [P0,P1]
    ])
    res = detect_stays([seg])
    assert len(res.stays) == 1
    s = res.stays[0]
    assert (s.start_index, s.end_index) == (0, 1)
    assert s.points == 2
    assert abs(s.duration_s - 200.0) < 1e-9


def test_zero_time_diff_rejected():
    seg = make_seg([(HOME[0], HOME[1], 0), (NEAR[0], NEAR[1], 0), (FAR[0], FAR[1], 10)])
    try:
        detect_stays([seg])
    except ValueError as exc:
        assert "未递增" in str(exc) and "第 1 个 trkseg" in str(exc)
    else:
        raise AssertionError("零时差应报明确错误")


def test_reversed_time_rejected():
    seg = make_seg([(HOME[0], HOME[1], 10), (NEAR[0], NEAR[1], 5), (FAR[0], FAR[1], 20)])
    try:
        detect_stays([seg])
    except ValueError as exc:
        assert "未递增" in str(exc)
    else:
        raise AssertionError("倒序时间应报明确错误")


def test_error_reports_segment_number():
    """第二个 trkseg 时间倒序：错误信息指出具体分段序号。"""
    seg1 = make_seg([(HOME[0], HOME[1], 0), (NEAR[0], NEAR[1], 60)])
    seg2 = make_seg([(HOME[0], HOME[1], 120), (NEAR[0], NEAR[1], 110)])
    try:
        detect_stays([seg1, seg2])
    except ValueError as exc:
        assert "第 2 个 trkseg" in str(exc)
    else:
        raise AssertionError("倒序时间应报明确错误")


def test_gap_excluded_from_durations():
    """间隔超 10 分钟视为记录中断：空档不计入时长，但不打断停留区间。"""
    seg = make_seg([
        (HOME[0], HOME[1], 0),
        (NEAR[0], NEAR[1], 60),
        (HOME[0], HOME[1], 120),
        (NEAR[0], NEAR[1], 1020),   # 中断 900 s（> 10 分钟）
        (HOME[0], HOME[1], 1080),
        (NEAR[0], NEAR[1], 1140),
    ])
    res = detect_stays([seg])
    # 空档前后仍在 30 米内 → 仍为同一次停留（首末 1140 s ≥ 3 分钟）
    assert len(res.stays) == 1
    s = res.stays[0]
    assert (s.start_index, s.end_index) == (0, 5)
    # 停留时长与记录时长都不含 900 s 空档：4 × 60 s
    assert abs(s.duration_s - 240.0) < 1e-9
    assert abs(res.recording_s - 240.0) < 1e-9
    assert abs(res.stay_total_s - 240.0) < 1e-9
    assert abs(res.moving_s - 0.0) < 1e-9


def test_gap_at_most_10min_counts():
    """间隔恰好 10 分钟：不算记录中断，计入时长。"""
    seg = make_seg([
        (HOME[0], HOME[1], 0),
        (NEAR[0], NEAR[1], MAX_GAP_S),
        (HOME[0], HOME[1], 2 * MAX_GAP_S),
    ])
    res = detect_stays([seg])
    assert len(res.stays) == 1
    assert abs(res.stays[0].duration_s - 1200.0) < 1e-9
    assert abs(res.recording_s - 1200.0) < 1e-9


def test_recording_stay_moving_totals():
    """停留 + 移动混合：记录时长 = 停留时长 + 移动时长。"""
    seg = make_seg([
        (HOME[0], HOME[1], 0),      # P0 停留锚
        (NEAR[0], NEAR[1], 60),     # P1
        (HOME[0], HOME[1], 120),    # P2
        (NEAR[0], NEAR[1], 180),    # P3
        (HOME[0], HOME[1], 240),    # P4 停留末点（首末 240 s）
        (FAR[0], FAR[1], 300),      # P5 越界 → 结算停留
        (31.002, 121.0, 360),       # P6 移动
        (31.003, 121.0, 420),       # P7 移动
    ])
    res = detect_stays([seg])
    assert len(res.stays) == 1
    s = res.stays[0]
    assert (s.start_index, s.end_index) == (0, 4)
    assert abs(s.duration_s - 240.0) < 1e-9
    assert abs(res.recording_s - 420.0) < 1e-9
    assert abs(res.stay_total_s - 240.0) < 1e-9
    assert abs(res.moving_s - 180.0) < 1e-9


def test_multiple_trksegs_independent():
    """不跨 trkseg 拼合：分段各自结算；分段间隔不计入记录时长。"""
    seg1 = make_seg([
        (HOME[0], HOME[1], 0),
        (NEAR[0], NEAR[1], 60),
        (HOME[0], HOME[1], 120),   # 首末仅 2 分钟
    ])
    seg2 = make_seg([
        (NEAR[0], NEAR[1], 180),
        (HOME[0], HOME[1], 240),
        (NEAR[0], NEAR[1], 300),   # 首末仅 2 分钟
    ])
    res = detect_stays([seg1, seg2])
    # 若跨分段拼合会凑成 5 分钟停留；按 trkseg 独立判定则两次都不够 3 分钟
    assert res.stays == []
    # 记录时长只含各分段内部时差（120 + 120），分段间隔 60 s 不计入
    assert abs(res.recording_s - 240.0) < 1e-9
    assert abs(res.moving_s - 240.0) < 1e-9


def test_single_point_segment():
    """单点分段：无停留，记录时长为 0。"""
    res = detect_stays([make_seg([(HOME[0], HOME[1], 0)])])
    assert res.stays == []
    assert res.recording_s == 0.0
    assert res.moving_s == 0.0


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
