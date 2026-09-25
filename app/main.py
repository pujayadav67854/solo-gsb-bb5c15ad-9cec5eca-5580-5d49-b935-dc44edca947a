"""FastAPI 入口：上传 GPX、遮蔽计算、导出、分段配速、海拔分析、静态页面。"""
from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .drift import MAX_SPEED_MPS, MIN_OFFSET_M, fix_drift
from .elevation import RESAMPLE_STEP_M, REVERSAL_THRESHOLD_M, elevation_profile
from .geo import MaskCircle, haversine_m, mask_track
from .gpxutil import build_gpx, parse_gpx
from .pace import DEFAULT_SPLIT_KM, MAX_SPLIT_KM, MIN_SPLIT_KM, pace_splits
from .stay import MAX_GAP_SECONDS, MIN_STAY_SECONDS, STAY_RADIUS_M, detect_stays

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

app = FastAPI(title="徒步轨迹分享 · 敏感地点遮蔽")

# 内存存储（演示用途，重启即清空）：track_id -> {"segments": ..., "name": ...}
TRACKS: dict = {}


class CircleIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: float = Field(gt=0, le=100_000)


class MaskRequest(BaseModel):
    track_id: str
    circles: list[CircleIn] = []


class PaceRequest(BaseModel):
    track_id: str
    split_km: float = Field(default=DEFAULT_SPLIT_KM, ge=MIN_SPLIT_KM, le=MAX_SPLIT_KM)


class ElevationRequest(BaseModel):
    track_id: str


class DriftRequest(BaseModel):
    track_id: str


class StayRequest(BaseModel):
    track_id: str


def _point_dict(p):
    return {
        "lat": p.lat,
        "lon": p.lon,
        "ele": p.ele,
        "time": p.time.isoformat() if p.time else None,
    }


def _get_track(track_id: str):
    track = TRACKS.get(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="轨迹不存在或已过期，请重新上传")
    return track


def _run_mask(track, circles_in):
    circles = [MaskCircle(lat=c.lat, lon=c.lon, radius_m=c.radius_m) for c in circles_in]
    return mask_track(track["segments"], circles)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/upload")
