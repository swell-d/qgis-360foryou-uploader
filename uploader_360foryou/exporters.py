# -*- coding: utf-8 -*-
"""Layer assessment and export jobs.

Split into two phases because QGIS layer objects must not be touched from a
worker thread:

  MAIN THREAD   assess_layer() / build_upload_items() / prepare_export_jobs()
                — classification, remote names, layer.clone() for vectors,
                QgsRasterPipe with cloned provider+renderer for rasters.
  WORKER THREAD run_export_job() — the heavy QgsVectorFileWriter /
                QgsRasterFileWriter I/O on the pre-built thread-safe inputs
                (the same pattern QGIS core uses in QgsVectorFileWriterTask /
                QgsRasterFileWriterTask).

Server-side behaviors this module guards against:
  - a GeoTIFF without an embedded CRS is silently skipped by the server, so
    layers without a valid CRS are marked not uploadable here;
  - float/single-band rasters would tile poorly, so anything that is not
    already an RGB(A) Byte GeoTIFF file is exported as a rendered RGBA
    GeoTIFF (WYSIWYG — what the QGIS renderer shows);
  - the server accepts only KML/KMZ for vectors.
"""
import os
from dataclasses import dataclass

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsRasterFileWriter,
    QgsRasterPipe,
    QgsVectorFileWriter,
)

from .naming import remote_filename

KIND_RASTER_SOURCE = 'raster_source'    # source GeoTIFF file uploaded as-is
KIND_RASTER_RENDER = 'raster_render'    # rendered RGBA GeoTIFF export
KIND_VECTOR_KML = 'vector_kml'          # KML export
KIND_POINTCLOUD = 'pointcloud'          # LAS/LAZ source file uploaded as-is
KIND_DISK_FILE = 'disk_file'            # arbitrary file picked by the user

_WEB_RASTER_PROVIDERS = {'wms', 'xyz', 'arcgismapserver', 'wcs', 'vectortile'}
_LARGE_RENDER_MPX = 50


class ExportError(Exception):
    pass


@dataclass
class LayerAssessment:
    uploadable: bool
    kind: str = ''
    note: str = ''      # shown in the "Will upload as" column
    reason: str = ''    # why the layer cannot be uploaded


@dataclass
class UploadItem:
    kind: str
    display_name: str
    remote_name: str
    layer_id: str = ''
    source_path: str = ''   # set upfront for pass-through items, after export otherwise


@dataclass
class ExportJob:
    item: UploadItem
    vector_clone: object = None
    raster_pipe: object = None
    raster_cols: int = 0
    raster_rows: int = 0
    raster_extent: object = None
    raster_crs: object = None


def _source_file(layer):
    """Local file behind the layer, stripped of provider URI options."""
    source = (layer.source() or '').split('|')[0]
    return source if os.path.isfile(source) else ''


def assess_layer(layer):
    """Classify one map layer; returns None for unsupported layer types
    (mesh, annotation, vector tile, ...) that should not appear in the list."""
    layer_type = layer.type()
    if layer_type == Qgis.LayerType.Raster:
        return _assess_raster(layer)
    if layer_type == Qgis.LayerType.Vector:
        return _assess_vector(layer)
    if layer_type == Qgis.LayerType.PointCloud:
        return _assess_point_cloud(layer)
    return None


def _assess_raster(layer):
    provider = layer.dataProvider()
    if provider is None:
        return LayerAssessment(False, reason='Layer has no data provider')
    if not layer.crs().isValid():
        return LayerAssessment(False, reason='Layer has no CRS — set one in Layer Properties, '
                                             'otherwise the server skips the GeoTIFF')
    if provider.name() in _WEB_RASTER_PROVIDERS:
        return LayerAssessment(False, reason='Web/remote raster layers are not supported')
    source = _source_file(layer)
    if (provider.name() == 'gdal' and source
            and os.path.splitext(source)[1].lower() in ('.tif', '.tiff')
            and provider.bandCount() in (3, 4)
            and all(provider.dataType(b) == Qgis.DataType.Byte
                    for b in range(1, provider.bandCount() + 1))
            and layer.crs() == provider.crs()):
        return LayerAssessment(True, KIND_RASTER_SOURCE, note='Source GeoTIFF uploaded as-is')
    cols, rows = provider.xSize(), provider.ySize()
    if cols > 0 and rows > 0:
        mpx = cols * rows / 1e6
        note = 'Rendered RGBA GeoTIFF export'
        if mpx > _LARGE_RENDER_MPX:
            note += ' (~%d Mpx — may take a while)' % round(mpx)
        return LayerAssessment(True, KIND_RASTER_RENDER, note=note)
    return LayerAssessment(False, reason='Raster size is unknown — layer cannot be exported')


def _assess_vector(layer):
    if not layer.isSpatial():
        return LayerAssessment(False, reason='Table without geometry')
    if not layer.crs().isValid():
        return LayerAssessment(False, reason='Layer has no CRS — set one in Layer Properties')
    if layer.featureCount() == 0:
        return LayerAssessment(False, reason='Layer has no features')
    return LayerAssessment(True, KIND_VECTOR_KML, note='Exported as KML (EPSG:4326)')


def _assess_point_cloud(layer):
    provider = layer.dataProvider()
    source = _source_file(layer)
    if (provider is not None and provider.name() in ('pdal', 'copc')
            and source and source.lower().endswith(('.las', '.laz'))):
        return LayerAssessment(True, KIND_POINTCLOUD, note='LAS/LAZ source file uploaded as-is')
    return LayerAssessment(False, reason='Only local LAS/LAZ files are supported — '
                                         'EPT/VPC/remote point clouds cannot be uploaded')


