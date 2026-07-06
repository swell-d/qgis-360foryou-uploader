# -*- coding: utf-8 -*-
"""Background task: export prepared jobs, upload everything into one session,
create the project. Runs in the QGIS task manager (progress bar, cancelable)."""
import os
import shutil
import traceback

from qgis.core import Qgis, QgsFeedback, QgsMessageLog, QgsRasterBlockFeedback, QgsTask

from . import exporters
from .api_client import ApiError, ExtensionNotAllowed, UploadCanceled, user_message_for

LOG_TAG = '360ForYou Uploader'

_PROGRESS_EXPORT_END = 25.0
_PROGRESS_UPLOAD_END = 95.0


class _TaskFailure(Exception):
    """Failure with a ready-to-display message."""


class UploadTask(QgsTask):
    """on_done(result: dict|None, error: str|None, was_canceled: bool,
    allowed_extensions: list|None) is called in finished() on the main thread."""

    def __init__(self, description, jobs, project_options, client, http_feedback,
                 temp_dir, transform_context, on_done):
        super().__init__(description)
        self.jobs = jobs
        self.project_options = dict(project_options)
        self.client = client
        self.temp_dir = temp_dir
        self.transform_context = transform_context
        self.on_done = on_done
        self.result_info = None
        self.error_message = None
        self.was_canceled = False
        self.allowed_refresh = None
        self.upload_id = None
        self._feedbacks = [http_feedback]

    # ------------------------------------------------------------ worker side

    def run(self):
        try:
            self._export_all()
            self._upload_all()
            self._create_project()
            self.setProgress(100)
            return True
        except UploadCanceled:
            self.was_canceled = True
            self.error_message = 'Upload canceled.'
        except ExtensionNotAllowed as e:
            self.allowed_refresh = e.allowed  # refresh the cached whitelist
            self.error_message = user_message_for(e, self.client.server_url)
        except ApiError as e:
            self.error_message = user_message_for(e, self.client.server_url)
        except (exporters.ExportError, _TaskFailure) as e:
            self.error_message = str(e)
        except Exception:
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG,
                                     Qgis.MessageLevel.Critical)
            self.error_message = 'Unexpected error — see the QGIS log (%s).' % LOG_TAG
        self._cleanup_session()
        return False

    def _export_all(self):
        export_jobs = [j for j in self.jobs
                       if j.item.kind in (exporters.KIND_VECTOR_KML,
                                          exporters.KIND_RASTER_RENDER)]
        done = 0
        for job in self.jobs:
            if self.isCanceled():
                raise UploadCanceled()
            path = exporters.run_export_job(job, self.temp_dir, self.transform_context,
                                            self._feedback_factory)
            if self.isCanceled():  # writers return partial output when canceled
                raise UploadCanceled()
            job.item.source_path = path
            if job in export_jobs:
                done += 1
                self.setProgress(_PROGRESS_EXPORT_END * done / len(export_jobs))
        self.setProgress(_PROGRESS_EXPORT_END)

    def _upload_all(self):
        items = [job.item for job in self.jobs]
        sizes = {item.remote_name: os.path.getsize(item.source_path) for item in items}
        total = sum(sizes.values()) or 1
        self.upload_id = self.client.create_session()['upload_id']
        span = _PROGRESS_UPLOAD_END - _PROGRESS_EXPORT_END
        done_bytes = 0
        for item in items:
            if self.isCanceled():
                raise UploadCanceled()
            base = done_bytes

            def _progress(received, _size, _base=base):
                self.setProgress(_PROGRESS_EXPORT_END + span * (_base + received) / total)

            self.client.upload_file(self.upload_id, item.source_path,
                                    remote_name=item.remote_name,
                                    progress_cb=_progress, cancel_cb=self.isCanceled)
            done_bytes += sizes[item.remote_name]
        listing = self.client.list_session(self.upload_id)
        complete = {f['name'] for f in listing if f.get('complete')}
        missing = [i.remote_name for i in items if i.remote_name not in complete]
        if missing:
            raise _TaskFailure('Some files did not finish uploading: %s. Try again.'
                               % ', '.join(missing))
        self.setProgress(_PROGRESS_UPLOAD_END)

    def _create_project(self):
        result = self.client.create_project(self.upload_id, **self.project_options)
        name = result.get('name', '')
        self.result_info = {
            'name': name,
            'url': self.client.project_page_url(name),
            'title': self.project_options.get('title') or name,
        }

    def _feedback_factory(self, is_raster):
        feedback = QgsRasterBlockFeedback() if is_raster else QgsFeedback()
        self._feedbacks.append(feedback)
        if self.isCanceled():
            feedback.cancel()
        return feedback

    def _cleanup_session(self):
        if self.upload_id and self.result_info is None:
            # QgsFeedback has no un-cancel: swap in a fresh transport so the
            # cleanup request is not aborted by the canceled feedback.
            try:
                from .transport_qgis import QgisBlockingTransport
                self.client.transport = QgisBlockingTransport()
                self.client._sleep = lambda seconds: None
                self.client.max_retries = 0
                self.client.delete_session(self.upload_id)
            except Exception:
                pass

    # -------------------------------------------------------------- main thread

    def cancel(self):
        for feedback in list(self._feedbacks):
            feedback.cancel()
        super().cancel()

    def finished(self, ok):
        exporters.dispose_jobs(self.jobs)
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        if self.on_done is not None:
            self.on_done(self.result_info if ok else None,
                         None if ok else self.error_message,
                         self.was_canceled,
                         self.allowed_refresh)
