#!/usr/bin/env python3
"""
Build app-specific KBOX radar frames for the iOS app.

This script:
- queries the latest Level II KBOX files from the AWS open-data bucket
- downloads the latest N files
- renders radar-only PNG frames without a baked-in map
- writes a manifest.json the iOS app can read

The existing website radar pipeline should remain separate.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore import UNSIGNED
from botocore.client import Config

try:
    import cartopy.crs as ccrs
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pyart
    from PIL import Image
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Missing radar rendering dependencies. Install requirements.txt first."
    ) from exc


SITE = os.environ.get("RADAR_SITE", "KBOX")
FRAME_COUNT = int(os.environ.get("RADAR_FRAME_COUNT", "12"))
BUCKET = os.environ.get("NEXRAD_BUCKET", "unidata-nexrad-level2")
OUT_DIR = Path(os.environ.get("APP_RADAR_OUTPUT_DIR", "output"))
DISPLAY_CRS = ccrs.epsg(3857)

@dataclass
class RadarObject:
    key: str
    timestamp: datetime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_object_time(key: str) -> datetime:
    # Expected filename example:
    # KBOX20260325_111757_V06
    name = key.split("/")[-1]
    stamp = name[len(SITE): len(SITE) + 15]
    return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)


def list_recent_objects(site: str, limit: int) -> list[RadarObject]:
    client = boto3.client("s3", config=Config(signature_version=UNSIGNED))
    paginator = client.get_paginator("list_objects_v2")

    objects: list[RadarObject] = []
    # Just after midnight UTC the latest available scans can still be in the
    # previous day's prefix, so check both days and then keep the newest frames.
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
    client = boto3.client("s3", config=Config(signature_version=UNSIGNED))
    client.download_file(BUCKET, key, str(destination))


def compute_bounds(radar) -> dict[str, float]:
    gate_lat = radar.gate_latitude["data"]
    gate_lon = radar.gate_longitude["data"]

    if hasattr(gate_lat, "compressed"):
        lat_values = gate_lat.compressed()
    else:
        lat_values = gate_lat.ravel()

    if hasattr(gate_lon, "compressed"):
        lon_values = gate_lon.compressed()
    else:
        lon_values = gate_lon.ravel()

    return {
        "north": float(lat_values.max()),
        "south": float(lat_values.min()),
        "east": float(lon_values.max()),
        "west": float(lon_values.min()),
    }


def render_frame(radar, output_file: Path, bounds: dict[str, float]) -> None:
    fig = plt.figure(figsize=(8, 8), dpi=256, facecolor=(0, 0, 0, 0))
    ax = fig.add_axes([0, 0, 1, 1], projection=DISPLAY_CRS)
    ax.set_facecolor((0, 0, 0, 0))
    ax.axis("off")

    sweep = 0
    slc = radar.get_slice(sweep)
    gate_lon = radar.gate_longitude["data"][slc]
    gate_lat = radar.gate_latitude["data"][slc]
    reflectivity = radar.fields["reflectivity"]["data"][slc]

    # Draw directly from the true gate lon/lat grid so the exported image and
    # manifest bounds live in the same coordinate space MapKit expects.
    ax.pcolormesh(
        gate_lon,
        gate_lat,
        reflectivity,
        transform=ccrs.PlateCarree(),
        cmap="pyart_NWSRef",
        vmin=-10,
        vmax=75,
        shading="nearest",
    )
    ax.set_extent(
        [bounds["west"], bounds["east"], bounds["south"], bounds["north"]],
        crs=ccrs.PlateCarree(),
    )

    fig.savefig(
        output_file,
        transparent=True,
        pad_inches=0,
    )
    plt.close(fig)


def alpha_crop_box(image_path: Path) -> tuple[int, int, int, int] | None:
    image = Image.open(image_path).convert("RGBA")
    try:
        return image.getchannel("A").getbbox()
    finally:
        image.close()


def union_crop_boxes(
    boxes: list[tuple[int, int, int, int]],
) -> tuple[int, int, int, int] | None:
    if not boxes:
        return None
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[2] for box in boxes)
    bottom = max(box[3] for box in boxes)
    return (left, top, right, bottom)


def crop_frame(image_path: Path, crop_box: tuple[int, int, int, int]) -> None:
    image = Image.open(image_path).convert("RGBA")
    try:
        image.crop(crop_box).save(image_path)
    finally:
        image.close()


def adjust_bounds_for_crop(
    bounds: dict[str, float],
    crop_box: tuple[int, int, int, int],
    image_size: tuple[int, int],
) -> dict[str, float]:
    width, height = image_size
    left, top, right, bottom = crop_box

    lon_span = bounds["east"] - bounds["west"]
    lat_span = bounds["north"] - bounds["south"]

    return {
        "west": bounds["west"] + lon_span * (left / width),
        "east": bounds["west"] + lon_span * (right / width),
        "north": bounds["north"] - lat_span * (top / height),
        "south": bounds["north"] - lat_span * (bottom / height),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    recent = list_recent_objects(SITE, FRAME_COUNT)
    if not recent:
        raise SystemExit("No recent radar objects found.")

    manifest_frames: list[dict] = []
    manifest_bounds: dict[str, float] | None = None
    source_image_size: tuple[int, int] | None = None
    rendered_paths: list[Path] = []
    frame_crop_boxes: list[tuple[int, int, int, int]] = []

    with tempfile.TemporaryDirectory(prefix="ccw_app_radar_") as tmp:
        tmpdir = Path(tmp)
        for index, obj in enumerate(recent):
            src = tmpdir / f"source_{index:02d}.ar2v"
            out = OUT_DIR / f"frame_{index:02d}.png"

            print(f"Downloading {obj.key}")
            download_object(obj.key, src)

            radar = pyart.io.read_nexrad_archive(str(src))
            if manifest_bounds is None:
                manifest_bounds = compute_bounds(radar)

            print(f"Rendering {out.name}")
            render_frame(radar, out, manifest_bounds)

            if source_image_size is None:
                with Image.open(out) as image:
                    source_image_size = image.size

            crop_box = alpha_crop_box(out)
            if crop_box:
                frame_crop_boxes.append(crop_box)
            rendered_paths.append(out)

            manifest_frames.append({
                "file": out.name,
                "timestamp": obj.timestamp.isoformat().replace("+00:00", "Z"),
            })

    if manifest_bounds is None:
        raise SystemExit("No radar bounds computed.")
    if source_image_size is None:
        raise SystemExit("No radar image size computed.")

    crop_box = union_crop_boxes(frame_crop_boxes)
    if crop_box:
        manifest_bounds = adjust_bounds_for_crop(manifest_bounds, crop_box, source_image_size)
        for path in rendered_paths:
            crop_frame(path, crop_box)

    payload = {
        "site": SITE,
        "generated_at": utcnow().isoformat().replace("+00:00", "Z"),
        "bounds": manifest_bounds,
        "frames": manifest_frames,
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {len(manifest_frames)} frames to {OUT_DIR}")


if __name__ == "__main__":
    main()
