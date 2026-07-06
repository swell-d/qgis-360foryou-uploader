# -*- coding: utf-8 -*-
"""Persistent plugin settings stored in the QGIS profile (QgsSettings).

The API key is stored in plain text like most service plugins do; it is
scope-limited (projects:read/write) and revocable at <server>/profile#api-keys.
"""
from qgis.core import QgsSettings

from .api_client import BASE_ALLOWED_EXTENSIONS, DEFAULT_CHUNK

GROUP = 'uploader_360foryou'
DEFAULT_SERVER_URL = 'https://360-for-you.com'


def _settings():
    return QgsSettings()


def server_url():
    return _settings().value('%s/server_url' % GROUP, DEFAULT_SERVER_URL, type=str) or DEFAULT_SERVER_URL


def set_server_url(value):
    _settings().setValue('%s/server_url' % GROUP, value)


def api_key():
    return _settings().value('%s/api_key' % GROUP, '', type=str) or ''


def set_api_key(value):
    _settings().setValue('%s/api_key' % GROUP, value)


def privacy_mode():
    try:
        return int(_settings().value('%s/privacy_mode' % GROUP, 2))
    except (TypeError, ValueError):
        return 2


def set_privacy_mode(value):
    _settings().setValue('%s/privacy_mode' % GROUP, int(value))


def chunk_bytes():
    """Hidden tuning knob; edit in the QGIS Advanced Settings editor if needed."""
    try:
        value = int(_settings().value('%s/chunk_bytes' % GROUP, DEFAULT_CHUNK))
        return value if value > 0 else DEFAULT_CHUNK
    except (TypeError, ValueError):
        return DEFAULT_CHUNK


def allowed_extensions():
    """Cached server extension whitelist; refreshed from 415 responses."""
    raw = _settings().value('%s/allowed_extensions' % GROUP, '', type=str) or ''
    exts = [e.strip().lower() for e in raw.split(',') if e.strip()]
    return exts or list(BASE_ALLOWED_EXTENSIONS)


def set_allowed_extensions(exts):
    _settings().setValue('%s/allowed_extensions' % GROUP, ','.join(sorted(set(exts))))
