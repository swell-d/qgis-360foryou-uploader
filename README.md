# 360ForYou Uploader — development

QGIS plugin (package `uploader_360foryou/`) that uploads QGIS layers and files
to a 360ForYou server through the public API v1 (`<server>/api-guide`,
Swagger at `<server>/api/v1/docs/`). This folder is self-contained and is the
future public source repository (a public repo is required by plugins.qgis.org
— see PUBLISHING.md).

## Layout

```
uploader_360foryou/     the plugin package (this is what gets zipped)
  __init__.py           classFactory
  plugin.py             toolbar/menu action, task/poller registry
  main_dialog.py        upload dialog, completion messages, status poller
  upload_task.py        QgsTask: export -> chunked upload -> create project
  exporters.py          layer assessment + export jobs (raster/vector/point cloud)
  api_client.py         pure-stdlib API client (no qgis imports, unit-testable)
  transport_qgis.py     QgsBlockingNetworkRequest transport (proxy/SSL-aware)
  naming.py             ASCII-safe remote filenames
  plugin_settings.py    QgsSettings persistence
  metadata.txt          plugin metadata (version lives here)
scripts/package.py      builds dist/uploader_360foryou.<version>.zip
tests/                  pytest suite for the pure-Python modules (no QGIS needed)
PUBLISHING.md           how to publish/update on plugins.qgis.org
```

Architecture notes:

- `api_client.py` contains all protocol logic (upload sessions, resumable
  chunks, 409 resync, retries with backoff, typed errors) and talks HTTP
  through a one-method transport. Inside QGIS the transport is
  `QgisBlockingTransport` (honors QGIS proxy/SSL settings, cancelable via
  `QgsFeedback`); tests use a fake, CLI use falls back to `UrllibTransport`.
- Everything that touches QGIS layer objects happens on the main thread
  (`exporters.prepare_export_jobs`: `layer.clone()` for vectors, a
  `QgsRasterPipe` with cloned provider+renderer for rasters). The worker
  thread (`UploadTask.run`) only runs file writers and HTTP — the same
  pattern as QGIS core's own writer tasks.
- Qt5/Qt6 compatible: `qgis.PyQt` imports and fully-qualified enums only.

## Tests

No QGIS required:

```
python -m pytest tests
```

Optional live integration check (against a local dev server of the platform):
run the Flask app, create an API key at `/profile#api-keys`, then exercise
`api_client.ApiClient` against `http://127.0.0.1:5000`. The documented
reference flow lives at <https://360-for-you.com/api-guide>.

## Developer install into QGIS

1. Find the active profile folder: *Settings → User Profiles → Open Active
   Profile Folder*. Typical Windows path:
   `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins`
2. Copy (or symlink/junction) `uploader_360foryou/` there.
3. Enable the plugin in *Plugins → Manage and Install Plugins*.
4. Iterate with the **Plugin Reloader** plugin instead of restarting QGIS.

## Build the release zip

```
python scripts/package.py
```

Produces `dist/uploader_360foryou.<version>.zip` (version from
`uploader_360foryou/metadata.txt`). Sanity-check the zip via
*Plugins → Manage and Install Plugins → Install from ZIP*.

## Language

The plugin ships English-only and no translation catalog is maintained. Keep
user-visible strings in English; the `self.tr(...)` /
`QCoreApplication.translate('UploadDialog', ...)` wrappers around them are Qt
boilerplate that returns the source string unchanged, kept only so a catalog
could be added later without touching every call site.
