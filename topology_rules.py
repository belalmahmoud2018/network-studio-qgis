"""General topology rules between any layers (like geodatabase topology):
polygons that must not overlap or leave gaps, lines that must not cross,
points that must lie on lines or inside polygons...

The rules are stored in the network (un_topology_rules); the layers are
referenced by their data source, so any layer of the project can take part,
not only the network classes. Broken rules go to the error layers with the
codes T01 - T14 and can be marked as exceptions like the other errors.
"""

from qgis.core import (
    QgsFeatureRequest,
    QgsGeometry,
    QgsProject,
    QgsSpatialIndex,
    QgsVectorLayer,
)

from . import storage as S
from .qgis_io import gkind

TABLE = "un_topology_rules"
FIELDS = [
    ("rule", S.ogr.OFTString),
    ("layer_a", S.ogr.OFTString),
    ("layer_b", S.ogr.OFTString),
    ("tolerance", S.ogr.OFTReal),
    ("enabled", S.ogr.OFTInteger),
]

# key: (code, label, geometry of A, geometry of B or None)
RULES = {
    "poly_no_overlap": ("T01", "Must not overlap", "polygon", None),
    "poly_no_gaps": ("T02", "Must not have gaps", "polygon", None),
    "poly_no_overlap_with": (
        "T03",
        "Must not overlap with",
        "polygon",
        "polygon",
    ),
    "poly_covered_by": ("T04", "Must be covered by", "polygon", "polygon"),
    "line_no_overlap": ("T05", "Must not overlap", "line", None),
    "line_no_intersect": ("T06", "Must not intersect", "line", None),
    "line_no_self_intersect": (
        "T07",
        "Must not self-intersect",
        "line",
        None,
    ),
    "line_no_dangles": ("T08", "Must not have dangles", "line", None),
    "line_inside": ("T09", "Must be inside", "line", "polygon"),
    "point_on_line": ("T10", "Must be covered by line", "point", "line"),
    "point_inside": ("T11", "Must be properly inside", "point", "polygon"),
    "no_duplicates": ("T12", "Must not have duplicates", "any", None),
    "valid": ("T13", "Must be valid geometry", "any", None),
    "point_on_endpoint": (
        "T14",
        "Must be covered by endpoint of",
        "point",
        "line",
    ),
}


def describe(key):
    code, label, ga, gb = RULES[key]
    return "%s  %s (%s%s)" % (
        code,
        label,
        ga,
        (" / " + gb) if gb else "",
    )


# ------------------------------------------------------------------ storage
def ensure_table(path):
    ds = S.open_ds(path, update=True)
    try:
        S._make_table(ds, TABLE, FIELDS)
    finally:
        ds = None


def load(path):
    ds = S.open_ds(path)
    try:
        rows = S._read_rows(ds, TABLE)
    finally:
        ds = None
    for r in rows:
        r["enabled"] = 0 if r.get("enabled") in (0, "0") else 1
    return rows


def save(path, rows):
    ensure_table(path)
    ds = S.open_ds(path, update=True)
    try:
        S._rewrite(
            ds,
            TABLE,
            FIELDS,
            [{k: r.get(k) for k, _t in FIELDS} for r in rows],
        )
    finally:
        ds = None


def find_layer(source):
    """Project layer with this data source, else the layer opened from it."""
    if not source:
        return None
    for lyr in QgsProject.instance().mapLayers().values():
        if isinstance(lyr, QgsVectorLayer) and lyr.source() == source:
            return lyr
    lyr = QgsVectorLayer(source, source.split("layername=")[-1], "ogr")
    return lyr if lyr.isValid() else None


def layer_label(lyr):
    return lyr.name() if lyr is not None else "?"


# ------------------------------------------------------------------ checks
def _issue(code, msg, geom, lyr, fid, sev="error"):
    pt = (
        geom.pointOnSurface()
        if geom is not None and not geom.isNull()
        else None
    )
    if pt is None or pt.isNull():
        pt = geom.centroid() if geom is not None else None
    p = pt.asPoint() if pt is not None and not pt.isNull() else None
    if p is None:
        return None
    return {
        "code": code,
        "severity": sev,
        "message": msg,
        "x": p.x(),
        "y": p.y(),
        "key": (lyr.name(), fid),
        "geom": "point",
    }


