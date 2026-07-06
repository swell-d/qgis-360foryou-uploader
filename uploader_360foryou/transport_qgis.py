# -*- coding: utf-8 -*-
"""HTTP transport backed by the QGIS network stack.

QgsBlockingNetworkRequest is thread-safe (usable from QgsTask worker threads),
honors the proxy and SSL settings configured in QGIS options — important for
corporate GIS users — and supports cancellation of an in-flight request
through a QgsFeedback object.
"""
import time

from qgis.core import QgsBlockingNetworkRequest
from qgis.PyQt.QtCore import QByteArray, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from .api_client import Response, TransportFailure, UploadCanceled


def feedback_sleep(feedback):
    """Retry-backoff sleep that aborts as soon as the feedback is canceled,
    so a user cancel is not stuck behind a 30 s backoff wait."""
    def _sleep(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if feedback is not None and feedback.isCanceled():
                raise UploadCanceled()
            time.sleep(0.2)
    return _sleep


class QgisBlockingTransport:

    def __init__(self, feedback=None):
        self.feedback = feedback  # QgsFeedback; cancel() aborts the in-flight request

    def send(self, method, url, headers, body):
        # A canceled request must surface as UploadCanceled, not as a retryable
        # TransportFailure — otherwise the retry loop keeps re-sending it.
        if self.feedback is not None and self.feedback.isCanceled():
            raise UploadCanceled()
        request = QNetworkRequest(QUrl(url))
        for name, value in (headers or {}).items():
            request.setRawHeader(name.encode('utf-8'), str(value).encode('utf-8'))
        blocking = QgsBlockingNetworkRequest()
        data = QByteArray(body if body is not None else b'')
        if method == 'GET':
            blocking.get(request, False, self.feedback)
        elif method == 'PUT':
            blocking.put(request, data, self.feedback)
        elif method == 'POST':
            blocking.post(request, data, False, self.feedback)
        elif method == 'DELETE':
            blocking.deleteResource(request, self.feedback)
        else:
            raise ValueError('unsupported HTTP method: %s' % method)

        reply = blocking.reply()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        # An HTTP error (4xx/5xx) still carries a usable response; only requests
        # that never produced an HTTP status are transport failures (timeout,
        # DNS, refused connection, user cancel via feedback).
        if not status:
            if self.feedback is not None and self.feedback.isCanceled():
                raise UploadCanceled()
            raise TransportFailure(blocking.errorMessage() or 'network request failed')
        response_headers = {}
        retry_after = bytes(reply.rawHeader(b'Retry-After'))
        if retry_after:
            response_headers['Retry-After'] = retry_after.decode('ascii', 'ignore')
        return Response(int(status), response_headers, bytes(reply.content()))
