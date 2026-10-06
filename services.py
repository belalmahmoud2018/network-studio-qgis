"""Automatic service connections.

For every customer (point or building polygon) the tool finds the nearest main,
then:
  * reuses a tap that already sits on that main close by, or creates a new tap
  junction,
  * inserts a vertex in the main at the tap (so the network connects there),
  * draws the service line between the tap and the customer,
  * optionally creates the end device (meter / ONT / customer connection) at
  the customer.
Gravity networks (sewer, storm) draw the service towards the main so the flow
is right.
"""

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSpatialIndex,
)

from .qgis_io import _int, _val, class_layers, gkind, project_layer


class ServiceOptions:
    def __init__(self, **kw):
        self.customer_layer = kw.get("customer_layer")
        self.selected_only = kw.get("selected_only", False)
        self.id_field = kw.get("id_field")  # customer id copied to customerid
        self.main_groups = (
            kw.get("main_groups") or []
        )  # asset group codes of the Line class
        self.max_distance = kw.get("max_distance", 50.0)
        self.method = kw.get("method", "perpendicular")  # or "vertex"
        self.reuse_tap = kw.get(
            "reuse_tap", 0.5
        )  # reuse a tap closer than this
        self.create_tap = kw.get("create_tap", True)
        self.insert_vertex = kw.get("insert_vertex", True)
        self.create_end = kw.get("create_end", True)
        self.end_at = kw.get(
            "end_at", "edge"
        )  # polygons: "edge" or "centroid"
        self.skip_existing = kw.get("skip_existing", True)
        self.avoid_crossing = kw.get("avoid_crossing", True)
        self.min_length = kw.get(
            "min_length", 0.5
        )  # closer than this = touching the main
        self.clearance = kw.get(
            "clearance", 1.0
        )  # keep taps this far from other devices
        self.service_group = kw.get("service_group")  # (ag, at)
        self.tap_group = kw.get("tap_group")
        self.end_group = kw.get("end_group")


def _set(f, name, value):
    i = f.fields().indexFromName(name)
    if i >= 0:
        f.setAttribute(i, value)


def _new_feature(lyr, geom, values):
    f = QgsFeature(lyr.fields())
    f.setAttributes([None] * len(lyr.fields()))
    for name, val in values.items():
        _set(f, name, val)
    f.setGeometry(geom)
    return f


