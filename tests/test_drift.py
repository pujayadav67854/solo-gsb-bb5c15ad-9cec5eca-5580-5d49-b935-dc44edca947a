"""单点漂移（跳点）检测与修复的单元测试。

可直接运行：python3 tests/test_drift.py
或使用 pytest：python3 -m pytest tests/
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.drift import MAX_SPEED_MPS, MIN_OFFSET_M, fix_drift
from app.geo import TrackPoint, haversine_m, point_segment_distance_m
from app.gpxutil import build_gpx, parse_gpx

T0 = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)

# 测试轨迹围绕 (31.0, 121.0)：0.001° 纬度 ≈ 111.19 m；
# 跳点 (31.0005, 121.0005) 距首尾连线（经线）约 47.6 m，进出两段各约 73.2 m
A = (31.0, 121.0)
SPIKE = (31.0005, 121.0005)
C1 = (31.001, 121.0)
C2 = (31.002, 121.0)


def make_seg(points):
    """points: [(lat, lon, 秒偏移), ...]"""
    return [
        TrackPoint(lat=la, lon=lo, ele=500.0, time=T0 + timedelta(seconds=s))
        for la, lo, s in points
    ]


def tp(latlon):
    return TrackPoint(lat=latlon[0], lon=latlon[1])


def test_single_spike_removed():
    """三点中段一个跳点：命中并删除，首尾点保留，里程按删除后重算。"""
    seg = make_seg([(A[0], A[1], 0), (SPIKE[0], SPIKE[1], 5), (C1[0], C1[1], 10)])
    res = fix_drift([seg])

    assert len(res.hits) == 1
    hit = res.hits[0]
    assert hit.seg_index == 1
    assert hit.point_index == 1
    assert abs(hit.lat - SPIKE[0]) < 1e-9 and abs(hit.lon - SPIKE[1]) < 1e-9
    assert hit.speed_in_mps > MAX_SPEED_MPS and hit.speed_out_mps > MAX_SPEED_MPS
    assert hit.speed_bypass_mps <= MAX_SPEED_MPS
    assert hit.offset_m > MIN_OFFSET_M

    fixed = res.fixed_segments[0]
    assert len(fixed) == 2
    assert fixed[0] is seg[0] and fixed[-1] is seg[-1]  # 首尾点保留

    d_legs = haversine_m(tp(A), tp(SPIKE)) + haversine_m(tp(SPIKE), tp(C1))
    assert abs(res.distance_before_m - d_legs) < 1e-9
    assert abs(res.distance_after_m - haversine_m(tp(A), tp(C1))) < 1e-9


def test_normal_track_no_hits():
    """等速直线（约 11.1 m/s，低于 12 m/s 阈值）：无命中，里程不变。"""
    seg = make_seg([(31.0 + i * 0.001, 121.0, i * 10) for i in range(11)])
    res = fix_drift([seg])
    assert res.hits == []
    assert res.fixed_segments[0] == seg
    assert abs(res.distance_before_m - res.distance_after_m) < 1e-9


def test_bypass_speed_too_high_not_hit():
    """绕过速度同样超过 12 m/s（整体高速）：不判为跳点。"""
    seg = make_seg([(A[0], A[1], 0), (SPIKE[0], SPIKE[1], 5), (C2[0], C2[1], 10)])
    # 进出两段均 > 12 m/s、偏移 > 40 m，但 A→C2 绕过速度 ≈ 22 m/s > 12
    res = fix_drift([seg])
    assert res.hits == []
    assert len(res.fixed_segments[0]) == 3


def test_small_offset_not_hit():
    """偏移不超过 40 m：即使速度条件满足也不判为跳点。"""
    near = (31.0005, 121.0004)  # 距经线约 38.1 m
    assert point_segment_distance_m(tp(near), tp(A), tp(C1)) <= MIN_OFFSET_M
    seg = make_seg([(A[0], A[1], 0), (near[0], near[1], 5), (C1[0], C1[1], 10)])
    res = fix_drift([seg])
    assert res.hits == []


def test_slow_leg_not_hit():
    """进出两段有一段不超过 12 m/s：不判为跳点。"""
    seg = make_seg([(A[0], A[1], 0), (SPIKE[0], SPIKE[1], 30), (C1[0], C1[1], 35)])
    res = fix_drift([seg])
    assert res.hits == []


def test_zero_time_diff_rejected():
    seg = make_seg([(A[0], A[1], 0), (SPIKE[0], SPIKE[1], 0), (C1[0], C1[1], 10)])
    try:
        fix_drift([seg])
    except ValueError as exc:
        assert "未递增" in str(exc) and "第 1 个 trkseg" in str(exc)
    else:
        raise AssertionError("零时差应报明确错误")


def test_reversed_time_rejected():
    seg = make_seg([(A[0], A[1], 10), (SPIKE[0], SPIKE[1], 5), (C1[0], C1[1], 20)])
    try:
        fix_drift([seg])
    except ValueError as exc:
        assert "未递增" in str(exc)
    else:
        raise AssertionError("倒序时间应报明确错误")


def test_segment_endpoints_never_removed():
    """漂移点位于 trkseg 首尾时不参与判定：首尾点始终保留。"""
    seg = make_seg([
        (SPIKE[0], SPIKE[1], 0),          # 首点漂移：不判定
        (A[0], A[1], 5),
        (C1[0], C1[1], 15),
        (C2[0], C2[1], 25),
        (31.0025, 121.0005, 30),          # 尾点漂移：不判定
    ])
    res = fix_drift([seg])
    assert res.hits == []
    fixed = res.fixed_segments[0]
    assert len(fixed) == 5
    assert fixed[0] is seg[0] and fixed[-1] is seg[-1]


def test_no_cross_trkseg_judgment():
    """不跨 trkseg 判断：分段边界的跳点不与相邻分段拼合判定。"""
    seg1 = make_seg([(A[0], A[1], 0), (C1[0], C1[1], 10), (31.01, 121.0, 20)])
    seg2 = make_seg([(C2[0], C2[1], 30), (31.003, 121.0, 40)])
    res = fix_drift([seg1, seg2])
    # 若跨分段判定，seg1 末点 (31.01,121.0) 会与 seg2 首点拼合而命中
    assert res.hits == []
    assert res.fixed_segments[0] == seg1
    assert res.fixed_segments[1] == seg2


def test_iterative_removal_earliest_first():
    """逐轮只删最早命中的跳点：两个跳点分两轮删除，命中顺序按原始点序。"""
    seg = make_seg([
        (A[0], A[1], 0),            # P0
        (SPIKE[0], SPIKE[1], 5),    # P1 跳点
        (C1[0], C1[1], 10),         # P2
        (31.0015, 121.0005, 15),    # P3 跳点（删除 P1 后才成为可判定的中间点）
        (C2[0], C2[1], 20),         # P4
    ])
    res = fix_drift([seg])

    assert len(res.hits) == 2
    assert res.hits[0].point_index == 1  # 最早命中先删
    assert res.hits[1].point_index == 3

    fixed = res.fixed_segments[0]
    assert [p.lat for p in fixed] == [A[0], C1[0], C2[0]]
    assert fixed[0] is seg[0] and fixed[-1] is seg[-1]

    pts = [tp(A), tp(SPIKE), tp(C1), tp((31.0015, 121.0005)), tp(C2)]
    d_before = sum(haversine_m(a, b) for a, b in zip(pts, pts[1:]))
    d_after = haversine_m(tp(A), tp(C1)) + haversine_m(tp(C1), tp(C2))
    assert abs(res.distance_before_m - d_before) < 1e-9
    assert abs(res.distance_after_m - d_after) < 1e-9


def test_fixed_gpx_roundtrip():
    """修复结果导出 GPX 后可解析：分段数不变、点数为删除后点数、时间保留。"""
    seg = make_seg([
        (A[0], A[1], 0),
        (SPIKE[0], SPIKE[1], 5),
        (C1[0], C1[1], 10),
        (31.0015, 121.0005, 15),
        (C2[0], C2[1], 20),
    ])
    res = fix_drift([seg])
    gpx_bytes = build_gpx(res.fixed_segments, "fixed-demo")
    segments, name = parse_gpx(gpx_bytes)
    assert name == "fixed-demo"
    assert len(segments) == 1
    assert len(segments[0]) == 3
    assert segments[0][0].time == seg[0].time
    assert segments[0][-1].time == seg[-1].time


def test_point_segment_distance_degenerate():
    """线段两端重合时，点到线段距离退化为点到该端点的距离（投影近似，厘米级一致）。"""
    p, a = tp(SPIKE), tp(A)
    assert abs(point_segment_distance_m(p, a, a) - haversine_m(p, a)) < 0.01


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
