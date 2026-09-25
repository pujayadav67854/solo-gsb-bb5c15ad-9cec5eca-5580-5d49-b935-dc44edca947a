"""海拔剖面与累计爬升分析的单元测试。

可直接运行：python3 tests/test_elevation.py
或使用 pytest：python3 -m pytest tests/
"""
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.elevation import (
    _confirmed_climb,
    _median3,
    _resample,
    elevation_profile,
)
from app.geo import EARTH_RADIUS_M, TrackPoint

T0 = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)


def make_seg(points):
    """points: [(lat, lon, ele 或 None, 秒偏移), ...]"""
    return [
        TrackPoint(lat=la, lon=lo, ele=ele, time=T0 + timedelta(seconds=s))
        for la, lo, ele, s in points
    ]


def step_lat(meters):
    """纯纬度变化时球面距离恰为 meters 的纬度差。"""
    return math.degrees(meters / EARTH_RADIUS_M)


def north_seg(eles, step_m=20.0, lat0=31.0, lon=121.0):
    """沿正北等距行进、海拔按 eles 给定的单 trkseg 轨迹。"""
    dlat = step_lat(step_m)
    return make_seg([
        (lat0 + i * dlat, lon, e, i * 10.0) for i, e in enumerate(eles)
    ])


def test_resample_step_and_endpoints():
    """每 20 米重采样，区间端点保留，海拔按里程线性插值。"""
    dists = [0.0, 50.0, 100.0, 150.0, 200.0]
    eles = [0.0, 10.0, 20.0, 30.0, 40.0]
    out = _resample(dists, eles)
    ms = [m for m, _ in out]
    assert len(out) == 11
    assert ms[0] == 0.0 and ms[-1] == 200.0  # 端点保留
    for a, b in zip(ms, ms[1:]):
        assert abs(b - a - 20.0) < 1e-9      # 间隔恰为 20 米
    for m, e in out:
        assert abs(e - m * 0.2) < 1e-9       # 线性插值


def test_resample_short_run_keeps_only_endpoints():
    """不足 20 米的区间：只保留起点和终点。"""
    out = _resample([0.0, 15.0], [100.0, 103.0])
    assert out == [(0.0, 100.0), (15.0, 103.0)]


def test_resample_non_multiple_appends_endpoint():
    """总长不是 20 米倍数时，末尾追加区间终点。"""
    out = _resample([0.0, 55.0], [0.0, 11.0])
    assert [m for m, _ in out] == [0.0, 20.0, 40.0, 55.0]
    assert abs(out[-1][1] - 11.0) < 1e-9


def test_median3_suppresses_isolated_spike():
    """孤立尖峰被三点中位数抑制；端点保持原值；持续变化保留。"""
    assert _median3([100.0, 100.0, 130.0, 100.0, 100.0]) == [100.0] * 5
    assert _median3([130.0, 100.0, 100.0]) == [130.0, 100.0, 100.0]
    assert _median3([100.0, 105.0]) == [100.0, 105.0]  # 不足 3 点不处理
    assert _median3([100.0, 110.0, 120.0]) == [100.0, 110.0, 120.0]


def test_confirmed_climb_basic():
    """5 米反向变化确认峰谷；不足 5 米的反向不确认。"""
    a, d = _confirmed_climb([100.0, 110.0, 104.0, 112.0])
    assert abs(a - 18.0) < 1e-9   # (110-100) + (112-104)
    assert abs(d - 6.0) < 1e-9    # 110-104
    # 回落 3 米不足阈值：不确认峰，上升段延续
    a, d = _confirmed_climb([100.0, 110.0, 107.0, 112.0])
    assert abs(a - 12.0) < 1e-9 and d == 0.0


def test_confirmed_climb_tail_counted():
    """尾端未确认的变化也计入；单调段全程计入。"""
    a, d = _confirmed_climb([100.0, 103.0])   # 不足 5 米的上升
    assert abs(a - 3.0) < 1e-9 and d == 0.0
    a, d = _confirmed_climb([103.0, 100.0])   # 不足 5 米的下降
    assert a == 0.0 and abs(d - 3.0) < 1e-9
    a, d = _confirmed_climb([100.0, 100.0])   # 水平
    assert a == 0.0 and d == 0.0
    a, d = _confirmed_climb([100.0, 120.0])   # 单调上升无反向
    assert abs(a - 20.0) < 1e-9 and d == 0.0


