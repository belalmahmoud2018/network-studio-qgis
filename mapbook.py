"""Map book: a grid of numbered pages over the network and a QGIS print layout
(atlas)
that can be exported to one PDF."""

import math

from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsLayoutExporter,
    QgsLayoutItemLabel,
    QgsLayoutItemMap,
    QgsLayoutItemScaleBar,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsPrintLayout,
    QgsProject,
    QgsRectangle,
    QgsUnitTypes,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QFont

from .qgis_io import class_layers, gkind

LAYOUT_NAME = "Network Studio map book"


def make_grid(
    cfg, scale=1000, paper=(277, 170), overlap=0.05, only_with_features=True
):
    """Pages sized for the map frame (mm) at `scale`; only cells that contain
    network features."""
    layers = class_layers(cfg)
    extent = None
    for lyr in layers.values():
        e = lyr.extent()
        if not e.isNull() and not e.isEmpty():
            if extent is None:
                extent = QgsRectangle(e)
            else:
                extent.combineExtentWith(e)
    if extent is None or extent.isEmpty():
        raise ValueError("The network has no features yet.")
    w = paper[0] / 1000.0 * scale
    h = paper[1] / 1000.0 * scale
    step_x, step_y = w * (1 - overlap), h * (1 - overlap)
    cols = max(1, math.ceil(extent.width() / step_x))
    rows = max(1, math.ceil(extent.height() / step_y))
    crs = (layers.get("Line") or next(iter(layers.values()))).crs()
    grid = QgsVectorLayer(
        "Polygon?crs=%s&field=page:integer&field=row:integer&field=col:integer"
        % (crs.authid() or crs.toWkt()),
        "Map book pages",
        "memory",
    )
    feats = []
    page = 0
    for r in range(rows):
        for c in range(cols):
            rect = QgsRectangle(
                extent.xMinimum() + c * step_x,
                extent.yMaximum() - r * step_y - h,
                extent.xMinimum() + c * step_x + w,
                extent.yMaximum() - r * step_y,
            )
            if only_with_features and not any(
                next(lyr.getFeatures(rect), None) is not None
                for lyr in layers.values()
                if gkind(lyr) in ("point", "line")
            ):
                continue
            page += 1
            f = QgsFeature(grid.fields())
            f.setAttributes([page, r + 1, c + 1])
            f.setGeometry(QgsGeometry.fromRect(rect))
            feats.append(f)
    grid.dataProvider().addFeatures(feats)
    from qgis.core import QgsFillSymbol

    grid.renderer().setSymbol(
        QgsFillSymbol.createSimple(
            {
                "color": "0,0,0,0",
                "outline_color": "#ba7517",
                "outline_width": "0.5",
                "outline_style": "dash",
            }
        )
    )
    from qgis.core import QgsPalLayerSettings, QgsVectorLayerSimpleLabeling

    lab = QgsPalLayerSettings()
    lab.fieldName = "page"
    grid.setLabeling(QgsVectorLayerSimpleLabeling(lab))
    grid.setLabelsEnabled(True)
    QgsProject.instance().addMapLayer(grid)
    return grid


def make_layout(cfg, grid, title, scale=1000, brand=None):
    project = QgsProject.instance()
    mgr = project.layoutManager()
    old = mgr.layoutByName(LAYOUT_NAME)
    if old is not None:
        mgr.removeLayout(old)
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()  # A4 landscape
    layout.setName(LAYOUT_NAME)
    mgr.addLayout(layout)
    mm = (
        QgsUnitTypes.LayoutMillimeters
        if hasattr(QgsUnitTypes, "LayoutMillimeters")
        else QgsUnitTypes.LayoutUnit.LayoutMillimeters
    )
    m = QgsLayoutItemMap(layout)
    m.attemptMove(QgsLayoutPoint(10, 22, mm))
    m.attemptResize(QgsLayoutSize(277, 170, mm))
    m.setFrameEnabled(True)
    m.setCrs(grid.crs())
    m.zoomToExtent(grid.extent())
    m.setAtlasDriven(True)
    m.setAtlasScalingMode(
        QgsLayoutItemMap.Fixed
        if hasattr(QgsLayoutItemMap, "Fixed")
        else QgsLayoutItemMap.AtlasScalingMode.Fixed
    )
    m.setScale(scale)
    layers = list(class_layers(cfg).values()) + [grid]
    m.setLayers([x for x in layers if project.mapLayer(x.id()) is not None])
    m.setKeepLayerSet(False)
    layout.addLayoutItem(m)
    lab = QgsLayoutItemLabel(layout)
    lab.setText(
        "%s  -  page [%% @atlas_featurenumber %%] of [%%"
        " @atlas_totalfeatures %%]" % title
    )
    f = QFont()
    f.setPointSize(14)
    f.setBold(True)
    try:
        from qgis.core import QgsTextFormat

        tf = QgsTextFormat.fromQFont(f)
        lab.setTextFormat(tf)
    except Exception:
        lab.setFont(f)
    lab.attemptMove(QgsLayoutPoint(10, 6, mm))
    lab.attemptResize(QgsLayoutSize(277, 12, mm))
    layout.addLayoutItem(lab)
    sb = QgsLayoutItemScaleBar(layout)
    sb.setLinkedMap(m)
    try:
        sb.setStyle("Single Box")
    except Exception as e:  # older QGIS: keep the default scale bar style
        sb.setToolTip(str(e))
    sb.applyDefaultSize()
    sb.attemptMove(QgsLayoutPoint(12, 196, mm))
    layout.addLayoutItem(sb)
    brand = brand or {}
    text = "  |  ".join(
        v
        for v in (
            brand.get("company"),
            brand.get("client"),
            brand.get("project"),
            brand.get("contact"),
        )
        if v
    )
    if text:
        foot = QgsLayoutItemLabel(layout)
        foot.setText(text)
        foot.attemptMove(QgsLayoutPoint(80, 196, mm))
        foot.attemptResize(QgsLayoutSize(160, 8, mm))
        layout.addLayoutItem(foot)
    logo = brand.get("logo")
    if logo:
        import os

        if os.path.exists(logo):
            from qgis.core import QgsLayoutItemPicture

            pic = QgsLayoutItemPicture(layout)
            pic.setPicturePath(logo)
            pic.attemptMove(QgsLayoutPoint(247, 190, mm))
            pic.attemptResize(QgsLayoutSize(40, 16, mm))
            layout.addLayoutItem(pic)
    atlas = layout.atlas()
    atlas.setCoverageLayer(grid)
    atlas.setHideCoverage(False)
    atlas.setEnabled(True)
    atlas.setSortFeatures(True)
    atlas.setSortExpression("page")
    return layout


def export_pdf(layout, path):
    exporter_settings = QgsLayoutExporter.PdfExportSettings()
    res = QgsLayoutExporter.exportToPdf(
        layout.atlas(), path, exporter_settings
    )
    code = res[0] if isinstance(res, tuple) else res
    if int(getattr(code, "value", code)) != 0:
        raise ValueError(
            "PDF export failed: %s"
            % (res[1] if isinstance(res, tuple) else code)
        )
    return path
