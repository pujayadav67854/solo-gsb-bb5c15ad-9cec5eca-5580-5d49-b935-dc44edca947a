"""核心裁切逻辑与 GPX 读写的单元测试。

可直接运行：python3 tests/test_geo.py
或使用 pytest：python3 -m pytest tests/
"""
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.geo import MaskCircle, TrackPoint, haversine_m, mask_track
from app.gpxutil import build_gpx, parse_gpx

T0 = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)


def make_seg(points):
    """points: [(lat, lon, 秒偏移), ...]"""
    return [
        TrackPoint(lat=la, lon=lo, ele=500.0, time=T0 + timedelta(seconds=s))
        for la, lo, s in points
    ]


def test_crossing_with_both_endpoints_outside():
    """相邻采样点都在圆外、但线段穿越圆：必须识别并裁切。"""
    center = TrackPoint(lat=31.0, lon=121.0)
    # 端点距圆心约 222.4 m（0.002 纬度），圆半径 100 m，线段穿过圆心
    seg = make_seg([(30.998, 121.0, 0), (31.002, 121.0, 100)])
    res = mask_track([seg], [MaskCircle(lat=31.0, lon=121.0, radius_m=100.0)])

    assert res.hidden_intervals == 1
    assert len(res.visible_segments) == 2  # 被裁成互不连接的两段

    # 裁切点应恰好落在圆边界上（误差 < 0.5 m）
    for boundary in (res.visible_segments[0][-1], res.visible_segments[1][0]):
        d = haversine_m(boundary, center)
        assert abs(d - 100.0) < 0.5, f"交点距圆心 {d} m"

    # 交点时间按比例插值：t = (222.39-100)/(2*222.39) ≈ 0.2752 → 约 27.5 s
    t_cross = res.visible_segments[0][-1].time
    expected = T0 + timedelta(seconds=27.52)
    assert abs((t_cross - expected).total_seconds()) < 0.5, t_cross

    # 可见距离 ≈ 2*(222.39-100)，隐藏距离 ≈ 200 m
    assert abs(res.distance_after_m - 244.8) < 2.0
    assert abs((res.distance_before_m - res.distance_after_m) - 200.0) < 2.0


def test_no_circles_keeps_everything():
    seg = make_seg([(31.0, 121.0, 0), (31.001, 121.001, 60), (31.002, 121.0, 120)])
    res = mask_track([seg], [])
    assert res.hidden_intervals == 0
    assert len(res.visible_segments) == 1
    assert len(res.visible_segments[0]) == 3
    assert abs(res.distance_before_m - res.distance_after_m) < 1e-6


def test_original_segment_boundaries_preserved():
    """无遮蔽时，原始 trkseg 边界必须保留（不合并）。"""
    seg1 = make_seg([(31.0, 121.0, 0), (31.001, 121.0, 60)])
    seg2 = make_seg([(31.001, 121.0, 60), (31.002, 121.0, 120)])
    res = mask_track([seg1, seg2], [])
    assert len(res.visible_segments) == 2


def test_point_inside_circle():
    """中间采样点落入圆内：两端可见、中间被裁。"""
    seg = make_seg([
        (30.996, 121.0, 0),
        (31.000, 121.0, 100),   # 圆心处
        (31.004, 121.0, 200),
    ])
    res = mask_track([seg], [MaskCircle(lat=31.0, lon=121.0, radius_m=150.0)])
    assert res.hidden_intervals == 1
    assert len(res.visible_segments) == 2
    # 隐藏段包含原始中间点
    hidden = res.hidden_segments[0]
    assert any(abs(p.lat - 31.0) < 1e-9 for p in hidden)


def test_entire_segment_inside():
    seg = make_seg([(31.0000, 121.0, 0), (31.0005, 121.0, 30)])
    res = mask_track([seg], [MaskCircle(lat=31.0, lon=121.0, radius_m=500.0)])
    assert res.hidden_intervals == 1
    assert res.visible_segments == []
    assert res.distance_after_m == 0.0


def test_multiple_circles():
    seg = make_seg([(30.998, 121.0, 0), (31.002, 121.0, 100)])
    circles = [
        MaskCircle(lat=30.999, lon=121.0, radius_m=50.0),
        MaskCircle(lat=31.001, lon=121.0, radius_m=50.0),
    ]
    res = mask_track([seg], circles)
    assert res.hidden_intervals == 2
    assert len(res.visible_segments) == 3


def test_gpx_roundtrip_and_trkseg_export():
    seg1 = make_seg([(31.0, 121.0, 0), (31.001, 121.0, 60)])
    seg2 = make_seg([(31.002, 121.0, 120), (31.003, 121.0, 180)])
    gpx_bytes = build_gpx([seg1, seg2], "demo-masked")
    text = gpx_bytes.decode("utf-8")
    assert text.count("<trkseg>") == 2  # 互不连接的两个 trkseg
    assert "demo-masked" in text
    assert "<time>" in text and text.rstrip().endswith("</gpx>")

    segments, name = parse_gpx(gpx_bytes)
    assert name == "demo-masked"
    assert len(segments) == 2
    assert segments[0][0].time == T0
    assert abs(segments[1][1].lat - 31.003) < 1e-7


def test_interpolated_time_in_export():
    """裁切产生的新端点应带插值时间并写入 GPX。"""
    seg = make_seg([(30.998, 121.0, 0), (31.002, 121.0, 100)])
    res = mask_track([seg], [MaskCircle(lat=31.0, lon=121.0, radius_m=100.0)])
    gpx_bytes = build_gpx(res.visible_segments, "t")
    segments, _ = parse_gpx(gpx_bytes)
    boundary_time = segments[0][-1].time
    assert T0 < boundary_time < T0 + timedelta(seconds=100)
    # 插值时间带小数秒，导出后仍精确到毫秒
    assert abs((boundary_time - (T0 + timedelta(seconds=27.52))).total_seconds()) < 0.5


def test_parse_rejects_missing_time():
    gpx = b"""<?xml version="1.0"?>
    <gpx version="1.1" creator="x" xmlns="http://www.topografix.com/GPX/1/1">
      <trk><name>no-time</name><trkseg>
        <trkpt lat="31.0" lon="121.0"><ele>10</ele></trkpt>
      </trkseg></trk>
    </gpx>"""
    try:
        parse_gpx(gpx)
    except ValueError as exc:
        assert "时间" in str(exc)
    else:
        raise AssertionError("缺少时间的 GPX 应被拒绝")


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
