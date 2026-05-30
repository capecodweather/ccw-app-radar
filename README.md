# App Radar Pipeline

This folder is for the app-specific radar pipeline.


```

Notes:
- Use the KBOX Level II files from the AWS open-data bucket.
- The official AWS registry shows the current Level II archive bucket as `unidata-nexrad-level2`.
- Keep every exported frame on the same geographic extent so the app can overlay them consistently.
- Transparent PNGs are strongly preferred.
- The renderer now uses a geographic projection (`Cartopy` + `Py-ART`) rather than raw radar x/y space.
- The script checks both the current UTC day and previous UTC day so it still finds recent scans near midnight UTC.
