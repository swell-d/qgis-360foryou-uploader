# -*- coding: utf-8 -*-
"""Upload dialog (modal), completion handling and project status polling.

The dialog only configures an upload: on accept it prepares thread-safe export
jobs (main thread), hands everything to an UploadTask in the QGIS task manager
and closes. Results arrive through the message bar; a lightweight poller then
watches the server-side processing status."""
import json
import os
import tempfile
import time
from datetime import datetime

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsFeedback,
    QgsNetworkAccessManager,
    QgsProject,
    QgsTask,
)
from qgis.gui import QgsCollapsibleGroupBox
from qgis.PyQt.QtCore import QCoreApplication, QObject, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QBrush, QColor, QDesktopServices
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from . import exporters, plugin_settings
from .api_client import ApiClient, normalize_server_url, user_message_for
from .transport_qgis import QgisBlockingTransport, feedback_sleep
from .upload_task import UploadTask

BAR_TITLE = '360ForYou'
_LAYER_ID_ROLE = Qt.ItemDataRole.UserRole
_POLL_INTERVAL_MS = 5000
_POLL_TIMEOUT_S = 15 * 60


class UploadDialog(QDialog):
    """registry must provide track_task(task) and register_poller(poller)
    (the plugin object) — it keeps Python references alive."""

    def __init__(self, iface, registry, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.registry = registry
        self._assessments = {}  # layer_id -> LayerAssessment
        self.setWindowTitle(self.tr('Upload to 360ForYou'))
        self.resize(660, 700)
        self._build_ui()
        self._populate_layers()

    # ----------------------------------------------------------------- UI

    def _build_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(self.tr('Layers to upload:')))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([self.tr('Layer'), self.tr('Will upload as')])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree, 3)

        layout.addWidget(QLabel(self.tr('Additional files from disk:')))
        self.files_list = QListWidget()
        self.files_list.setMaximumHeight(90)
        layout.addWidget(self.files_list, 1)
        files_buttons = QHBoxLayout()
        add_files = QPushButton(self.tr('Add files...'))
        add_files.clicked.connect(self._add_files)
        remove_files = QPushButton(self.tr('Remove'))
        remove_files.clicked.connect(self._remove_files)
        files_buttons.addWidget(add_files)
        files_buttons.addWidget(remove_files)
        files_buttons.addStretch()
        layout.addLayout(files_buttons)

        project_box = QgsCollapsibleGroupBox(self.tr('Project'))
        project_layout = QVBoxLayout(project_box)
        title_row = QHBoxLayout()
        title_row.addWidget(QLabel(self.tr('Title:')))
        self.title_edit = QLineEdit(self._default_title())
        title_row.addWidget(self.title_edit)
        project_layout.addLayout(title_row)
        privacy_row = QHBoxLayout()
        privacy_row.addWidget(QLabel(self.tr('Privacy:')))
        self.privacy_combo = QComboBox()
        self.privacy_combo.addItem(self.tr('Private (only you and invited users)'), 2)
        self.privacy_combo.addItem(self.tr('Via link (anyone with the link)'), 1)
        self.privacy_combo.addItem(self.tr('Public'), 0)
        index = self.privacy_combo.findData(plugin_settings.privacy_mode())
        self.privacy_combo.setCurrentIndex(index if index >= 0 else 0)
        privacy_row.addWidget(self.privacy_combo, 1)
        project_layout.addLayout(privacy_row)
        layout.addWidget(project_box)

        advanced_box = QgsCollapsibleGroupBox(self.tr('Advanced'))
        advanced_layout = QVBoxLayout(advanced_box)
        crs_row = QHBoxLayout()
        crs_row.addWidget(QLabel(self.tr('Coordinate system:')))
        self.crs_edit = QLineEdit()
        self.crs_edit.setPlaceholderText(self.tr('EPSG code, PROJ or WKT — optional'))
        crs_row.addWidget(self.crs_edit, 1)
        advanced_layout.addLayout(crs_row)
        advanced_layout.addWidget(QLabel(self.tr(
            'Used by the server to position point clouds that carry no CRS themselves.')))
        advanced_box.setCollapsed(True)
        layout.addWidget(advanced_box)

        server_box = QgsCollapsibleGroupBox(self.tr('Server'))
        server_layout = QVBoxLayout(server_box)
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel(self.tr('Server URL:')))
        self.server_edit = QLineEdit(plugin_settings.server_url())
        url_row.addWidget(self.server_edit, 1)
        server_layout.addLayout(url_row)
        key_row = QHBoxLayout()
        key_row.addWidget(QLabel(self.tr('API key:')))
        self.key_edit = QLineEdit(plugin_settings.api_key())
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_row.addWidget(self.key_edit, 1)
        self.show_key = QCheckBox(self.tr('Show'))
        self.show_key.toggled.connect(self._toggle_key_visibility)
        key_row.addWidget(self.show_key)
        server_layout.addLayout(key_row)
        actions_row = QHBoxLayout()
        get_key = QPushButton(self.tr('Get an API key...'))
        get_key.setToolTip(self.tr('Opens your profile — create a key with scopes '
                                   'projects:read and projects:write'))
        get_key.clicked.connect(self._open_key_page)
        actions_row.addWidget(get_key)
        self.test_button = QPushButton(self.tr('Test connection'))
        self.test_button.clicked.connect(self._test_connection)
        actions_row.addWidget(self.test_button)
        self.test_label = QLabel('')
        actions_row.addWidget(self.test_label, 1)
        server_layout.addLayout(actions_row)
        server_box.setCollapsed(bool(plugin_settings.api_key()))
        layout.addWidget(server_box)

        buttons = QDialogButtonBox()
        self.upload_button = buttons.addButton(self.tr('Upload'),
                                               QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _default_title(self):
        base = QgsProject.instance().baseName()
        if base:
            return base
        return self.tr('QGIS upload') + datetime.now().strftime(' %Y-%m-%d %H:%M')

    def _populate_layers(self):
        groups = {
            'raster': QTreeWidgetItem([self.tr('Raster layers')]),
            'vector': QTreeWidgetItem([self.tr('Vector layers')]),
            'pointcloud': QTreeWidgetItem([self.tr('Point clouds')]),
        }
        root = QgsProject.instance().layerTreeRoot()
        first_pc_crs = ''
        for layer in QgsProject.instance().mapLayers().values():
            assessment = exporters.assess_layer(layer)
            if assessment is None:
                continue
            self._assessments[layer.id()] = assessment
            layer_type = layer.type()
            if layer_type == Qgis.LayerType.Raster:
                group = groups['raster']
            elif layer_type == Qgis.LayerType.Vector:
                group = groups['vector']
            else:
                group = groups['pointcloud']
            item = QTreeWidgetItem([layer.name(), assessment.note or assessment.reason])
            item.setData(0, _LAYER_ID_ROLE, layer.id())
            if assessment.uploadable:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                node = root.findLayer(layer.id())
                visible = node.isVisible() if node is not None else True
                item.setCheckState(0, Qt.CheckState.Checked if visible
                                   else Qt.CheckState.Unchecked)
                if (layer_type == Qgis.LayerType.PointCloud and not first_pc_crs
                        and layer.crs().isValid() and layer.crs().authid().startswith('EPSG:')):
                    first_pc_crs = layer.crs().authid()
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setForeground(1, QBrush(QColor(150, 150, 150)))
                item.setToolTip(1, assessment.reason)
            group.addChild(item)
        for group in groups.values():
            if group.childCount():
                self.tree.addTopLevelItem(group)
                group.setExpanded(True)
        if first_pc_crs:
            self.crs_edit.setText(first_pc_crs)

    # ------------------------------------------------------------- actions

    def _toggle_key_visibility(self, show):
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Normal if show
                                  else QLineEdit.EchoMode.Password)

    def _open_key_page(self):
        server = normalize_server_url(self.server_edit.text()) or plugin_settings.DEFAULT_SERVER_URL
        QDesktopServices.openUrl(QUrl(server + '/profile#api-keys'))

    def _add_files(self):
        exts = plugin_settings.allowed_extensions()
        mask = ' '.join('*.%s' % e for e in exts)
        paths, _ = QFileDialog.getOpenFileNames(
            self, self.tr('Add files'), '',
            self.tr('Supported files') + ' (%s);;' % mask + self.tr('All files') + ' (*)')
        allowed = set(exts)
        existing = {self.files_list.item(i).text() for i in range(self.files_list.count())}
        rejected = []
        for path in paths:
            ext = os.path.splitext(path)[1].lower().lstrip('.')
            if ext not in allowed:
                rejected.append(os.path.basename(path))
                continue
            if path not in existing:
                self.files_list.addItem(path)
                existing.add(path)
        if rejected:
            QMessageBox.warning(self, BAR_TITLE, self.tr(
                'These files were skipped — the server does not accept their type:')
                + '\n' + '\n'.join(rejected))

    def _remove_files(self):
        for item in self.files_list.selectedItems():
            self.files_list.takeItem(self.files_list.row(item))

    def _test_connection(self):
        server = normalize_server_url(self.server_edit.text())
        key = self.key_edit.text().strip()
        if not server or not key:
            self.test_label.setText(self.tr('Enter the server URL and API key first.'))
            return
        client = ApiClient(server, key, transport=QgisBlockingTransport(), max_retries=1)
        self.test_button.setEnabled(False)
        self.test_label.setText(self.tr('Testing...'))

        def _work(_task):
            client.list_projects()
            return True

        def _finished(exception, _result=None):
            try:
                self.test_button.setEnabled(True)
                if exception is None:
                    self.test_label.setText(self.tr('Connected — the key works.'))
                    plugin_settings.set_server_url(server)
                    plugin_settings.set_api_key(key)
                else:
                    self.test_label.setText(user_message_for(exception, server))
            except RuntimeError:
                pass  # dialog already closed/deleted

        task = QgsTask.fromFunction('360ForYou: test connection', _work,
                                    on_finished=_finished)
        self.registry.track_task(task)
        QgsApplication.taskManager().addTask(task)

    # -------------------------------------------------------------- accept

    def _checked_layers(self):
        checked = []
        layers_by_id = {}
        project = QgsProject.instance()
        for g in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(g)
            for c in range(group.childCount()):
                item = group.child(c)
                if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                    continue
                if item.checkState(0) != Qt.CheckState.Checked:
                    continue
                layer_id = item.data(0, _LAYER_ID_ROLE)
                layer = project.mapLayer(layer_id)
                assessment = self._assessments.get(layer_id)
                if layer is None or assessment is None:
                    continue
                checked.append((layer, assessment))
                layers_by_id[layer_id] = layer
        return checked, layers_by_id

    def accept(self):
        server = normalize_server_url(self.server_edit.text())
        key = self.key_edit.text().strip()
        if not server or not key:
            QMessageBox.warning(self, BAR_TITLE, self.tr(
                'Enter the server URL and an API key (see "Get an API key...").'))
            return
        checked, layers_by_id = self._checked_layers()
        disk_files = [self.files_list.item(i).text() for i in range(self.files_list.count())]
        items, problems = exporters.build_upload_items(
            checked, disk_files, plugin_settings.allowed_extensions())
        if problems:
            QMessageBox.warning(self, BAR_TITLE, '\n'.join(problems))
        if not items:
            QMessageBox.warning(self, BAR_TITLE,
                                self.tr('Nothing selected — check at least one layer or add a file.'))
            return
        try:
            jobs = exporters.prepare_export_jobs(items, layers_by_id)
        except exporters.ExportError as e:
            QMessageBox.critical(self, BAR_TITLE, str(e))
            return

        title = self.title_edit.text().strip() or self._default_title()
        privacy = self.privacy_combo.currentData()
        coordinate_system = self.crs_edit.text().strip()
        project_options = {'title': title, 'privacy_mode': privacy}
        if coordinate_system:
            project_options['coordinate_system'] = coordinate_system

        plugin_settings.set_server_url(server)
        plugin_settings.set_api_key(key)
        plugin_settings.set_privacy_mode(privacy)

        feedback = QgsFeedback()
        client = ApiClient(server, key,
                           transport=QgisBlockingTransport(feedback),
                           chunk_bytes=plugin_settings.chunk_bytes(),
                           sleep=feedback_sleep(feedback))
        temp_dir = tempfile.mkdtemp(prefix='uploader_360foryou_')
        task = UploadTask(self.tr('Upload to 360ForYou: %s') % title,
                          jobs, project_options, client, feedback, temp_dir,
                          QgsProject.instance().transformContext(),
                          _make_on_done(self.iface, server, key, self.registry))
        self.registry.track_task(task)
        QgsApplication.taskManager().addTask(task)
        self.iface.messageBar().pushMessage(
            BAR_TITLE,
            self.tr('Upload started — progress is shown in the task manager (bottom bar).'),
            Qgis.MessageLevel.Info, 6)
        super().accept()


