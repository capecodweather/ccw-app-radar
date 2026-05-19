#!/usr/bin/env python3
"""
Incremental app radar builder for the iOS app.

This variant is designed to reduce per-run work:
- reads the existing manifest.json if present
- downloads and renders only newly-seen scans
- names PNG files by radar timestamp
- keeps only the newest N frames in manifest.json
- leaves older PNGs on disk for optional later cleanup
"""

from __future__ import annotations

import gc
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore import UNSIGNED
from botocore.client import Config

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pyart
except Exception as exc:
    raise SystemExit(
        "Missing radar rendering dependencies. Install requirements.txt first."
    ) from exc


SITE = os.environ.get("RADAR_SITE", "KBOX")
FRAME_LIMIT = int(os.environ.get("RADAR_FRAME_COUNT", "12"))
OBJECT_SCAN_LIMIT = int(os.environ.get("RADAR_OBJECT_SCAN_LIMIT", "24"))
BUCKET = os.environ.get("NEXRAD_BUCKET", "unidata-nexrad-level2")
OUT_DIR = Path(os.environ.get("APP_RADAR_OUTPUT_DIR", "output"))
MANIFEST_PATH = OUT_DIR / "manifest.json"
FIGURE_SIZE = float(os.environ.get("APP_RADAR_FIGURE_SIZE", "6"))
FIGURE_DPI = int(os.environ.get("APP_RADAR_DPI", "160"))
WEB_MERCATOR_LIMIT = 85.05112878

S3_CLIENT = boto3.client("s3", config=Config(signature_version=UNSIGNED))


@dataclass
class RadarObject:
    key: str
    timestamp: datetime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_object_time(key: str) -> datetime:
    name = key.split("/")[-1]
    stamp = name[len(SITE): len(SITE) + 15]
    return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)


def timestamp_to_png_name(timestamp: datetime) -> str:
    return f"{SITE}{timestamp.strftime('%Y%m%d_%H%M%S')}.png"


def parse_manifest_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def list_recent_objects(site: str, limit: int) -> list[RadarObject]:
    paginator = S3_CLIENT.get_paginator("list_objects_v2")
    objects: list[RadarObject] = []

    for offset_days in (0, 1):
        prefix_time = utcnow() - timedelta(days=offset_days)
        prefix = prefix_time.strftime(f"%Y/%m/%d/{site}/")
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item["Key"]
                if key.endswith("_MDM"):
                    continue
                try:
                    objects.append(RadarObject(key=key, timestamp=parse_object_time(key)))
                except Exception:
                    continue

    objects.sort(key=lambda item: item.timestamp, reverse=True)
    return list(reversed(objects[:limit]))


def download_object(key: str, destination: Path) -> None:
    S3_CLIENT.download_file(BUCKET, key, str(destination))


def compute_bounds(radar) -> dict[str, float]:
    gate_lat = radar.gate_latitude["data"]
    gate_lon = radar.gate_longitude["data"]

    lat_values = gate_lat.compressed() if hasattr(gate_lat, "compressed") else gate_lat.ravel()
    lon_values = gate_lon.compressed() if hasattr(gate_lon, "compressed") else gate_lon.ravel()

    return {
        "north": float(lat_values.max()),
        "south": float(lat_values.min()),
        "east": float(lon_values.max()),
        "west": float(lon_values.min()),
    }


def render_frame(radar, output_file: Path, bounds: dict[str, float]) -> None:
    fig = plt.figure(figsize=(FIGURE_SIZE, FIGURE_SIZE), dpi=FIGURE_DPI, facecolor=(0, 0, 0, 0))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor((0, 0, 0, 0))
    ax.axis("off")

    sweep = 0
    slc = radar.get_slice(sweep)
    gate_lon = radar.gate_longitude["data"][slc]
    gate_lat = radar.gate_latitude["data"][slc]
    reflectivity = radar.fields["reflectivity"]["data"][slc]

    mercator_x, mercator_y = lonlat_to_web_mercator(gate_lon, gate_lat)
    west_x, south_y = lonlat_to_web_mercator(bounds["west"], bounds["south"])
    east_x, north_y = lonlat_to_web_mercator(bounds["east"], bounds["north"])

    ax.pcolormesh(
        mercator_x,
        mercator_y,
        reflectivity,
        cmap="pyart_NWSRef",
        vmin=-10,
        vmax=75,
        shading="nearest",
    )
    ax.set_xlim(west_x, east_x)
    ax.set_ylim(south_y, north_y)
    ax.set_aspect("equal")

    fig.savefig(output_file, transparent=True, pad_inches=0)
    plt.close(fig)