def run(cfg, opt, feedback=None):
    layers = class_layers(cfg)
    line_l, junc_l, dev_l = (
        layers.get("Line"),
        layers.get("Junction"),
        layers.get("Device"),
    )
    if line_l is None:
        raise ValueError("The network has no Line class.")
    cust = opt.customer_layer
    if cust is None:
        raise ValueError("Choose the customers layer.")
    if not opt.service_group:
        raise ValueError("Choose the asset group of the service line.")
    xform = None
    if cust.crs() != line_l.crs():
        xform = QgsCoordinateTransform(
            cust.crs(), line_l.crs(), QgsProject.instance()
        )
    tol = cfg.tolerance
    gravity = cfg.flow == "gravity"
    service_ag = opt.service_group[0]

    # mains and existing services
    mains, idx = {}, QgsSpatialIndex()
    existing_ids = set()
    for f in line_l.getFeatures():
        ag = _int(_val(f, "assetgroup"))
        if ag in opt.main_groups and f.hasGeometry():
            mains[f.id()] = QgsGeometry(f.geometry())
            idx.addFeature(f)
        if ag == service_ag and _val(f, "customerid"):
            existing_ids.add(str(_val(f, "customerid")))
    if not mains:
        raise ValueError("No main lines found for the chosen asset groups.")

    taps, tap_idx = {}, QgsSpatialIndex()
    if junc_l is not None and opt.tap_group:
        for f in junc_l.getFeatures():
            if (
                _int(_val(f, "assetgroup")) == opt.tap_group[0]
                and f.hasGeometry()
            ):
                taps[f.id()] = f.geometry().asPoint()
                tap_idx.addFeature(f)

    # devices / junctions already on the network (taps keep a clearance from
    # them)
    others, oth_idx = {}, QgsSpatialIndex()
    for lyr, skip_ag in (
        (dev_l, None),
        (junc_l, opt.tap_group[0] if opt.tap_group else None),
    ):
        if lyr is None:
            continue
        for f in lyr.getFeatures():
            if f.hasGeometry() and _int(_val(f, "assetgroup")) != skip_ag:
                key = len(others)
                others[key] = QgsPointXY(f.geometry().asPoint())
                oth_idx.addFeature(_pt_feature(key, others[key]))

    def clear_spot(mg, cpt, toward):
        """Slide the tap along the main until it is `clearance` away from other
        devices."""
        if not opt.clearance or not oth_idx.nearestNeighbor(
            cpt, 1, opt.clearance
        ):
            return cpt
        at = mg.lineLocatePoint(QgsGeometry.fromPointXY(cpt))
        best = None
        for step in (1, 2, 3, 4):
            for sign in (1, -1):
                pos = at + sign * step * opt.clearance
                if pos < 0 or pos > mg.length():
                    continue
                cand = mg.interpolate(pos).asPoint()
                if oth_idx.nearestNeighbor(
                    QgsPointXY(cand), 1, opt.clearance * 0.999
                ):
                    continue
                dd = QgsPointXY(cand).distance(toward)
                if best is None or dd < best[0]:
                    best = (dd, QgsPointXY(cand))
            if best:
                return best[1]
        return cpt

    req = QgsFeatureRequest()
    feats = (
        cust.getSelectedFeatures()
        if opt.selected_only
        else cust.getFeatures(req)
    )
    report = {
        "created": 0,
        "reused_taps": 0,
        "too_far": 0,
        "existing": 0,
        "crossing": 0,
        "touching": 0,
        "no_geometry": 0,
    }
    new_lines, new_taps, new_ends = [], [], []
    main_changes = {}
    poly = gkind(cust) == "polygon"
    total = (
        cust.selectedFeatureCount()
        if opt.selected_only
        else cust.featureCount()
    )

    for n, f in enumerate(feats):
        if feedback is not None:
            if feedback.isCanceled():
                break
            feedback.setProgress(100.0 * n / max(total, 1))
        cid = _val(f, opt.id_field) if opt.id_field else f.id()
        cid = None if cid is None else str(cid)
        if opt.skip_existing and cid is not None and cid in existing_ids:
            report["existing"] += 1
            continue
        g = QgsGeometry(f.geometry()) if f.hasGeometry() else None
        if g is None or g.isNull():
            report["no_geometry"] += 1
            continue
        if xform is not None:
            g.transform(xform)
        probe = g.centroid() if poly else g
        p0 = (
            probe.asPoint()
            if not probe.isMultipart()
            else probe.asMultiPoint()[0]
        )
        candidates = idx.nearestNeighbor(
            QgsPointXY(p0),
            5,
            opt.max_distance + (g.boundingBox().width() if poly else 0),
        )
        best = None
        crossed = touching = False
        for mid in candidates:
            mg = mains[mid]
            if poly:
                if opt.end_at == "edge":
                    edge = QgsGeometry(g.constGet().boundary())
                    sl = edge.shortestLine(mg)
                    end_pt = (
                        sl.asPolyline()[0] if sl and not sl.isNull() else p0
                    )
                else:
                    end_pt = p0
            else:
                end_pt = p0
            end_pt = QgsPointXY(end_pt)
            if opt.method == "vertex":
                cpt, vidx, _b, _a, dist2 = mg.closestVertex(end_pt)
                after = None
            else:
                dist2, cpt, after, _left = mg.closestSegmentWithContext(end_pt)
            d = dist2**0.5
            if d > opt.max_distance:
                continue
            if d < max(opt.min_length, tol):
                touching = True
                continue
            svc = QgsGeometry.fromPolylineXY([QgsPointXY(cpt), end_pt])
            if opt.avoid_crossing and _crosses_other(
                svc, mid, mains, idx, tol
            ):
                crossed = True
                continue
            if best is None or d < best[0]:
                best = (d, mid, QgsPointXY(cpt), after, end_pt)
        if best is None:
            report[
                (
                    "touching"
                    if touching
                    else "crossing" if crossed else "too_far"
                )
            ] += 1
            continue
        d, mid, cpt, after, end_pt = best
        if opt.method != "vertex":
            cpt = clear_spot(mains[mid], cpt, end_pt)

        # reuse a nearby tap, or create one
        reused = False
        if opt.reuse_tap and taps:
            for tid in tap_idx.nearestNeighbor(cpt, 1, opt.reuse_tap):
                cpt = QgsPointXY(taps[tid])
                reused = True
        if reused:
            report["reused_taps"] += 1
        else:
            if opt.insert_vertex and opt.method != "vertex":
                mg = mains[mid]
                _v, _i, _b, _a, vd2 = mg.closestVertex(cpt)
                if vd2**0.5 > tol:
                    _d2, _c, after, _l = mg.closestSegmentWithContext(cpt)
                    if mg.insertVertex(cpt.x(), cpt.y(), after):
                        main_changes[mid] = mg
            if opt.create_tap and junc_l is not None and opt.tap_group:
                new_taps.append(
                    _new_feature(
                        junc_l,
                        QgsGeometry.fromPointXY(cpt),
                        {
                            "assetgroup": opt.tap_group[0],
                            "assettype": opt.tap_group[1],
                            "lifecyclestatus": 3,
                        },
                    )
                )
                taps[-len(new_taps)] = cpt  # later customers may reuse it
                tf = QgsFeature()
                tf.setId(-len(new_taps))
                tf.setGeometry(QgsGeometry.fromPointXY(cpt))
                tap_idx.addFeature(tf)

        pts = [end_pt, cpt] if gravity else [cpt, end_pt]
        geom = QgsGeometry.fromPolylineXY(pts)
        new_lines.append(
            _new_feature(
                line_l,
                geom,
                {
                    "assetgroup": opt.service_group[0],
                    "assettype": opt.service_group[1],
                    "lifecyclestatus": 3,
                    "customerid": cid,
                    "measuredlength": round(geom.length(), 3),
                },
            )
        )
        if opt.create_end and dev_l is not None and opt.end_group:
            new_ends.append(
                _new_feature(
                    dev_l,
                    QgsGeometry.fromPointXY(end_pt),
                    {
                        "assetgroup": opt.end_group[0],
                        "assettype": opt.end_group[1],
                        "lifecyclestatus": 3,
                        "operatingstatus": 1,
                        "customerid": cid,
                    },
                )
            )
        if cid is not None:
            existing_ids.add(cid)
        report["created"] += 1

    _apply(cfg, line_l, new_lines, main_changes)
    if junc_l is not None and new_taps:
        _apply(cfg, junc_l, new_taps, {})
    if dev_l is not None and new_ends:
        _apply(cfg, dev_l, new_ends, {})
    report["mains_updated"] = len(main_changes)
    return report


def _pt_feature(fid, pt):
    f = QgsFeature()
    f.setId(fid)
    f.setGeometry(QgsGeometry.fromPointXY(pt))
    return f


def _crosses_other(svc, mid, mains, idx, tol):
    for oid in idx.intersects(svc.boundingBox()):
        if oid == mid:
            continue
        inter = svc.intersection(mains[oid])
        if inter is not None and not inter.isEmpty():
            return True
    return False


def _apply(cfg, lyr, new_feats, geom_changes):
    """Add features through the edit buffer when the layer is loaded (so the
    user can review
    and undo), or straight into the file otherwise."""
    from .qgis_io import layer_source

    path, name = layer_source(lyr)
    loaded = project_layer(path, name) is lyr
    if loaded:
        if not lyr.isEditable():
            lyr.startEditing()
        lyr.beginEditCommand("Network Studio: service connections")
        for fid, g in geom_changes.items():
            lyr.changeGeometry(fid, g)
        lyr.addFeatures(new_feats)
        lyr.endEditCommand()
        lyr.triggerRepaint()
    else:
        prov = lyr.dataProvider()
        if geom_changes:
            prov.changeGeometryValues(geom_changes)
        prov.addFeatures(new_feats)