# ------------------------------------------------------------------ results


def _translate(text):
    return QCoreApplication.translate('UploadDialog', text)


def _push_open_button(iface, level, text, url, duration=0):
    bar = iface.messageBar()
    widget = bar.createMessage(BAR_TITLE, text)
    button = QPushButton(_translate('Open project'))
    button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
    widget.layout().addWidget(button)
    bar.pushWidget(widget, level, duration)


def _make_on_done(iface, server_url, api_key, registry):
    """Completion callback for UploadTask; deliberately holds no dialog refs."""

    def on_done(result, error, was_canceled, allowed_refresh):
        if allowed_refresh:
            plugin_settings.set_allowed_extensions(allowed_refresh)
        bar = iface.messageBar()
        if result is not None:
            _push_open_button(iface, Qgis.MessageLevel.Success,
                              _translate('Project "%s" created — processing started.')
                              % result['title'],
                              result['url'])
            poller = ProjectStatusPoller(iface, server_url, api_key,
                                         result['name'], result['title'],
                                         parent=iface.mainWindow())
            registry.register_poller(poller)
        elif was_canceled:
            bar.pushMessage(BAR_TITLE, _translate('Upload canceled.'),
                            Qgis.MessageLevel.Warning, 6)
        else:
            message = error or _translate('Upload failed.')
            widget = bar.createMessage(BAR_TITLE, message[:160])
            if len(message) > 160:
                details = QPushButton(_translate('Details'))
                details.clicked.connect(
                    lambda: QMessageBox.critical(iface.mainWindow(), BAR_TITLE, message))
                widget.layout().addWidget(details)
            bar.pushWidget(widget, Qgis.MessageLevel.Critical, 0)

    return on_done