def build_upload_items(checked, disk_files, allowed_exts):
    """Turn checked (layer, assessment) pairs + disk file paths into UploadItems
    with unique ASCII remote names. Returns (items, problems); problems are
    human-readable strings for files that had to be skipped."""
    taken = set()
    items = []
    problems = []
    for layer, assessment in checked:
        kind = assessment.kind
        if kind in (KIND_RASTER_SOURCE, KIND_POINTCLOUD):
            source = _source_file(layer)
            base = os.path.basename(source)
            stem, ext = os.path.splitext(base)
            items.append(UploadItem(kind, layer.name(),
                                    remote_filename(stem, ext, taken),
                                    layer_id=layer.id(), source_path=source))
        elif kind == KIND_RASTER_RENDER:
            items.append(UploadItem(kind, layer.name(),
                                    remote_filename(layer.name(), 'tif', taken),
                                    layer_id=layer.id()))
        elif kind == KIND_VECTOR_KML:
            items.append(UploadItem(kind, layer.name(),
                                    remote_filename(layer.name(), 'kml', taken),
                                    layer_id=layer.id()))
    allowed = {e.lower().lstrip('.') for e in allowed_exts}
    for path in disk_files:
        base = os.path.basename(path)
        stem, ext = os.path.splitext(base)
        if not os.path.isfile(path):
            problems.append('File not found: %s' % path)
            continue
        if ext.lower().lstrip('.') not in allowed:
            problems.append('File type not accepted by the server: %s' % base)
            continue
        items.append(UploadItem(KIND_DISK_FILE, base, remote_filename(stem, ext, taken),
                                source_path=path))
    return items, problems


def prepare_export_jobs(items, layers_by_id):
    """MAIN THREAD ONLY. Build thread-safe inputs for the worker."""
    jobs = []
    for item in items:
        job = ExportJob(item=item)
        if item.kind == KIND_VECTOR_KML:
            layer = layers_by_id[item.layer_id]
            job.vector_clone = layer.clone()
        elif item.kind == KIND_RASTER_RENDER:
            layer = layers_by_id[item.layer_id]
            provider = layer.dataProvider()
            renderer = layer.renderer()
            if renderer is None:
                raise ExportError('Layer "%s" has no renderer' % item.display_name)
            pipe = QgsRasterPipe()
            # set() places each interface according to its role (provider first,
            # then renderer) — no fragile index arithmetic.
            if not pipe.set(provider.clone()):
                raise ExportError('Cannot prepare raster pipe for "%s"' % item.display_name)
            if not pipe.set(renderer.clone()):
                raise ExportError('Cannot attach renderer for "%s"' % item.display_name)
            job.raster_pipe = pipe
            job.raster_cols = provider.xSize()
            job.raster_rows = provider.ySize()
            job.raster_extent = provider.extent()
            job.raster_crs = layer.crs()
        jobs.append(job)
    return jobs


def run_export_job(job, temp_dir, transform_context, feedback_factory):
    """WORKER THREAD. Execute one export; returns the produced local path.
    feedback_factory(is_raster) must return a fresh feedback object already
    registered with the owning task for cancellation."""
    item = job.item
    out_path = os.path.join(temp_dir, item.remote_name)
    if item.kind == KIND_VECTOR_KML:
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = _kml_driver()
        options.fileEncoding = 'UTF-8'
        options.layerName = item.display_name
        options.ct = QgsCoordinateTransform(job.vector_clone.crs(),
                                            QgsCoordinateReferenceSystem('EPSG:4326'),
                                            transform_context)
        _set_symbology_export(options)
        options.feedback = feedback_factory(False)
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            job.vector_clone, out_path, transform_context, options)
        error, message = result[0], (result[1] if len(result) > 1 else '')
        if error != _vector_no_error():
            raise ExportError('KML export of "%s" failed: %s' % (item.display_name,
                                                                 message or error))
    elif item.kind == KIND_RASTER_RENDER:
        writer = QgsRasterFileWriter(out_path)
        writer.setOutputFormat('GTiff')
        writer.setCreateOptions(['COMPRESS=DEFLATE', 'TILED=YES', 'BIGTIFF=IF_SAFER'])
        error = writer.writeRaster(job.raster_pipe, job.raster_cols, job.raster_rows,
                                   job.raster_extent, job.raster_crs, transform_context,
                                   feedback_factory(True))
        if error != _raster_no_error():
            raise ExportError('GeoTIFF export of "%s" failed (code %s)'
                              % (item.display_name, error))
    else:
        return item.source_path
    return out_path


def dispose_jobs(jobs):
    """MAIN THREAD (task finished()): drop clones/pipes explicitly."""
    for job in jobs:
        job.vector_clone = None
        job.raster_pipe = None


def _kml_driver():
    # LIBKML preserves more styling than the bare OGR KML driver.
    try:
        names = {d.driverName for d in QgsVectorFileWriter.ogrDriverList()}
    except Exception:
        return 'KML'
    return 'LIBKML' if 'LIBKML' in names else 'KML'


def _set_symbology_export(options):
    # Best-effort style export; enum home differs across QGIS versions.
    try:
        options.symbologyExport = Qgis.FeatureSymbologyExport.PerFeature
        return
    except AttributeError:
        pass
    try:
        options.symbologyExport = QgsVectorFileWriter.SymbologyExport.FeatureSymbology
    except AttributeError:
        pass


def _vector_no_error():
    try:
        return QgsVectorFileWriter.WriterError.NoError
    except AttributeError:
        return QgsVectorFileWriter.NoError


def _raster_no_error():
    try:
        return QgsRasterFileWriter.WriterError.NoError
    except AttributeError:
        return QgsRasterFileWriter.NoError
