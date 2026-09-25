"""GPX 1.1 解析与生成（仅处理 trk/trkseg/trkpt，要求每个轨迹点含 time）。"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .geo import TrackPoint

GPX_NS = "http://www.topografix.com/GPX/1/1"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_time(text: str):
    text = text.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_gpx(content: bytes):
    """解析 GPX，返回 (segments, name)；segments 为 list[list[TrackPoint]]。"""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"GPX 解析失败：{exc}") from exc

    segments = []
    track_name = None
    for trk in root.iter():
        if _local(trk.tag) != "trk":
            continue
        for child in trk:
            tag = _local(child.tag)
            if tag == "name":
                if track_name is None:
                    track_name = (child.text or "").strip() or None
            elif tag == "trkseg":
                pts = []
                for trkpt in child:
                    if _local(trkpt.tag) != "trkpt":
                        continue
                    lat, lon = trkpt.get("lat"), trkpt.get("lon")
                    if lat is None or lon is None:
                        continue
                    ele = time = None
                    for sub in trkpt:
                        stag = _local(sub.tag)
                        if stag == "ele" and sub.text:
                            try:
                                ele = float(sub.text.strip())
                            except ValueError:
                                ele = None
                        elif stag == "time" and sub.text and sub.text.strip():
                            time = _parse_time(sub.text)
                    if time is None:
                        raise ValueError("存在缺少时间的轨迹点，请上传含时间的 GPX 文件")
                    pts.append(TrackPoint(lat=float(lat), lon=float(lon), ele=ele, time=time))
                if pts:
                    segments.append(pts)
    if not segments:
        raise ValueError("GPX 中未找到轨迹（trk/trkseg/trkpt）")
    return segments, track_name or "track"


def _fmt_time(dt) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    spec = "milliseconds" if dt.microsecond else "seconds"
    return dt.isoformat(timespec=spec).replace("+00:00", "Z")


def build_gpx(segments, name: str = "masked-track") -> bytes:
    """把可见分段导出为一条 trk 下多个互不连接的 trkseg。"""
    ET.register_namespace("", GPX_NS)
    gpx = ET.Element("gpx", {
        "version": "1.1",
        "creator": "gpx-track-masker",
        "xmlns": GPX_NS,
    })
    trk = ET.SubElement(gpx, "trk")
    ET.SubElement(trk, "name").text = name
    for seg in segments:
        trkseg = ET.SubElement(trk, "trkseg")
        for p in seg:
            trkpt = ET.SubElement(trkseg, "trkpt", {
                "lat": f"{p.lat:.7f}",
                "lon": f"{p.lon:.7f}",
            })
            if p.ele is not None:
                ET.SubElement(trkpt, "ele").text = f"{p.ele:.2f}"
            if p.time is not None:
                ET.SubElement(trkpt, "time").text = _fmt_time(p.time)
    return ET.tostring(gpx, encoding="utf-8", xml_declaration=True)
