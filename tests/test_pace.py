"""分段配速统计的单元测试。

可直接运行：python3 tests/test_pace.py
或使用 pytest：python3 -m pytest tests/
"""
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.geo import EARTH_RADIUS_M, TrackPoint, haversine_m
from app.pace import pace_splits

T0 = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)

# 相邻点纬度差 0.001° 时的球面距离（约 111.19 m），测试内统一使用
STEP_M = haversine_m(TrackPoint(lat=31.0, lon=121.0), TrackPoint(lat=31.001, lon=121.0))


def make_seg(points):
    """points: [(lat, lon, 秒偏移), ...]"""
    return [
        TrackPoint(lat=la, lon=lo, ele=500.0, time=T0 + timedelta(seconds=s))
        for la, lo, s in points
    ]


def north_seg(n_points, seconds_per_step=10.0):
    """沿正北等距（0.001° 纬度）等速的轨迹段。"""
    return make_seg([
        (31.0 + i * 0.001, 121.0, i * seconds_per_step) for i in range(n_points)
    ])


def test_constant_speed_pace_and_distances():
    """等速直线：每段距离恰为设定里程，各段配速一致，末尾不足一段也列出。"""
    seg = north_seg(11)  # 10 个采样间隔，总长约 1111.9 m
    per_seg = pace_splits([seg], 0.1)
    assert len(per_seg) == 1
    splits = per_seg[0]

    total = 10 * STEP_M
    full = int(total // 100.0)  # 完整段数
    assert len(splits) == full + 1  # 末尾余量单列一段
    for s in splits[:full]:
        assert s.distance_m == 100.0
    assert abs(splits[-1].distance_m - (total - full * 100.0)) < 1e-6

    # 等速 → 各段配速相同：每 1000 m 耗时 10*1000/STEP_M 秒
    expected_pace = (10.0 * 1000.0 / STEP_M) / 60.0
    for s in splits:
        assert abs(s.pace_min_per_km - expected_pace) < expected_pace * 0.01

    # 段与段首尾相接，整体覆盖轨迹时间
    assert splits[0].start_time == T0
    assert splits[-1].end_time == seg[-1].time
    for prev, nxt in zip(splits, splits[1:]):
        assert prev.end_time == nxt.start_time


def test_one_sample_segment_crosses_multiple_boundaries():
    """一条采样线段跨越多个里程边界：边界时间按距离比例插值。"""
    # 两个点相距 0.005° 纬度（约 556 m），耗时 50 s，每 100 m 一段
    seg = make_seg([(31.0, 121.0, 0), (31.005, 121.0, 50)])
    d = haversine_m(seg[0], seg[1])
    splits = pace_splits([seg], 0.1)[0]

    full = int(d // 100.0)
    assert len(splits) == full + 1
    # 第 k 个边界时间 = T0 + 50 * (100*k)/d
    for k, s in enumerate(splits[:full], start=1):
        expected = T0 + timedelta(seconds=50.0 * (100.0 * k) / d)
        assert abs((s.end_time - expected).total_seconds()) < 1e-3, s.end_time
        assert abs(s.duration_s - 50.0 * 100.0 / d) < 1e-3
    # 末尾不足一段的余量
    assert abs(splits[-1].distance_m - (d - full * 100.0)) < 1e-6
    assert splits[-1].end_time == seg[-1].time


def test_trailing_partial_split_listed():
    """总里程不是整段倍数时，末尾余量单独成段且起止时间正确。"""
    seg = north_seg(3)  # 2 个间隔，总长约 222.4 m
    splits = pace_splits([seg], 0.1)[0]
    assert len(splits) == 3
    tail = splits[-1]
    assert abs(tail.distance_m - (2 * STEP_M - 200.0)) < 1e-6
    # 尾段起点是第二个边界（插值点），终点是轨迹末点
    assert tail.start_time == splits[-2].end_time
    assert tail.end_time == seg[-1].time
    assert T0 < tail.start_time < tail.end_time


def test_trkseg_accumulated_separately():
    """每个原始 trkseg 单独从 0 累计，不跨段合并。"""
    seg1 = north_seg(2)  # 约 111.2 m
    seg2 = make_seg([(31.01, 121.0, 200), (31.011, 121.0, 210)])  # 约 111.2 m
    per_seg = pace_splits([seg1, seg2], 0.1)

    assert len(per_seg) == 2
    for seg_idx, splits in enumerate(per_seg, start=1):
        assert len(splits) == 2  # 各 1 个完整段 + 1 个尾段
        assert all(s.seg_index == seg_idx for s in splits)
        assert [s.split_index for s in splits] == [1, 2]
        assert abs(splits[0].distance_m - 100.0) < 1e-9
        assert abs(splits[1].distance_m - (STEP_M - 100.0)) < 1e-6
    # 第二个 trkseg 的第一段从其自身起点开始，而非接着上一段累计
    assert per_seg[1][0].start_time == seg2[0].time


def test_non_increasing_time_rejected():
    """相邻点时间相同或倒退：提示无法计算。"""
    ok = north_seg(3)
    pace_splits([ok], 1.0)  # 正常轨迹不报错

    same_time = make_seg([(31.0, 121.0, 0), (31.001, 121.0, 0)])
    backwards = make_seg([(31.0, 121.0, 10), (31.001, 121.0, 5)])
    for bad in (same_time, backwards):
        try:
            pace_splits([bad], 1.0)
        except ValueError as exc:
            assert "未递增" in str(exc) and "无法计算" in str(exc)
        else:
            raise AssertionError("时间不递增的轨迹应被拒绝")


def test_zero_distance_no_infinite_pace():
    """零里程段配速为 None，而不是无穷大。"""
    # 整段原地不动（位置不变、时间前进）
    seg = make_seg([(31.0, 121.0, 0), (31.0, 121.0, 30), (31.0, 121.0, 90)])
    splits = pace_splits([seg], 1.0)[0]
    assert len(splits) == 1
    assert splits[0].distance_m == 0.0
    assert splits[0].duration_s == 90.0
    assert splits[0].pace_min_per_km is None

    # 单点 trkseg 同理
    single = pace_splits([make_seg([(31.0, 121.0, 0)])], 1.0)[0]
    assert len(single) == 1
    assert single[0].distance_m == 0.0
    assert single[0].pace_min_per_km is None


def test_exact_multiple_has_no_trailing_split():
    """总里程恰好是整段倍数时，不多出零长度尾段。"""
    # 纯纬度变化时球面距离 = R * Δlat(rad)，反解出每步恰好 100 m 的纬度差
    step_lat = math.degrees(100.0 / EARTH_RADIUS_M)
    seg = make_seg([(31.0 + i * step_lat, 121.0, i * 10) for i in range(5)])
    splits = pace_splits([seg], 0.1)[0]
    assert len(splits) == 4
    for s in splits:
        assert abs(s.distance_m - 100.0) < 1e-6
        assert abs(s.duration_s - 10.0) < 1e-3


def test_split_km_range_validated():
    seg = north_seg(3)
    for bad in (0.05, 0.0, -1.0, 10.5):
        try:
            pace_splits([seg], bad)
        except ValueError as exc:
            assert "每段里程" in str(exc)
        else:
            raise AssertionError(f"split_km={bad} 应被拒绝")
    pace_splits([seg], 0.1)   # 边界值可用
    pace_splits([seg], 10.0)


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