def lonlat_to_web_mercator(lon, lat):
    lat = clamp_latitude(lat)
    x = lon * 20037508.34 / 180.0
    y = log_tan_mercator(lat)
    return x, y


def clamp_latitude(lat):
    if hasattr(lat, "clip"):
        return lat.clip(-WEB_MERCATOR_LIMIT, WEB_MERCATOR_LIMIT)
    return max(min(lat, WEB_MERCATOR_LIMIT), -WEB_MERCATOR_LIMIT)


def log_tan_mercator(lat):
    if hasattr(lat, "__array__"):
        import numpy as np
        return np.log(np.tan((90.0 + lat) * math.pi / 360.0)) * (20037508.34 / math.pi)
    return math.log(math.tan((90.0 + lat) * math.pi / 360.0)) * (20037508.34 / math.pi)


def load_existing_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"site": SITE, "generated_at": "", "bounds": None, "frames": []}

    try:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Failed to read existing manifest: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit("Existing manifest.json is not a JSON object.")

    frames = payload.get("frames")
    if not isinstance(frames, list):
        payload["frames"] = []
    if "bounds" not in payload:
        payload["bounds"] = None
    return payload


def cleanup_large_objects(*objects) -> None:
    for obj in objects:
        try:
            del obj
        except Exception:
            pass
    gc.collect()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest = load_existing_manifest()
    existing_frames = manifest.get("frames", [])
    existing_files = {
        str(frame.get("file", "")).strip()
        for frame in existing_frames
        if isinstance(frame, dict) and str(frame.get("file", "")).strip()
    }
    existing_timestamps = {
        str(frame.get("timestamp", "")).strip()
        for frame in existing_frames
        if isinstance(frame, dict) and str(frame.get("timestamp", "")).strip()
    }

    recent = list_recent_objects(SITE, OBJECT_SCAN_LIMIT)
    if not recent:
        raise SystemExit("No recent radar objects found.")

    newest_known_time = max(
        (parse_manifest_timestamp(ts) for ts in existing_timestamps),
        default=None,
    )

    new_objects = []
    for obj in recent:
        png_name = timestamp_to_png_name(obj.timestamp)
        timestamp_text = obj.timestamp.isoformat().replace("+00:00", "Z")
        if png_name in existing_files or timestamp_text in existing_timestamps:
            continue
        if newest_known_time and obj.timestamp <= newest_known_time:
            continue
        new_objects.append(obj)

    if not new_objects:
        print("No new radar scans detected.")
        return

    bounds = manifest.get("bounds")
    if bounds is not None and not isinstance(bounds, dict):
        raise SystemExit("Existing manifest bounds are invalid.")

    appended_frames: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="ccw_app_radar_") as tmp:
        tmpdir = Path(tmp)

        for index, obj in enumerate(new_objects):
            src = tmpdir / f"source_{index:02d}.ar2v"
            png_name = timestamp_to_png_name(obj.timestamp)
            out = OUT_DIR / png_name

            print(f"[{index + 1}/{len(new_objects)}] Downloading {obj.key}")
            download_object(obj.key, src)

            radar = pyart.io.read_nexrad_archive(str(src))
            if bounds is None:
                bounds = compute_bounds(radar)

            print(f"[{index + 1}/{len(new_objects)}] Rendering {png_name}")
            render_frame(radar, out, bounds)

            appended_frames.append({
                "file": png_name,
                "timestamp": obj.timestamp.isoformat().replace("+00:00", "Z"),
            })

            cleanup_large_objects(radar)
            try:
                src.unlink(missing_ok=True)
            except Exception:
                pass

    if bounds is None:
        raise SystemExit("No radar bounds available.")

    merged_frames = [
        frame for frame in existing_frames
        if isinstance(frame, dict)
        and str(frame.get("file", "")).strip()
        and str(frame.get("timestamp", "")).strip()
    ] + appended_frames

    merged_frames.sort(key=lambda frame: parse_manifest_timestamp(str(frame["timestamp"])))
    merged_frames = merged_frames[-FRAME_LIMIT:]

    payload = {
        "site": SITE,
        "generated_at": utcnow().isoformat().replace("+00:00", "Z"),
        "bounds": bounds,
        "frames": merged_frames,
    }
    MANIFEST_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Rendered {len(appended_frames)} new frame(s).")
    print(f"Manifest now references {len(merged_frames)} frame(s).")


if __name__ == "__main__":
    main()
