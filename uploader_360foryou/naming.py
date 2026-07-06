# -*- coding: utf-8 -*-
"""ASCII-safe remote filename helpers.

Pure Python standard library — no qgis/PyQt imports (unit-testable anywhere).

The server flattens uploaded filenames with its own secure_filename(); behavior
for non-ASCII names may differ between deployments, so the client always sends
deterministic ASCII slugs.
"""
import re
import unicodedata

_ALLOWED_RE = re.compile(r'[^A-Za-z0-9._-]+')
_EDGE_RE = re.compile(r'^[._\s-]+|[._\s-]+$')
_MAX_STEM = 100


def ascii_slug(text, fallback='layer'):
    """Reduce arbitrary text to a safe ASCII file stem.

    NFKD-decompose and drop non-ASCII, collapse disallowed runs to '_',
    strip leading/trailing dots/dashes/underscores (prevents dot-files),
    and fall back when nothing survives (e.g. a fully Cyrillic layer name —
    otherwise the remote name would collapse to just '.kml').
    """
    text = '' if text is None else str(text)
    result = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    result = _ALLOWED_RE.sub('_', result)
    result = _EDGE_RE.sub('', result)
    result = re.sub(r'_{2,}', '_', result)
    if not result or not result.strip('._-'):
        result = fallback
    return result[:_MAX_STEM]


def remote_filename(display_name, ext, taken):
    """Build a unique remote filename '<slug>.<ext>' and register it in `taken`.

    `taken` is a set of already-assigned names (lowercase comparison — one
    upload session keys files by name, and the server lowercases extensions).
    Duplicates get '_2', '_3', ... suffixes.
    """
    ext = (ext or '').lstrip('.').lower()
    stem = ascii_slug(display_name)
    candidate = '%s.%s' % (stem, ext) if ext else stem
    n = 1
    while candidate.lower() in taken:
        n += 1
        candidate = '%s_%d.%s' % (stem, n, ext) if ext else '%s_%d' % (stem, n)
    taken.add(candidate.lower())
    return candidate