def _features(lyr, selected_only=False, fix=True):
    """{fid: geometry}; invalid geometries are repaired for the overlay
    rules (the 'valid' rule reports them)."""
    req = QgsFeatureRequest()
    if selected_only:
        req.setFilterFids(lyr.selectedFeatureIds())
    out = {}
    for f in lyr.getFeatures(req):
        if not f.hasGeometry():
            continue
        g = f.geometry()
        if fix and not g.isGeosValid():
            fixed = g.makeValid()
            if not fixed.isNull():
                g = fixed
        out[f.id()] = g
    return out


def _index(geoms):
    idx = QgsSpatialIndex()
    for fid, g in geoms.items():
        idx.addFeature(fid, g.boundingBox())
    return idx


def _pairs(geoms, idx, tol=0.0):
    for fid, g in geoms.items():
        box = g.boundingBox()
        box.grow(tol)
        for other in idx.intersects(box):
            if other > fid:
                yield fid, other


def run_rule(key, lyr_a, lyr_b=None, tol=0.0, selected_only=False):
    code, label, _ga, _gb = RULES[key]
    a = _features(lyr_a, selected_only, fix=key != "valid")
    out = []

    def add(msg, geom, fid):
        it = _issue(code, "%s: %s" % (label, msg), geom, lyr_a, fid)
        if it is not None:
            out.append(it)

    if key in ("poly_no_overlap", "line_no_overlap"):
        idx = _index(a)
        for f1, f2 in _pairs(a, idx):
            inter = a[f1].intersection(a[f2])
            if inter.isNull() or inter.isEmpty():
                continue
            if key == "poly_no_overlap" and inter.area() > tol * tol:
                add("overlaps feature %d" % f2, inter, f1)
            elif key == "line_no_overlap" and inter.length() > tol:
                add("overlaps feature %d" % f2, inter, f1)
    elif key == "poly_no_gaps":
        if a:
            union = QgsGeometry.unaryUnion(list(a.values()))
            polys = []
            if not union.isNull():
                if union.isMultipart():
                    polys = union.asMultiPolygon()
                elif gkind(union) == "polygon":
                    polys = [union.asPolygon()]
            for poly in polys:
                for ring in poly[1:]:
                    hole = QgsGeometry.fromPolygonXY([ring])
                    if hole.area() > tol * tol:
                        fid = next(
                            (
                                f
                                for f, g in a.items()
                                if g.boundingBox().intersects(
                                    hole.boundingBox()
                                )
                            ),
                            -1,
                        )
                        add("gap of %.2f" % hole.area(), hole, fid)
    elif key == "line_no_intersect":
        idx = _index(a)
        for f1, f2 in _pairs(a, idx):
            if not a[f1].intersects(a[f2]):
                continue
            inter = a[f1].intersection(a[f2])
            if inter.isNull() or inter.isEmpty():
                continue
            ends = []
            for g in (a[f1], a[f2]):
                for part in (
                    g.asMultiPolyline()
                    if g.isMultipart()
                    else [g.asPolyline()]
                ):
                    if part:
                        ends += [part[0], part[-1]]
            pts = inter.asGeometryCollection() or [inter]
            for p in pts:
                if gkind(p) != "point":
                    add("overlaps feature %d" % f2, p, f1)
                    continue
                pp = p.asPoint()
                if not any(pp.distance(e) <= max(tol, 1e-9) for e in ends):
                    add("crosses feature %d" % f2, p, f1)
    elif key == "line_no_self_intersect":
        for fid, g in a.items():
            for part in (
                g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            ):
                line = QgsGeometry.fromPolylineXY(part)
                if len(part) > 3 and not line.isSimple():
                    add("crosses itself", line, fid)
    elif key == "line_no_dangles":
        ends = {}
        for fid, g in a.items():
            for part in (
                g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            ):
                if part:
                    for p in (part[0], part[-1]):
                        ends.setdefault(
                            (
                                round(p.x() / max(tol, 1e-6)),
                                round(p.y() / max(tol, 1e-6)),
                            ),
                            [],
                        ).append((fid, p))
        idx = _index(a)
        for items in ends.values():
            if len(items) > 1:
                continue
            fid, p = items[0]
            pg = QgsGeometry.fromPointXY(p)
            box = pg.boundingBox()
            box.grow(max(tol, 1e-6))
            touching = [
                o
                for o in idx.intersects(box)
                if o != fid and a[o].distance(pg) <= max(tol, 1e-6)
            ]
            if not touching:
                add("dangling end", pg, fid)
    elif key == "no_duplicates":
        seen = {}
        for fid, g in a.items():
            k = (
                g.asWkb().data()
                if hasattr(g.asWkb(), "data")
                else bytes(g.asWkb())
            )
            if k in seen:
                add("duplicate of feature %d" % seen[k], g, fid)
            else:
                seen[k] = fid
    elif key == "valid":
        for fid, g in a.items():
            errs = g.validateGeometry()
            if errs:
                add(errs[0].what(), g, fid)
    else:
        if lyr_b is None:
            raise ValueError("The rule '%s' needs a second layer." % label)
        b = _features(lyr_b)
        idx = _index(b)

        def near(g, grow=0.0):
            box = g.boundingBox()
            box.grow(grow)
            return [b[o] for o in idx.intersects(box)]

        for fid, g in a.items():
            cands = near(g, tol)
            if key == "poly_no_overlap_with":
                for o in cands:
                    inter = g.intersection(o)
                    if not inter.isNull() and inter.area() > tol * tol:
                        add("overlaps %s" % lyr_b.name(), inter, fid)
                        break
            elif key in ("poly_covered_by", "line_inside"):
                cover = (
                    QgsGeometry.unaryUnion(cands) if cands else QgsGeometry()
                )
                if cover.isNull():
                    add("outside %s" % lyr_b.name(), g, fid)
                    continue
                rest = g.difference(cover.buffer(tol, 4) if tol else cover)
                bad = (
                    rest.area() > tol * tol
                    if key == "poly_covered_by"
                    else rest.length() > tol
                )
                if not rest.isNull() and not rest.isEmpty() and bad:
                    add("not inside %s" % lyr_b.name(), rest, fid)
            elif key == "point_on_line":
                if not any(o.distance(g) <= tol for o in cands):
                    add("not on %s" % lyr_b.name(), g, fid)
            elif key == "point_on_endpoint":
                ok = False
                for o in cands:
                    for part in (
                        o.asMultiPolyline()
                        if o.isMultipart()
                        else [o.asPolyline()]
                    ):
                        if part and any(
                            QgsGeometry.fromPointXY(e).distance(g) <= tol
                            for e in (part[0], part[-1])
                        ):
                            ok = True
                if not ok:
                    add("not on an end of %s" % lyr_b.name(), g, fid)
            elif key == "point_inside":
                if not any(o.contains(g) for o in cands):
                    add("not inside %s" % lyr_b.name(), g, fid)
    return out


def run_all(rows, selected_only=False, feedback=None):
    """Run the enabled rules. Returns (issues, [(rule text, count)],
    errors)."""
    issues, report, errors = [], [], []
    for r in rows:
        if not r.get("enabled"):
            continue
        key = r.get("rule")
        if key not in RULES:
            errors.append("Unknown rule %s" % key)
            continue
        la = find_layer(r.get("layer_a"))
        lb = find_layer(r.get("layer_b")) if r.get("layer_b") else None
        if la is None:
            errors.append("%s: layer not found" % describe(key))
            continue
        try:
            found = run_rule(
                key, la, lb, float(r.get("tolerance") or 0.0), selected_only
            )
        except ValueError as e:
            errors.append(str(e))
            continue
        issues += found
        report.append(
            (
                "%s  %s%s"
                % (
                    describe(key),
                    la.name(),
                    (" / " + lb.name()) if lb is not None else "",
                ),
                len(found),
            )
        )
        if feedback:
            feedback(key)
    return issues, report, errors
