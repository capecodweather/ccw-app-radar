# App Radar Pipeline

This folder is for the app-specific radar pipeline.

Goal:
- keep the existing website radar script unchanged
- generate a second radar product for the iOS app
- render radar frames without a baked-in map
- publish a `manifest.json` plus transparent PNG frames that the app can load
- keep every frame on one fixed Cape Cod geographic extent so iOS can place it on `MapKit`

Recommended hosting:
- GitHub Pages in a separate repo or branch
- Supabase Storage
- S3 / Cloudflare R2

This repo is set up for GitHub Actions + `gh-pages` publishing.

Expected output structure:

```text
manifest.json
frame_00.png
frame_01.png
...
frame_11.png
```

Example `manifest.json`:

```json
{
  "site": "KBOX",
  "generated_at": "2026-03-25T12:00:00Z",
  "bounds": {
    "north": 42.95,
    "south": 40.75,
    "east": -69.15,
    "west": -71.75
  },
  "frames": [
    {
      "file": "frame_00.png",
      "timestamp": "2026-03-25T11:12:00Z"
    }
  ]
}
```

Notes:
- Use the KBOX Level II files from the AWS open-data bucket.
- The official AWS registry shows the current Level II archive bucket as `unidata-nexrad-level2`.
- Keep every exported frame on the same geographic extent so the app can overlay them consistently.
- Transparent PNGs are strongly preferred.
- The renderer now uses a geographic projection (`Cartopy` + `Py-ART`) rather than raw radar x/y space.
- The script checks both the current UTC day and previous UTC day so it still finds recent scans near midnight UTC.

Current app behavior:
- The iOS app now tries to load `manifest.json` from the configured app radar base URL first.
- If no manifest is available, it falls back to the existing website radar PNG loop.

Deployment:
1. Run `build_app_radar.py`
2. Upload the generated contents of `output/`
3. Point the iOS app to the hosted directory using `APP_RADAR_BASE_URL` in `Info.plist` if needed

Recommended GitHub Pages shape:

```text
https://raw.githubusercontent.com/<org-or-user>/<repo>/gh-pages/
  manifest.json
  frame_00.png
  ...
```

If you use a dedicated repo, the current iOS fallback expectation is:

```text
https://raw.githubusercontent.com/capecodweather/ccw-app-radar/gh-pages
```

Suggested first deployment process:
1. Create a new repo just for app radar output, for example `ccw-app-radar`
2. Add a `gh-pages` branch
3. Run the script locally or in GitHub Actions
4. Copy the contents of `output/` to the root of that branch
5. Build the app and verify the radar switches from legacy image mode to native map mode

GitHub Actions:
- `.github/workflows/build-radar.yml` is included.
- It runs every 5 minutes and can also be triggered manually.
- It builds the frames and publishes the `output/` folder to `gh-pages`.

Before relying on the workflow, do one manual local run first so you can verify:
- the Python dependencies install cleanly
- the generated frames look correct
- the manifest loads in the app
