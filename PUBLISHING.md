# Publishing on the official QGIS plugin repository (plugins.qgis.org)

Runbook for the first submission and for every subsequent release.
Rules below reflect https://plugins.qgis.org/publish/ as of July 2026.

## 1. One-time prerequisites

1. **OSGeo ID.** Register at https://id.osgeo.org — plugins.qgis.org logins
   use OSGeo accounts. (Registration is instant; no membership required.)
2. **Public source repository — done.** The store *requires* working, public
   `repository=` and `tracker=` URLs in `metadata.txt` — a private repo or a
   zip download page gets the plugin rejected. The public mirror of this
   `qgis-plugin/` folder lives at
   https://github.com/swell-d/qgis-360foryou-uploader (GitHub Issues is the
   tracker URL); `metadata.txt` already points there. When plugin files change
   here, push the same changes to the mirror before releasing.
3. **License.** GPL-2.0-or-later is mandatory for QGIS plugins. The LICENSE
   file must be present both in the repo root and *inside* the zipped package
   (both copies already exist here).

## 2. Package checklist (validated automatically on upload)

`python scripts/package.py` produces a compliant zip, but re-check after
changes:

- [ ] the zip contains a single top-level folder `uploader_360foryou/`
- [ ] `metadata.txt` has valid `name`, `version`, `qgisMinimumVersion`,
      `description`, `about`, `author`, `email`, `homepage`, `tracker`,
      `repository`, `changelog`
- [ ] LICENSE and README.md are inside the package
- [ ] no `__pycache__`, `.pyc`, hidden files, tests, or build leftovers
- [ ] no compiled binaries (this plugin ships none — stdlib + QGIS API only;
      external Python dependencies, if ever added, must be declared in `about`)
- [ ] zip is under 20 MB (package.py enforces this)
- [ ] plugin and folder names do not contain the word "plugin"
- [ ] code comments and docs are in English

Before uploading, smoke-test the exact zip locally:
*Plugins → Manage and Install Plugins → Install from ZIP*, then upload a small
project to the production server with a real API key.

## 3. First submission

1. Sign in at https://plugins.qgis.org with the OSGeo ID.
2. *Plugins → Upload a plugin* (https://plugins.qgis.org/plugins/add/) and
   attach `dist/uploader_360foryou.0.1.0.zip`. Metadata is validated on upload;
   fix and re-upload if it complains.
3. **New plugins wait for human approval.** Reviewers approve daily except
   weekends (a Friday upload usually clears on Monday; holidays take longer).
   They check that the metadata links work, the repo is public, and the plugin
   installs and starts without crashing. Answer reviewer feedback in the
   plugin's page/ticket — unanswered remarks stall the approval.
4. `experimental=True` keeps expectations low for 0.x releases; users only see
   the plugin after enabling *Show also experimental plugins* in the plugin
   manager settings. The flag is read from the metadata of each uploaded
   version, so promoting to stable is just `experimental=False` plus a version
   bump in a new upload (done in 1.0.0). Note that users who installed an
   experimental version keep getting updates only while that setting stays on.

## 4. Releasing an update

Versions of an already-approved plugin publish immediately, without re-review:

1. Bump `version=` and extend `changelog=` in
   `uploader_360foryou/metadata.txt`; commit and tag in the public repo.
2. `python scripts/package.py`
3. plugins.qgis.org → *My plugins* → 360ForYou Uploader → *Add version* →
   upload the new zip.

Optional later: automate step 2–3 from CI with
[qgis-plugin-ci](https://github.com/opengisch/qgis-plugin-ci) (reads
metadata.txt, uploads via the plugins.qgis.org API with OSGeo credentials).

## 5. Support expectations

The plugin page shows the tracker link; QGIS users file issues there. Keep the
`email=` in metadata monitored — the review team also uses it. Broken
`homepage`/`tracker`/`repository` links are grounds for unpublishing.
