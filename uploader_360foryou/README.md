# 360ForYou Uploader

QGIS plugin that uploads data from the current QGIS project to
[360-for-you.com](https://360-for-you.com) (or a self-hosted 360ForYou server)
and creates a shareable viewer project.

## What it uploads

| QGIS layer / input        | Sent to the server as                                        |
|---------------------------|--------------------------------------------------------------|
| Raster layer              | GeoTIFF — the source file as-is when it is already an RGB(A) Byte GeoTIFF, otherwise a rendered RGBA GeoTIFF (what you see in QGIS) |
| Vector layer              | KML (EPSG:4326), styling exported best-effort                |
| Point cloud layer         | its LAS/LAZ source file, unchanged                           |
| "Additional files" picker | any file type the server accepts (IFC, E57, panoramas, ...)  |

Layers without a valid CRS are listed but disabled — the server cannot
position them. Uploads are chunked and resumable and run as a background task
in the QGIS task manager (cancelable). When processing finishes, a message bar
notification offers to open the new project in the browser.

## Requirements

- QGIS 3.40 or newer (Qt5 and Qt6 builds supported)
- A 360ForYou account with API access and an API key with scopes
  `projects:read` and `projects:write` — create one under
  **Profile → API keys** (`<server>/profile#api-keys`)

## Usage

1. Click the 360ForYou toolbar button (or *Plugins → 360ForYou Uploader*).
2. Enter the server URL (self-hosted installations only — the default is
   360-for-you.com) and your API key; *Test connection* verifies both.
3. Check the layers to upload, optionally add files from disk.
4. Set the project title and privacy, press **Upload**.

## License

GPL-2.0-or-later — see LICENSE.
