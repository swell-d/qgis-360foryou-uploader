# -*- coding: utf-8 -*-
import json
import sys

import pytest

from uploader_360foryou import api_client as ac


def jr(status, obj=None, headers=None):
    body = b'' if obj is None else json.dumps(obj).encode()
    return ac.Response(status, headers or {}, body)


class FakeTransport:
    """Returns scripted responses in order; exceptions in the script are raised."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def send(self, method, url, headers, body):
        self.calls.append({'method': method, 'url': url, 'headers': dict(headers), 'body': body})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(script, **kw):
    transport = FakeTransport(script)
    sleeps = []
    c = ac.ApiClient('https://example.com', 'sk_test', transport=transport,
                     sleep=sleeps.append, **kw)
    return c, transport, sleeps


def test_modules_do_not_import_qgis():
    assert not any(m == 'qgis' or m.startswith('qgis.') for m in sys.modules)


def test_normalize_server_url():
    assert ac.normalize_server_url('example.com/') == 'https://example.com'
    assert ac.normalize_server_url(' http://x.local ') == 'http://x.local'
    assert ac.normalize_server_url('') == ''


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://x/y', 'data:text/plain,x', '//no-scheme'])
def test_non_http_schemes_rejected(url):
    with pytest.raises(ac.TransportFailure):
        ac.ensure_http_scheme(url)
    with pytest.raises(ac.TransportFailure):
        ac.UrllibTransport().send('GET', url, {}, None)


def test_http_schemes_accepted():
    ac.ensure_http_scheme('https://example.com/api')
    ac.ensure_http_scheme('http://127.0.0.1:5000/api')


def test_create_session():
    c, t, _ = client([jr(201, {'upload_id': 'Ab12Cd34', 'expires_in_seconds': 259200})])
    assert c.create_session()['upload_id'] == 'Ab12Cd34'
    call = t.calls[0]
    assert call['method'] == 'POST'
    assert call['url'] == 'https://example.com/api/v1/uploads'
    assert call['headers']['Authorization'] == 'Bearer sk_test'


def test_create_session_429_not_retried():
    c, _, sleeps = client([jr(429, {'error': 'rate_limited'}, {'Retry-After': '10'})])
    with pytest.raises(ac.RateLimited) as e:
        c.create_session()
    assert e.value.retry_after == 10
    assert sleeps == []


def test_upload_chunk_loop(tmp_path):
    path = tmp_path / 'data.bin'
    path.write_bytes(b'0123456789')
    c, t, _ = client([
        jr(404),  # file_status: nothing on the server yet
        jr(200, {'received': 4, 'total': 10, 'complete': False}),
        jr(200, {'received': 8, 'total': 10, 'complete': False}),
        jr(200, {'received': 10, 'total': 10, 'complete': True}),
    ], chunk_bytes=4)
    progress = []
    result = c.upload_file('Ab12Cd34', str(path), progress_cb=lambda r, t_: progress.append(r))
    assert result['complete'] is True
    puts = [x for x in t.calls if x['method'] == 'PUT']
    assert [p['headers']['Content-Range'] for p in puts] == [
        'bytes 0-3/10', 'bytes 4-7/10', 'bytes 8-9/10']
    assert [p['body'] for p in puts] == [b'0123', b'4567', b'89']
    assert all(p['headers']['Content-Type'] == 'application/octet-stream' for p in puts)
    assert progress == [4, 8, 10]


def test_upload_resumes_from_server_offset(tmp_path):
    path = tmp_path / 'data.bin'
    path.write_bytes(b'0123456789')
    c, t, _ = client([
        jr(200, {'received': 6, 'total': 10, 'complete': False}),  # file_status
        jr(200, {'received': 10, 'total': 10, 'complete': True}),
    ], chunk_bytes=8)
    c.upload_file('Ab12Cd34', str(path))
    put = [x for x in t.calls if x['method'] == 'PUT'][0]
    assert put['headers']['Content-Range'] == 'bytes 6-9/10'
    assert put['body'] == b'6789'


def test_upload_409_resync(tmp_path):
    path = tmp_path / 'data.bin'
    path.write_bytes(b'0123456789')
    c, t, _ = client([
        jr(404),
        jr(409, {'error': 'non_contiguous_chunk', 'received': 2}),
        jr(200, {'received': 10, 'total': 10, 'complete': True}),
    ], chunk_bytes=100)
    c.upload_file('Ab12Cd34', str(path))
    puts = [x for x in t.calls if x['method'] == 'PUT']
    assert puts[0]['headers']['Content-Range'] == 'bytes 0-9/10'
    assert puts[1]['headers']['Content-Range'] == 'bytes 2-9/10'
    assert puts[1]['body'] == b'23456789'


def test_upload_empty_file(tmp_path):
    path = tmp_path / 'empty.txt'
    path.write_bytes(b'')
    c, t, _ = client([jr(200, {'received': 0, 'total': 0, 'complete': True})])
    result = c.upload_file('Ab12Cd34', str(path))
    assert result['complete'] is True
    put = t.calls[0]
    assert put['method'] == 'PUT'
    assert 'Content-Range' not in put['headers']
    assert put['headers']['Content-Length'] == '0'


def test_upload_cancel(tmp_path):
    path = tmp_path / 'data.bin'
    path.write_bytes(b'0123456789')
    c, t, _ = client([jr(404)])
    with pytest.raises(ac.UploadCanceled):
        c.upload_file('Ab12Cd34', str(path), cancel_cb=lambda: True)
    assert [x['method'] for x in t.calls] == ['GET']  # canceled before any PUT


def test_remote_name_is_url_quoted(tmp_path):
    path = tmp_path / 'empty.txt'
    path.write_bytes(b'')
    c, t, _ = client([jr(200, {'received': 0, 'total': 0, 'complete': True})])
    c.upload_file('Ab12Cd34', str(path), remote_name='a b.txt')
    assert t.calls[0]['url'].endswith('/uploads/Ab12Cd34/a%20b.txt')


def test_network_error_retried_then_succeeds():
    c, t, sleeps = client([ac.TransportFailure('boom'), jr(200, {'files': []})])
    assert c.list_session('Ab12Cd34') == []
    assert len(t.calls) == 2
    assert sleeps == [1.0]


def test_5xx_retried_with_backoff_then_exhausted():
    c, _, sleeps = client([jr(500)] * 3, max_retries=2)
    with pytest.raises(ac.ServerError):
        c.list_session('Ab12Cd34')
    assert sleeps == [1.0, 2.0]


def test_network_error_exhausted():
    c, _, _ = client([ac.TransportFailure('boom')] * 3, max_retries=2)
    with pytest.raises(ac.NetworkError):
        c.list_session('Ab12Cd34')


def test_429_on_idempotent_request_waits_retry_after():
    c, t, sleeps = client([jr(429, {'error': 'rate_limited'}, {'Retry-After': '3'}),
                           jr(200, {'files': []})])
    assert c.list_session('Ab12Cd34') == []
    assert sleeps == [3]
    assert len(t.calls) == 2


@pytest.mark.parametrize('status,code,exc', [
    (401, 'invalid_api_key', ac.AuthError),
    (401, 'missing_bearer_token', ac.AuthError),
    (402, 'storage_quota_exceeded', ac.QuotaError),
    (403, 'insufficient_scope', ac.ForbiddenError),
    (403, 'project_limit_reached', ac.ForbiddenError),
    (413, 'file_too_large', ac.TooLargeError),
    (404, 'upload_session_not_found', ac.ApiError),
])
def test_error_mapping(status, code, exc):
    c, _, _ = client([jr(status, {'error': code})])
    with pytest.raises(exc) as e:
        c.list_session('Ab12Cd34')
    assert e.value.code == code
    assert e.value.http_status == status


def test_415_carries_allowed_list():
    c, _, _ = client([jr(415, {'error': 'extension_not_allowed', 'extension': 'exe',
                               'allowed': ['jpg', 'las']})])
    with pytest.raises(ac.ExtensionNotAllowed) as e:
        c.list_session('Ab12Cd34')
    assert e.value.allowed == ['jpg', 'las']
    assert e.value.extension == 'exe'


def test_create_project_payload():
    c, t, _ = client([jr(201, {'name': 'Xy9Zw8Vu', 'status': 0})])
    result = c.create_project('Ab12Cd34', 'My title', privacy_mode=1)
    assert result['name'] == 'Xy9Zw8Vu'
    call = t.calls[0]
    payload = json.loads(call['body'].decode())
    assert payload == {'upload_id': 'Ab12Cd34', 'title': 'My title', 'privacy_mode': 1}
    assert call['headers']['Content-Type'] == 'application/json'


def test_create_project_with_crs():
    c, t, _ = client([jr(201, {'name': 'Xy9Zw8Vu', 'status': 0})])
    c.create_project('Ab12Cd34', 't', coordinate_system='EPSG:25832')
    payload = json.loads(t.calls[0]['body'].decode())
    assert payload['coordinate_system'] == 'EPSG:25832'
    assert payload['privacy_mode'] == 2


def test_delete_session_swallows_errors():
    c, _, _ = client([jr(500)] * 10, max_retries=1)
    c.delete_session('Ab12Cd34')  # must not raise


def test_user_messages():
    msg = ac.user_message_for(ac.AuthError('invalid_api_key', 401), 'https://example.com')
    assert 'example.com/profile#api-keys' in msg
    msg = ac.user_message_for(ac.ExtensionNotAllowed(
        'extension_not_allowed', 415, payload={'extension': 'exe', 'allowed': ['jpg']}))
    assert 'exe' in msg and 'jpg' in msg
    assert 'later' in ac.user_message_for(ac.RateLimited('rate_limited', 429))
