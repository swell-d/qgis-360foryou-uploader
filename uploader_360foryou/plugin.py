# -*- coding: utf-8 -*-
"""Plugin entry object: toolbar button, menu entry, and the registry that
keeps background tasks / pollers alive between dialogs."""
import os

from qgis.PyQt.QtCore import QCoreApplication

try:  # QAction lives in QtGui on Qt6; the qgis.PyQt shim provides it there on Qt5 too
    from qgis.PyQt.QtGui import QAction, QIcon
except ImportError:
    from qgis.PyQt.QtGui import QIcon
    from qgis.PyQt.QtWidgets import QAction

PLUGIN_DIR = os.path.dirname(__file__)
MENU_TITLE = '&360ForYou Uploader'


class UploaderPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self._tasks = []    # Python refs — QgsTask wrappers must not be GC'd mid-run
        self._pollers = []

    # -- registry protocol used by main_dialog ------------------------------

    def track_task(self, task):
        self._tasks.append(task)

    def register_poller(self, poller):
        self._pollers.append(poller)

    # -- QGIS plugin interface ----------------------------------------------

    def initGui(self):
        icon = QIcon(os.path.join(PLUGIN_DIR, 'icon.png'))
        self.action = QAction(icon, self.tr('Upload to 360ForYou...'),
                              self.iface.mainWindow())
        self.action.setToolTip(self.tr('Upload layers and files to a 360ForYou project'))
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(MENU_TITLE, self.action)

    def unload(self):
        for poller in self._pollers:
            try:
                poller.stop()
            except RuntimeError:
                pass  # underlying C++ object already gone
        self._pollers = []
        self._tasks = []
        if self.action is not None:
            self.iface.removePluginMenu(MENU_TITLE, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

    def run(self):
        from .main_dialog import UploadDialog
        dialog = UploadDialog(self.iface, self)
        dialog.exec()

    # ------------------------------------------------------------------------

    def tr(self, message):
        return QCoreApplication.translate('UploaderPlugin', message)
