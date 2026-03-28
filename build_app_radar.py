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
from typing import Iterable

import boto3
from botocore import UNSIGNED
from botocore.client import Config

try:
    import cartopy.crs as ccrs
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pyart
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Missing radar rendering dependencies. Install requirements.txt first."
    ) from exc


SITE = os.environ.get("RADAR_SITE", "KBOX")
FRAME_COUNT = int(os.environ.get("RADAR_FRAME_COUNT", "12"))
BUCKET = os.environ.get("NEXRAD_BUCKET", "unidata-nexrad-level2")
OUT_DIR = Path(os.environ.get("APP_RADAR_OUTPUT_DIR", "output"))

# Cape Cod-focused extent for app overlay use.
BOUNDS = {
    "north": 42.95,
    "south": 40.75,
    "east": -69.15,
    "west": -71.75,
}


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


def render_frame(source_file: Path, output_file: Path) -> None:
    radar = pyart.io.read(str(source_file))

    fig = plt.figure(figsize=(8, 8), dpi=256, facecolor=(0, 0, 0, 0))
    ax = fig.add_axes([0, 0, 1, 1], projection=ccrs.PlateCarree())
    ax.set_facecolor((0, 0, 0, 0))
    ax.axis("off")

    display = pyart.graph.RadarMapDisplay(radar)
    display.plot_ppi_map(
        "reflectivity",
        sweep=0,
        ax=ax,
        projection=ccrs.PlateCarree(),
        cmap="pyart_NWSRef",
        vmin=-10,
        vmax=75,
        min_lon=BOUNDS["west"],
        max_lon=BOUNDS["east"],
        min_lat=BOUNDS["south"],
        max_lat=BOUNDS["north"],
        lon_lines=[],
        lat_lines=[],
        embellish=False,
        colorbar_flag=False,
        title_flag=False,
    )
    ax.set_extent(
        [BOUNDS["west"], BOUNDS["east"], BOUNDS["south"], BOUNDS["north"]],
        crs=ccrs.PlateCarree(),
    )

    fig.savefig(
        output_file,
        transparent=True,
        pad_inches=0,
    )
    plt.close(fig)


def write_manifest(frames: Iterable[dict], destination: Path) -> None:
    payload = {
        "site": SITE,
        "generated_at": utcnow().isoformat().replace("+00:00", "Z"),
        "bounds": BOUNDS,
        "frames": list(frames),
    }
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    recent = list_recent_objects(SITE, FRAME_COUNT)
    if not recent:
        raise SystemExit("No recent radar objects found.")

    manifest_frames: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="ccw_app_radar_") as tmp:
        tmpdir = Path(tmp)
        for index, obj in enumerate(recent):
            src = tmpdir / f"source_{index:02d}.ar2v"
            out = OUT_DIR / f"frame_{index:02d}.png"

            print(f"Downloading {obj.key}")
            download_object(obj.key, src)

            print(f"Rendering {out.name}")
            render_frame(src, out)

            manifest_frames.append({
                "file": out.name,
                "timestamp": obj.timestamp.isoformat().replace("+00:00", "Z"),
            })

    write_manifest(manifest_frames, OUT_DIR / "manifest.json")
    print(f"Wrote {len(manifest_frames)} frames to {OUT_DIR}")


if __name__ == "__main__":
    main()
