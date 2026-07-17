# -*- coding: utf-8 -*-
"""Client for the 360ForYou public API v1.

Pure Python standard library — no qgis/PyQt imports, so this module is
unit-testable on any Python and reusable outside QGIS. HTTP goes through a
pluggable transport with a single method:

    transport.send(method, url, headers, body) -> Response

Production code inside QGIS injects transport_qgis.QgisBlockingTransport
(honors QGIS proxy/SSL settings); tests and CLI use UrllibTransport.

Protocol (see <server>/api-guide):
    POST /uploads                     -> {"upload_id", "expires_in_seconds"}
    PUT  /uploads/<id>/<filename>     raw bytes; whole file (no Content-Range)
                                      or resumable chunks (Content-Range:
                                      bytes <start>-<end>/<total>, end inclusive,
                                      contiguous, in order)
    GET  /uploads/<id>/<filename>     -> {"received", "total", "complete"}
    GET  /uploads/<id>                -> {"upload_id", "files": [...]}
    DELETE /uploads/<id>              idempotent
    POST /projects                    {"upload_id", "title", "privacy_mode", ...}
    GET  /projects and /projects/<name>
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_CHUNK = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 300
OCTET_STREAM = 'application/octet-stream'

# Base extension whitelist of a default deployment; the authoritative list for a
# concrete server arrives in the body of a 415 extension_not_allowed response.
BASE_ALLOWED_EXTENSIONS = [
    '7z', 'csv', 'dat', 'db', 'e57', 'exr', 'frag', 'glb', 'gpx', 'ifc',
    'jpeg', 'jpg', 'json', 'kml', 'kmz', 'ksplat', 'las', 'laz', 'lgs',
    'lgsx', 'mp4', 'mtl', 'obj', 'opt', 'out', 'pdf', 'ply', 'png', 'pts',
    'ptx', 'rad', 'rar', 'rmx', 'sog', 'splat', 'spz', 'stl', 'tif', 'tiff',
    'txt', 'xyz', 'zip',
]


class Response:
    """Minimal HTTP response value object produced by transports."""

    def __init__(self, status, headers=None, body=b''):
        self.status = status
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self.body = body or b''

    def json(self):
        try:
            return json.loads(self.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return None

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)


class TransportFailure(Exception):
    """Network-level failure (no HTTP response) — retryable."""


def ensure_secure_scheme(url):
    """Require https before the URL reaches a network stack; plain http is
    allowed only for loopback hosts (local development servers), where traffic
    never leaves the machine. Also rejects file:// and other non-HTTP schemes
    that urlopen and Qt would otherwise accept."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == 'https':
        return
    host = (parts.hostname or '').lower()
    if scheme == 'http' and (host == 'localhost' or host == '::1' or host.startswith('127.')):
        return
    if scheme == 'http':
        raise TransportFailure('https is required for %s (http is allowed only for localhost)' % host)
    raise TransportFailure('unsupported URL scheme: %s' % (scheme or '(none)'))


