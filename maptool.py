"""Map tool that picks network features for traces.

It follows the project snapping settings (Snapping toolbar): while the mouse moves the
usual snap indicator is shown, and a click on a snapped vertex / segment / point uses
that feature and that exact location. Without a snap match the nearest network feature
under the cursor is used.
"""
from qgis.core import (QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsPointLocator, QgsPointXY,
                       QgsProject, QgsRectangle)
from qgis.gui import QgsMapToolEmitPoint, QgsSnapIndicator, QgsVertexMarker
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QColor

from .qgis_io import gkind

COLORS = {"start": "#1a9850", "barrier": "#d73027", "end": "#4575b4"}


def _icon(name):
    value = getattr(QgsVertexMarker, name, None)
    return value if value is not None else getattr(QgsVertexMarker.IconType, name)


class PickTool(QgsMapToolEmitPoint):
    picked = pyqtSignal(object)  # dict(key, x, y, kind, marker)

    def __init__(self, canvas, layers_fn):
        super().__init__(canvas)
        self.canvas = canvas
        self.layers_fn = layers_fn  # -> {class name: layer}
        self.kind = "start"
        self.indicator = QgsSnapIndicator(canvas)

    def deactivate(self):
        self.indicator.setMatch(QgsPointLocator.Match())
        super().deactivate()

    def _snap(self, event):
        try:
            return self.canvas.snappingUtils().snapToMap(event.mapPoint())
        except Exception:
            return None

    def canvasMoveEvent(self, event):
        m = self._snap(event)
        self.indicator.setMatch(m if m is not None else QgsPointLocator.Match())

    def canvasReleaseEvent(self, event):
        layers = self.layers_fn()
        match = self._snap(event)
        hit = None
        if match is not None and match.isValid() and match.layer() is not None:
            for cls, lyr in layers.items():
                if lyr is match.layer() and match.featureId() >= 0:
                    xf = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), lyr.crs(),
                                                QgsProject.instance())
                    hit = (cls, match.featureId(), xf.transform(match.point()), lyr)
                    break
        if hit is None:
            hit = self._nearest(event.mapPoint(), layers)
        if hit is None:
            return
        self.emit_pick(*hit)

    def emit_pick(self, cls, fid, p, lyr):
        if gkind(lyr) == "point":
            f = next(lyr.getFeatures(QgsFeatureRequest(fid)), None)
            if f is not None and f.hasGeometry():
                g = f.geometry()
                p = g.asPoint() if not g.isMultipart() else g.asMultiPoint()[0]
        marker = QgsVertexMarker(self.canvas)
        back = QgsCoordinateTransform(lyr.crs(), self.canvas.mapSettings().destinationCrs(), QgsProject.instance())
        marker.setCenter(back.transform(QgsPointXY(p)))
        marker.setColor(QColor(COLORS.get(self.kind, "#000000")))
        marker.setFillColor(QColor(COLORS.get(self.kind, "#000000")))
        marker.setIconSize(14)
        marker.setPenWidth(3)
        marker.setIconType(_icon("ICON_CIRCLE" if self.kind == "start" else
                                 "ICON_X" if self.kind == "barrier" else "ICON_BOX"))
        self.picked.emit({"key": (cls, fid), "x": p.x(), "y": p.y(), "kind": self.kind, "marker": marker})

    def _nearest(self, point, layers):
        radius = self.canvas.mapUnitsPerPixel() * 8
        best = None
        for cls, lyr in layers.items():
            if gkind(lyr) not in ("point", "line"):
                continue
            xf = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), lyr.crs(), QgsProject.instance())
            p = xf.transform(point)
            q = xf.transform(QgsPointXY(point.x() + radius, point.y()))
            r = max(abs(q.x() - p.x()), abs(q.y() - p.y()), 1e-9)
            rect = QgsRectangle(p.x() - r, p.y() - r, p.x() + r, p.y() + r)
            pg = QgsGeometry.fromPointXY(QgsPointXY(p))
            for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                d = f.geometry().distance(pg) - (r * 0.5 if gkind(lyr) == "point" else 0)
                if d <= r and (best is None or d < best[0]):
                    best = (d, cls, f.id(), QgsPointXY(p), lyr)
        return best[1:] if best else None