async def upload(file: UploadFile):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 50 MB 限制")
    try:
        segments, name = parse_gpx(content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    track_id = uuid.uuid4().hex[:12]
    TRACKS[track_id] = {"segments": segments, "name": name}

    distance = sum(
        haversine_m(a, b) for seg in segments for a, b in zip(seg, seg[1:])
    )
    times = [p.time for seg in segments for p in seg if p.time]
    return {
        "track_id": track_id,
        "name": name,
        "segments": [[_point_dict(p) for p in seg] for seg in segments],
        "stats": {
            "points": sum(len(s) for s in segments),
            "segments": len(segments),
            "distance_m": distance,
            "start_time": min(times).isoformat() if times else None,
            "end_time": max(times).isoformat() if times else None,
        },
    }


@app.post("/api/mask")
def mask(req: MaskRequest):
    track = _get_track(req.track_id)
    result = _run_mask(track, req.circles)
    return {
        "visible_segments": [[_point_dict(p) for p in seg] for seg in result.visible_segments],
        "hidden_segments": [[_point_dict(p) for p in seg] for seg in result.hidden_segments],
        "hidden_intervals": result.hidden_intervals,
        "distance_before_m": result.distance_before_m,
        "distance_after_m": result.distance_after_m,
        "visible_points": sum(len(s) for s in result.visible_segments),
    }


@app.post("/api/export")
def export(req: MaskRequest):
    track = _get_track(req.track_id)
    result = _run_mask(track, req.circles)
    name = f"{track['name']}-masked"
    gpx_bytes = build_gpx(result.visible_segments, name)
    filename = quote(f"{name}.gpx")
    return Response(
        content=gpx_bytes,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


def _split_dict(s):
    return {
        "seg_index": s.seg_index,
        "split_index": s.split_index,
        "distance_m": s.distance_m,
        "duration_s": s.duration_s,
        "pace_min_per_km": s.pace_min_per_km,
        "start_time": s.start_time.isoformat(),
        "end_time": s.end_time.isoformat(),
    }


@app.post("/api/pace")
def pace(req: PaceRequest):
    """分段配速：按 split_km 公里把每个原始 trkseg 切段，返回每段距离/耗时/配速/起止时间。"""
    track = _get_track(req.track_id)
    try:
        per_segment = pace_splits(track["segments"], req.split_km)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    segments_out = [
        {
            "seg_index": seg_idx,
            "distance_m": sum(s.distance_m for s in splits),
            "splits": [_split_dict(s) for s in splits],
        }
        for seg_idx, splits in enumerate(per_segment, start=1)
    ]
    total_distance = sum(g["distance_m"] for g in segments_out)
    total_duration = sum(s.duration_s for splits in per_segment for s in splits)
    return {
        "split_km": req.split_km,
        "segments": segments_out,
        "totals": {
            "distance_m": total_distance,
            "duration_s": total_duration,
            "pace_min_per_km": (
                total_duration / 60.0 / (total_distance / 1000.0)
                if total_distance > 0 else None
            ),
        },
    }


@app.post("/api/elevation")
def elevation(req: ElevationRequest):
    """海拔剖面与累计爬升：基于上传时保存的原始轨迹计算，与遮蔽裁切无关。"""
    track = _get_track(req.track_id)
    result = elevation_profile(track["segments"])
    return {
        "has_ele": result.has_ele,
        "sample_step_m": RESAMPLE_STEP_M,
        "reversal_threshold_m": REVERSAL_THRESHOLD_M,
        "distance_m": result.distance_m,
        "ascent_m": result.ascent_m,
        "descent_m": result.descent_m,
        "ele_min": result.ele_min,
        "ele_max": result.ele_max,
        "runs": [
            {
                "seg_index": r.seg_index,
                "start_m": r.start_m,
                "points": [{"d": d, "ele": e} for d, e in r.points],
            }
            for r in result.runs
        ],
    }


def _hit_dict(h):
    return {
        "seg_index": h.seg_index,
        "point_index": h.point_index,
        "lat": h.lat,
        "lon": h.lon,
        "time": h.time.isoformat() if h.time else None,
        "speed_in_mps": h.speed_in_mps,
        "speed_out_mps": h.speed_out_mps,
        "speed_bypass_mps": h.speed_bypass_mps,
        "offset_m": h.offset_m,
    }


def _run_drift(track):
    """对原始轨迹执行漂移修复；时间不递增时转为 400 明确提示。"""
    try:
        return fix_drift(track["segments"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/drift")
def drift(req: DriftRequest):
    """单点漂移检测与修复：基于上传时保存的原始轨迹计算，不影响遮蔽与配速。"""
    track = _get_track(req.track_id)
    result = _run_drift(track)
    return {
        "hit_count": len(result.hits),
        "hits": [_hit_dict(h) for h in result.hits],
        "distance_before_m": result.distance_before_m,
        "distance_after_m": result.distance_after_m,
        "points_before": sum(len(s) for s in track["segments"]),
        "points_after": sum(len(s) for s in result.fixed_segments),
        "thresholds": {
            "max_speed_mps": MAX_SPEED_MPS,
            "min_offset_m": MIN_OFFSET_M,
        },
    }


@app.post("/api/drift/export")
def drift_export(req: DriftRequest):
    """导出修复后的 GPX：删除全部命中跳点，保留原始 trkseg 结构与首尾点。"""
    track = _get_track(req.track_id)
    result = _run_drift(track)
    name = f"{track['name']}-fixed"
    gpx_bytes = build_gpx(result.fixed_segments, name)
    filename = quote(f"{name}.gpx")
    return Response(
        content=gpx_bytes,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


def _stay_dict(s):
    return {
        "seg_index": s.seg_index,
        "start_index": s.start_index,
        "end_index": s.end_index,
        "point_count": s.point_count,
        "lat": s.lat,
        "lon": s.lon,
        "start_time": s.start_time.isoformat(),
        "end_time": s.end_time.isoformat(),
        "duration_s": s.duration_s,
    }


@app.post("/api/stay")
def stay(req: StayRequest):
    """停留识别：基于已上传的 track_id 计算停留点与全程记录/停留/移动时长。"""
    track = _get_track(req.track_id)
    try:
        result = detect_stays(track["segments"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "stay_count": len(result.stays),
        "stays": [_stay_dict(s) for s in result.stays],
        "record_duration_s": result.record_duration_s,
        "stay_duration_s": result.stay_duration_s,
        "moving_duration_s": result.moving_duration_s,
        "gap_count": result.gap_count,
        "gap_duration_s": result.gap_duration_s,
        "thresholds": {
            "radius_m": STAY_RADIUS_M,
            "min_stay_s": MIN_STAY_SECONDS,
            "max_gap_s": MAX_GAP_SECONDS,
        },
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
