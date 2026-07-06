# -*- coding: utf-8 -*-
"""360ForYou Uploader — QGIS plugin entry point."""


def classFactory(iface):
    from .plugin import UploaderPlugin
    return UploaderPlugin(iface)