class ProjectStatusPoller(QObject):
    """Polls GET /api/v1/projects/<name> every few seconds until the project is
    ready (status == 100) or a deadline passes. Uses the async network manager —
    never blocks the UI thread."""

    def __init__(self, iface, server_url, api_key, name, title, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.title = title
        self.page_url = '%s/projects/%s' % (normalize_server_url(server_url), name)
        self.api_url = QUrl('%s/api/v1/projects/%s' % (normalize_server_url(server_url), name))
        self.auth = b'Bearer ' + api_key.encode('utf-8')
        self.deadline = time.monotonic() + _POLL_TIMEOUT_S
        self._reply = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(_POLL_INTERVAL_MS)

    def stop(self):
        self.timer.stop()
        if self._reply is not None:
            self._reply.abort()
            self._reply = None

    def _tick(self):
        if time.monotonic() > self.deadline:
            self.stop()
            _push_open_button(self.iface, Qgis.MessageLevel.Info,
                              _translate('Project "%s" is still processing — check it later.')
                              % self.title,
                              self.page_url, duration=0)
            return
        if self._reply is not None:  # previous request still in flight
            return
        request = QNetworkRequest(self.api_url)
        request.setRawHeader(b'Authorization', self.auth)
        self._reply = QgsNetworkAccessManager.instance().get(request)
        self._reply.finished.connect(self._on_reply)

    def _on_reply(self):
        reply, self._reply = self._reply, None
        if reply is None:
            return
        try:
            payload = bytes(reply.readAll())
        finally:
            reply.deleteLater()
        try:
            status = json.loads(payload.decode('utf-8')).get('status')
        except (ValueError, UnicodeDecodeError, AttributeError):
            return  # transient error — keep polling until the deadline
        if status == 100:
            self.stop()
            _push_open_button(self.iface, Qgis.MessageLevel.Success,
                              _translate('Project "%s" is ready.') % self.title,
                              self.page_url)
