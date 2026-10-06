"""Smart editing: network behaviour while you draw with the normal QGIS tools.

When it is on, for every feature added (or reshaped) in a network class:
  * a new point near a line is snapped onto it and the line gets a vertex
  there,
    or is split in two (option), so the point is connected;
  * the ends of a new line are snapped onto the nearest point / line end / line
    (and a vertex is added in that line), so the line is connected;
  * the new connections are checked against the connectivity rules and a
  warning
    is shown at once when a rule is broken.
"""

from qgis.core import (
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QTimer

from .qgis_io import _int, _lines_of, _point_of, gkind, project_layer


class SmartEditor:
    def __init__(self, iface):
        self.iface = iface
        self.cfg = None
        self.enabled = False
        self.snap = True
        # split the line under a new device instead of adding a vertex
        self.split = False
        self.check_rules = True
        self.distance = None  # snap distance (default: network gap distance)
        self._layers = {}  # class -> layer
        self._pending = []
        self._busy = False
        self._connected = []

    # ------------------------------------------------------------ wiring
    def set_network(self, cfg):
        self.detach()
        self.cfg = cfg
        if self.enabled:
            self.attach()

    def set_enabled(self, on):
        self.enabled = on
        self.detach()
        if on:
            self.attach()

    def attach(self):
        if self.cfg is None:
            return 0
        for role, cls in self.cfg.classes.items():
            if role.startswith("Structure") or role == "Assembly":
                continue
            lyr = project_layer(self.cfg.path, cls)
            if lyr is None:
                continue
            self._layers[cls] = lyr
            a = lambda fid, l=lyr: self._queue(l, fid)  # noqa: E731,E741
            g = lambda fid, _geom, l=lyr: self._queue(l, fid)  # noqa
            lyr.featureAdded.connect(a)
            lyr.geometryChanged.connect(g)
            self._connected.append((lyr, a, g))
        return len(self._connected)

    def detach(self):
        for lyr, a, g in self._connected:
            try:
                lyr.featureAdded.disconnect(a)
                lyr.geometryChanged.disconnect(g)
            except (TypeError, RuntimeError):
                pass
        self._connected = []
        self._layers = {}

    def _queue(self, lyr, fid):
        if self._busy:
            return
        self._pending.append((lyr, fid))
        if len(self._pending) == 1:
            QTimer.singleShot(0, self._process)

    # ------------------------------------------------------------ processing
    def _process(self):
        items, self._pending = self._pending, []
        self._busy = True
        warnings = []
        try:
            for lyr, fid in items:
                try:
                    f = next(lyr.getFeatures(QgsFeatureRequest(fid)), None)
                except RuntimeError:
                    continue
                if f is None or not f.hasGeometry():
                    continue
                if gkind(lyr) == "point":
                    warnings += self._point_added(lyr, f)
                elif gkind(lyr) == "line":
                    warnings += self._line_added(lyr, f)
        finally:
            self._busy = False
        if warnings:
            self.iface.messageBar().pushWarning(
                "Network Studio", "; ".join(sorted(set(warnings)))[:400]
            )

    @staticmethod
    def _edit(lyr):
        """The other network layer must be editable for the automatic
        change."""
        if not lyr.isEditable():
            lyr.startEditing()
        return lyr

    def _dist(self):
        return float(self.distance or self.cfg.gap)

    def _cls(self, lyr):
        for cls, l in self._layers.items():
            if l is lyr:
                return cls
        return None

    def _near(self, pt, radius, kinds=("point", "line"), skip=None):
        """[(dist, cls, layer, feature)] of network features near a point."""
        out = []
        rect = QgsRectangle(
            pt.x() - radius, pt.y() - radius, pt.x() + radius, pt.y() + radius
        )
        pg = QgsGeometry.fromPointXY(pt)
        for cls, lyr in self._layers.items():
            if gkind(lyr) not in kinds:
                continue
            for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                if skip is not None and lyr is skip[0] and f.id() == skip[1]:
                    continue
                d = f.geometry().distance(pg)
                if d <= radius:
                    out.append((d, cls, lyr, f))
        return sorted(out, key=lambda x: x[0])

    def _point_added(self, lyr, f):
        tol = self.cfg.tolerance
        pt = QgsPointXY(_point_of(f.geometry()))
        hits = self._near(pt, self._dist(), ("line",))
        if not hits:
            return []
        _d, lcls, llyr, lf = hits[0]
        g = QgsGeometry(lf.geometry())
        vpt, vidx, _b, _a, vd2 = g.closestVertex(pt)
        target = QgsPointXY(vpt)
        if vd2**0.5 > max(tol, self._dist() * 0.3):
            sd2, spt, after, _l = g.closestSegmentWithContext(pt)
            target = QgsPointXY(spt)
            if self.split and not g.isMultipart():
                self._split_line(llyr, lf, target, after)
            else:
                g.insertVertex(target.x(), target.y(), after)
                self._edit(llyr).changeGeometry(lf.id(), g)
        elif (
            self.split
            and not g.isMultipart()
            and 0 < vidx < len(g.asPolyline()) - 1
        ):
            self._split_line(llyr, lf, target, None, vidx)
        if self.snap and target.distance(pt) > 0:
            lyr.changeGeometry(f.id(), QgsGeometry.fromPointXY(target))
        return self._rules_at(target) if self.check_rules else []

    def _split_line(self, llyr, lf, pt, after, at_vertex=None):
        self._edit(llyr)
        coords = lf.geometry().asPolyline()
        if at_vertex is None:
            coords = coords[:after] + [pt] + coords[after:]
            at_vertex = after
        part1, part2 = coords[: at_vertex + 1], coords[at_vertex:]
        if len(part1) < 2 or len(part2) < 2:
            return
        llyr.changeGeometry(lf.id(), QgsGeometry.fromPolylineXY(part1))
        nf = QgsFeature(lf)
        nf.setId(-1)
        pk = llyr.dataProvider().pkAttributeIndexes()
        for i in pk:
            nf.setAttribute(i, None)
        i = llyr.fields().indexFromName("fid")
        if i >= 0:
            nf.setAttribute(i, None)
        nf.setGeometry(QgsGeometry.fromPolylineXY(part2))
        llyr.addFeature(nf)

    def _line_added(self, lyr, f):
        tol = self.cfg.tolerance
        g = QgsGeometry(f.geometry())
        parts = _lines_of(g)
        changed = False
        ends = []
        for pi, part in enumerate(parts):
            for local in (0, len(part) - 1):
                x, y = part[local]
                pt = QgsPointXY(x, y)
                vid = sum(len(p) for p in parts[:pi]) + local
                target = self._snap_end(lyr, f, pt) if self.snap else None
                if target is not None and target.distance(pt) > tol:
                    g.moveVertex(target.x(), target.y(), vid)
                    changed = True
                ends.append(target or pt)
        if changed:
            lyr.changeGeometry(f.id(), g)
        warnings = []
        if self.check_rules:
            for pt in ends:
                warnings += self._rules_at(pt)
        return warnings

    def _snap_end(self, lyr, f, pt):
        tol = self.cfg.tolerance
        hits = self._near(pt, self._dist(), skip=(lyr, f.id()))
        if not hits:
            return None
        # already connected?
        for d, _cls, hl, hf in hits:
            if d > tol:
                break
            if gkind(hl) == "point":
                return None
            _v, _i, _b, _a, vd2 = hf.geometry().closestVertex(pt)
            if vd2**0.5 <= tol:
                return None
        # points first, then line ends / vertices, then segments
        pts = [h for h in hits if gkind(h[2]) == "point"]
        if pts:
            return QgsPointXY(_point_of(pts[0][3].geometry()))
        _d, _cls, hl, hf = hits[0]
        hg = QgsGeometry(hf.geometry())
        vpt, _i, _b, _a, vd2 = hg.closestVertex(pt)
        if vd2**0.5 <= self._dist():
            return QgsPointXY(vpt)
        sd2, spt, after, _l = hg.closestSegmentWithContext(pt)
        hg.insertVertex(spt.x(), spt.y(), after)
        self._edit(hl).changeGeometry(hf.id(), hg)
        return QgsPointXY(spt)

    def _rules_at(self, pt):
        rules = self.cfg.rules
        if not rules:
            return []
        groups = []
        for d, cls, lyr, f in self._near(pt, self.cfg.tolerance * 2):
            ag = (
                _int(f["assetgroup"])
                if f.fields().indexFromName("assetgroup") >= 0
                else None
            )
            if ag is not None:
                groups.append((gkind(lyr), cls, ag, f.id()))
        has_point = any(k == "point" for k, *_r in groups)
        out = []
        for i, (ka, ca, aa, fa) in enumerate(groups):
            for kb, cb, ab, fb in groups[i + 1:]:
                if ka == "point" and kb == "point":
                    continue
                if ka == "line" and kb == "line" and has_point:
                    continue
                if (ca, fa) == (cb, fb):
                    continue
                pair = tuple(sorted(((ca, aa), (cb, ab)), key=str))
                if pair not in rules:
                    out.append(
                        "No rule: %s - %s"
                        % (self.cfg.label(ca, aa), self.cfg.label(cb, ab))
                    )
        return out