def _build_http_opener():
    # Explicit handler list instead of urlopen/build_opener: no FileHandler,
    # FTPHandler or DataHandler, so non-HTTP schemes cannot be opened at all.
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(),  # honors HTTP(S)_PROXY env vars
                    urllib.request.UnknownHandler(),
                    urllib.request.HTTPHandler(),
                    urllib.request.HTTPSHandler(),
                    urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPRedirectHandler(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


class UrllibTransport:
    """Stdlib transport. Note: ignores QGIS proxy settings (only HTTP(S)_PROXY
    env vars) and rejects certificates that QGIS itself might trust."""

    def __init__(self, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout
        self._opener = _build_http_opener()

    def send(self, method, url, headers, body):
        ensure_secure_scheme(url)
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
        try:
            with self._opener.open(request, timeout=self.timeout) as r:
                return Response(r.status, dict(r.headers), r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, dict(e.headers or {}), e.read())
        except (urllib.error.URLError, OSError) as e:
            raise TransportFailure(str(e))


class ApiError(Exception):
    """HTTP-level API error with the server's machine-readable code."""

    def __init__(self, code, http_status, message='', payload=None):
        super().__init__(message or code)
        self.code = code
        self.http_status = http_status
        self.payload = payload or {}


class AuthError(ApiError):
    pass  # 401 missing_bearer_token / invalid_api_key


class QuotaError(ApiError):
    pass  # 402 storage_quota_exceeded


class ForbiddenError(ApiError):
    pass  # 403 tariff_* / insufficient_scope / project_limit_reached / email_not_verified


class TooLargeError(ApiError):
    pass  # 413 file_too_large / session_too_large


class ExtensionNotAllowed(ApiError):
    def __init__(self, code, http_status, message='', payload=None):
        super().__init__(code, http_status, message, payload)
        self.allowed = list(self.payload.get('allowed') or [])
        self.extension = self.payload.get('extension') or ''


class RateLimited(ApiError):
    def __init__(self, code, http_status, message='', payload=None, retry_after=None):
        super().__init__(code, http_status, message, payload)
        self.retry_after = retry_after


class ServerError(ApiError):
    pass  # 5xx after retries exhausted


class NetworkError(ApiError):
    pass  # transport failure after retries exhausted


class UploadCanceled(Exception):
    """Raised when cancel_cb() reports cancellation mid-upload."""


def normalize_server_url(url):
    url = (url or '').strip().rstrip('/')
    if url and '://' not in url:
        url = 'https://' + url
    return url


class ApiClient:

    def __init__(self, server_url, api_key, transport=None, chunk_bytes=DEFAULT_CHUNK,
                 max_retries=6, backoff_cap=30.0, sleep=time.sleep):
        self.server_url = normalize_server_url(server_url)
        self.base = self.server_url + '/api/v1'
        self.api_key = (api_key or '').strip()
        self.transport = transport or UrllibTransport()
        self.chunk_bytes = int(chunk_bytes)
        self.max_retries = int(max_retries)
        self.backoff_cap = float(backoff_cap)
        self._sleep = sleep

    # ---------------------------------------------------------------- helpers

    def project_page_url(self, name):
        return '%s/projects/%s' % (self.server_url, name)

    def api_keys_page_url(self):
        return self.server_url + '/profile#api-keys'

    def _request(self, method, path, headers=None, body=None, ok=(200,),
                 retry_status=(), retry_429=True):
        """One HTTP request with exponential backoff on network errors and 5xx
        (uploads are idempotent by offset, so retrying is safe — same policy as
        the reference client). Statuses in `ok` or `retry_status` are returned
        to the caller; anything else raises a typed ApiError.

        retry_429=False for the 10/hour endpoints (POST /uploads, POST /projects)
        where waiting out the window inside a request makes no sense.
        """
        url = self.base + path
        send_headers = {'Authorization': 'Bearer ' + self.api_key}
        send_headers.update(headers or {})
        delay = 1.0
        last_failure = None
        attempt = 0
        while attempt <= self.max_retries:
            attempt += 1
            try:
                resp = self.transport.send(method, url, send_headers, body)
            except TransportFailure as e:
                last_failure = str(e)
                if attempt > self.max_retries:
                    break
                self._sleep(delay)
                delay = min(delay * 2, self.backoff_cap)
                continue
            if resp.status in ok or resp.status in retry_status:
                return resp
            if resp.status >= 500:
                last_failure = 'server returned %d' % resp.status
                if attempt > self.max_retries:
                    break
                self._sleep(delay)
                delay = min(delay * 2, self.backoff_cap)
                continue
            if resp.status == 429 and retry_429:
                retry_after = _int_or_none(resp.header('Retry-After'))
                if retry_after is not None and retry_after <= 120 and attempt <= self.max_retries:
                    self._sleep(retry_after)
                    continue
            raise self._map_error(resp)
        if 'server returned' in (last_failure or ''):
            raise ServerError('server_error', 500, 'Request failed: %s' % last_failure)
        raise NetworkError('network_error', 0, last_failure or 'network failure')

    def _map_error(self, resp):
        payload = resp.json() or {}
        code = payload.get('error') or ('http_%d' % resp.status)
        status = resp.status
        if status == 401:
            return AuthError(code, status, payload=payload)
        if status == 402:
            return QuotaError(code, status, payload=payload)
        if status == 403:
            return ForbiddenError(code, status, payload=payload)
        if status == 413:
            return TooLargeError(code, status, payload=payload)
        if status == 415:
            return ExtensionNotAllowed(code, status, payload=payload)
        if status == 429:
            return RateLimited(code, status, payload=payload,
                               retry_after=_int_or_none(resp.header('Retry-After')))
        return ApiError(code, status, payload=payload)

    # ---------------------------------------------------------------- uploads

    def create_session(self):
        """POST /uploads -> {'upload_id': ..., 'expires_in_seconds': ...}"""
        resp = self._request('POST', '/uploads', ok=(201,), retry_429=False)
        return resp.json()

    def file_status(self, upload_id, remote_name):
        """Bytes the server already holds for this file (0 if none) — drives resume."""
        path = '/uploads/%s/%s' % (upload_id, urllib.parse.quote(remote_name))
        resp = self._request('GET', path, ok=(200,), retry_status=(404,))
        if resp.status != 200:
            return 0
        return (resp.json() or {}).get('received', 0)

    def upload_file(self, upload_id, path, remote_name=None, progress_cb=None, cancel_cb=None):
        """Upload one file in resumable chunks; returns the final status dict.

        Resumes from whatever the server already has; a 409 non_contiguous_chunk
        resyncs the offset to the server's view and continues.
        """
        remote_name = remote_name or os.path.basename(path)
        url_path = '/uploads/%s/%s' % (upload_id, urllib.parse.quote(remote_name))
        size = os.path.getsize(path)
        if size == 0:
            resp = self._request('PUT', url_path,
                                 headers={'Content-Type': OCTET_STREAM, 'Content-Length': '0'},
                                 body=b'')
            return resp.json()
        received = self.file_status(upload_id, remote_name)
        body = {'received': received, 'total': size, 'complete': received >= size}
        with open(path, 'rb') as f:
            while received < size:
                if cancel_cb is not None and cancel_cb():
                    raise UploadCanceled()
                f.seek(received)
                chunk = f.read(self.chunk_bytes)
                end = received + len(chunk) - 1
                resp = self._request(
                    'PUT', url_path,
                    headers={'Content-Type': OCTET_STREAM,
                             'Content-Range': 'bytes %d-%d/%d' % (received, end, size)},
                    body=chunk, retry_status=(409,))
                if resp.status == 409:  # offset desync -> adopt the server's view
                    received = (resp.json() or {}).get('received', 0)
                    continue
                body = resp.json()
                received = body['received']
                if progress_cb is not None:
                    progress_cb(received, size)
                if body.get('complete'):
                    break
        return body

    def list_session(self, upload_id):
        """GET /uploads/<id> -> list of {'name','received','total','complete'}"""
        resp = self._request('GET', '/uploads/%s' % upload_id)
        return (resp.json() or {}).get('files', [])

    def delete_session(self, upload_id):
        """Best-effort cleanup — never raises."""
        try:
            self._request('DELETE', '/uploads/%s' % upload_id)
        except (ApiError, TransportFailure, OSError):
            pass

    # --------------------------------------------------------------- projects

    def create_project(self, upload_id, title, privacy_mode=2, coordinate_system=None):
        """POST /projects -> {'name': '<slug>', 'status': 0, ...}
        The returned name is authoritative (may differ from upload_id)."""
        payload = {'upload_id': upload_id, 'title': title, 'privacy_mode': int(privacy_mode)}
        if coordinate_system:
            payload['coordinate_system'] = coordinate_system
        resp = self._request('POST', '/projects', ok=(201,), retry_429=False,
                             headers={'Content-Type': 'application/json'},
                             body=json.dumps(payload).encode('utf-8'))
        return resp.json()

    def get_project(self, name):
        """GET /projects/<name> -> {'name','status',...}; status 100 == ready."""
        resp = self._request('GET', '/projects/%s' % urllib.parse.quote(str(name)))
        return resp.json()

    def list_projects(self):
        """GET /projects — cheapest authenticated call; used for 'Test connection'."""
        resp = self._request('GET', '/projects')
        return resp.json()


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def user_message_for(exc, server_url=''):
    """Human-readable English message for a client exception."""
    server = normalize_server_url(server_url) or 'the server'
    if isinstance(exc, AuthError):
        return ('Invalid or missing API key. Create one at %s/profile#api-keys '
                'with scopes projects:read and projects:write.' % server)
    if isinstance(exc, QuotaError):
        return 'Storage quota exceeded on your account. Free up space or upgrade your plan.'
    if isinstance(exc, ForbiddenError):
        messages = {
            'tariff_does_not_allow_api': 'Your plan does not include API access.',
            'insufficient_scope': ('The API key lacks required scopes. Create a new key at '
                                   '%s/profile#api-keys with scopes projects:read and projects:write.' % server),
            'tariff_forbids_upload': 'Your plan does not allow uploads.',
            'project_limit_reached': 'Project limit reached on your account. Delete an old project or upgrade your plan.',
            'email_not_verified': 'Verify your account email address before creating projects.',
        }
        return messages.get(exc.code, 'Access denied by the server (%s).' % exc.code)
    if isinstance(exc, TooLargeError):
        return 'A file (or the whole upload) exceeds the server size limit.'
    if isinstance(exc, ExtensionNotAllowed):
        allowed = ', '.join(exc.allowed) if exc.allowed else 'unknown'
        return ('File type "%s" is not accepted by this server. Allowed types: %s.'
                % (exc.extension or '?', allowed))
    if isinstance(exc, RateLimited):
        return ('Server rate limit reached (upload sessions and project creation are '
                'limited per hour). Try again later.')
    if isinstance(exc, NetworkError):
        return ('Could not reach %s: %s. Check the server URL, your connection and '
                'proxy settings.' % (server, exc))
    if isinstance(exc, ServerError):
        return 'The server reported an internal error. Try again later.'
    if isinstance(exc, ApiError):
        return 'Request failed: %s (HTTP %d).' % (exc.code, exc.http_status)
    return str(exc)