def test_profile_resamples_every_20m():
    """剖面按水平里程每 20 米一个点，端点保留；单调上升累计为总爬升。"""
    seg = north_seg([100.0 + i * 2.0 for i in range(11)])  # 200 m，海拔 100→120
    res = elevation_profile([seg])
    assert res.has_ele
    assert len(res.runs) == 1
    pts = res.runs[0].points
    assert len(pts) == 11
    assert abs(pts[0][0] - 0.0) < 1e-9
    assert abs(pts[-1][0] - 200.0) < 1e-6
    assert abs(res.ascent_m - 20.0) < 1e-6
    assert res.descent_m == 0.0
    assert abs(res.distance_m - 200.0) < 1e-6
    assert abs(res.ele_min - 100.0) < 1e-6 and abs(res.ele_max - 120.0) < 1e-6


def test_missing_ele_breaks_run():
    """缺失海拔处断开为两个区间，不跨缺口插值累计爬升。"""
    seg = north_seg([100.0, 101.0, None, 110.0, 111.0], step_m=50.0)
    res = elevation_profile([seg])
    assert res.has_ele
    assert len(res.runs) == 2
    # 缺口两侧的 9 米差（101→110）不计入爬升
    assert abs(res.ascent_m - 2.0) < 1e-6   # (101-100) + (111-110)
    assert res.descent_m == 0.0
    # 剖面里程连续：第二个区间起点在 150 m 处（含无海拔点的里程）
    assert abs(res.runs[1].start_m - 150.0) < 1e-6
    assert abs(res.distance_m - 200.0) < 1e-6


def test_not_accumulated_across_trkseg():
    """爬升不跨原始 trkseg 累计；各段独立计算后求和。"""
    seg1 = north_seg([100.0, 102.0], step_m=50.0, lat0=31.0, lon=121.0)
    seg2 = north_seg([50.0, 53.0], step_m=50.0, lat0=32.0, lon=122.0)
    res = elevation_profile([seg1, seg2])
    assert len(res.runs) == 2
    # 跨段的 102→50 下降不计入；各段内 2 和 3 米上升计入
    assert abs(res.ascent_m - 5.0) < 1e-6
    assert res.descent_m == 0.0
    # 第二个 trkseg 的剖面里程接着第一段累计（图上 x 轴连续）
    assert abs(res.runs[1].start_m - 50.0) < 1e-6
    assert abs(res.distance_m - 100.0) < 1e-6


def test_spike_not_counted_in_profile():
    """孤立尖峰经三点中位数抑制后不计入爬升。"""
    seg = north_seg([100.0, 100.0, 100.0, 130.0, 100.0, 100.0, 100.0])
    res = elevation_profile([seg])
    assert abs(res.ascent_m) < 1e-9
    assert abs(res.descent_m) < 1e-9


def test_no_ele_at_all():
    """全无有效海拔：has_ele=False，爬升/下降为 None（页面显示"无海拔数据"）。"""
    seg = north_seg([None, None, None, None], step_m=50.0)
    res = elevation_profile([seg])
    assert not res.has_ele
    assert res.ascent_m is None and res.descent_m is None
    assert res.runs == []
    assert abs(res.distance_m - 150.0) < 1e-6  # 里程仍统计


def test_single_point_run():
    """只有一个有效海拔点的区间：剖面一个点，爬升为 0，不报错。"""
    seg = north_seg([None, 88.0, None], step_m=50.0)
    res = elevation_profile([seg])
    assert res.has_ele
    assert len(res.runs) == 1
    assert len(res.runs[0].points) == 1
    assert res.ascent_m == 0.0 and res.descent_m == 0.0
    assert abs(res.runs[0].points[0][0] - 50.0) < 1e-6  # 单点位于 50 m 处


def test_v_shape_profile():
    """V 形剖面（整体联调）：下降与上升分别累计。"""
    seg = north_seg([100.0, 95.0, 90.0, 90.0, 95.0, 100.0, 104.0])
    res = elevation_profile([seg])
    assert abs(res.ascent_m - 14.0) < 1e-6   # 90→104
    assert abs(res.descent_m - 10.0) < 1e-6  # 100→90


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
