"""Automatic preparation of a network before validation / tracing.

prepare()        closes gaps and adds the vertices connectivity needs:
                   * point features are moved onto the nearest line end /
                   vertex / segment,
                   * line ends are moved onto the nearest point / line end /
                   segment,
                   * a vertex is inserted where something touches a segment
                   between vertices.
associations()   containment (points inside assemblies / structure boundaries)
and
                 structural attachment (devices next to poles / manholes).
"""

import math

from qgis.core import QgsGeometry, QgsPointXY, QgsSpatialIndex

from . import storage as S
from .qgis_io import (
    _lines_of,
    _point_of,
    class_layers,
    gkind,
    layer_source,
    project_layer,
)


def _begin(lyr):
    path, name = layer_source(lyr)
    if project_layer(path, name) is lyr:
        if not lyr.isEditable():
            lyr.startEditing()
        lyr.beginEditCommand("Network Studio: prepare network")
        return True
    return False


def _end(lyr, loaded, changes):
    if loaded:
        for fid, g in changes.items():
            lyr.changeGeometry(fid, g)
        lyr.endEditCommand()
        lyr.triggerRepaint()
    elif changes:
        lyr.dataProvider().changeGeometryValues(changes)


def prepare(cfg, gap=None, snap_points=True, snap_ends=True, feedback=None):
    gap = float(gap or cfg.gap)
    tol = cfg.tolerance
    layers = class_layers(cfg)
    line_layers = {
        r: l
        for r, l in layers.items()
        if gkind(l) == "line" and not r.startswith("Structure")
    }
    point_layers = {
        r: l
        for r, l in layers.items()
        if gkind(l) == "point" and not r.startswith("Structure")
    }

    lines = {}  # (role, fid) -> QgsGeometry (mutable working copy)
    lidx = QgsSpatialIndex()
    lkeys = []
    for role, lyr in line_layers.items():
        for f in lyr.getFeatures():
            if f.hasGeometry():
                lines[(role, f.id())] = QgsGeometry(f.geometry())
                lkeys.append((role, f.id()))
                g = QgsGeometry(f.geometry())
                tmp = _feat(len(lkeys) - 1, g)
                lidx.addFeature(tmp)
    points = {}
    pidx = QgsSpatialIndex()
    pkeys = []
    for role, lyr in point_layers.items():
        for f in lyr.getFeatures():
            if f.hasGeometry():
                p = _point_of(f.geometry())
                if p is None:
                    continue
                points[(role, f.id())] = QgsPointXY(p)
                pkeys.append((role, f.id()))
                pidx.addFeature(
                    _feat(
                        len(pkeys) - 1, QgsGeometry.fromPointXY(QgsPointXY(p))
                    )
                )

    moved_points, moved_ends, inserted = {}, 0, 0
    changed_lines = set()

    def ends_near(pt, radius, skip=None):
        """Nearest line end (within radius) as (dist, QgsPointXY)."""
        best = None
        for i in lidx.intersects(_box(pt, radius)):
            key = lkeys[i]
            if key == skip:
                continue
            for part in _lines_of(lines[key]):
                for x, y in (part[0], part[-1]):
                    d = math.hypot(x - pt.x(), y - pt.y())
                    if d <= radius and (best is None or d < best[0]):
                        best = (d, QgsPointXY(x, y))
        return best

    def has_vertex_at(pt, skip=None):
        for i in lidx.intersects(_box(pt, tol)):
            key = lkeys[i]
            if key == skip:
                continue
            _v, _a, _b, _c, d2 = lines[key].closestVertex(pt)
            if d2**0.5 <= tol:
                return True
        return False

    def snap_on_line(pt, radius, skip=None):
        """Nearest location on another line: (dist, point, key, is_vertex)."""
        best = None
        for i in lidx.intersects(_box(pt, radius)):
            key = lkeys[i]
            if key == skip:
                continue
            g = lines[key]
            vpt, _a, _b, _c, vd2 = g.closestVertex(pt)
            sd2, spt, _after, _l = g.closestSegmentWithContext(pt)
            vd, sd = vd2**0.5, sd2**0.5
            # prefer an existing vertex when it is almost as close as the
            # segment
            cand = (
                (vd, QgsPointXY(vpt), key, True)
                if vd <= max(sd * 1.5, tol)
                else (sd, QgsPointXY(spt), key, False)
            )
            if cand[0] <= radius and (best is None or cand[0] < best[0]):
                best = cand
        return best

    def insert_vertex(key, pt):
        nonlocal inserted
        g = lines[key]
        _v, _a, _b, _c, d2 = g.closestVertex(pt)
        if d2**0.5 <= tol:
            return
        _sd2, _sp, after, _l = g.closestSegmentWithContext(pt)
        if g.insertVertex(pt.x(), pt.y(), after):
            changed_lines.add(key)
            inserted += 1

    # 1. points onto lines
    if snap_points:
        for key, pt in points.items():
            if has_vertex_at(pt):
                continue
            hit = snap_on_line(pt, gap)
            if hit is None:
                continue
            _d, target, lkey, is_vertex = hit
            if not is_vertex:
                insert_vertex(lkey, target)
            if target.distance(pt) > 0:
                moved_points[key] = target
                points[key] = target

    # 2. line ends onto points / ends / segments
    if snap_ends:
        for key in list(lines):
            g = lines[key]
            parts = _lines_of(g)
            for pi, part in enumerate(parts):
                for end_index in (0, len(part) - 1):
                    x, y = part[end_index]
                    pt = QgsPointXY(x, y)
                    if _connected(
                        pt, key, points, pidx, pkeys, lines, lidx, lkeys, tol
                    ):
                        continue
                    target = None
                    hp = _nearest_point(pt, points, pidx, pkeys, gap)
                    he = ends_near(pt, gap, skip=key)
                    cands = [h for h in (hp, he) if h is not None]
                    if cands:
                        target = min(cands, key=lambda h: h[0])[1]
                        if not has_vertex_at(target, skip=key):
                            hs = snap_on_line(target, tol, skip=key)
                            if hs is not None and not hs[3]:
                                insert_vertex(hs[2], target)
                    else:
                        hs = snap_on_line(pt, gap, skip=key)
                        if hs is not None:
                            target = hs[1]
                            if not hs[3]:
                                insert_vertex(hs[2], target)
                    if target is not None and target.distance(pt) > 0:
                        vid = _global_vertex(parts, pi, end_index)
                        if g.moveVertex(target.x(), target.y(), vid):
                            changed_lines.add(key)
                            moved_ends += 1
                            parts = _lines_of(g)

    # write back
    by_role = {}
    for key in changed_lines:
        by_role.setdefault(key[0], {})[key[1]] = lines[key]
    for role, lyr in line_layers.items():
        changes = by_role.get(role, {})
        if changes:
            loaded = _begin(lyr)
            _end(lyr, loaded, changes)
    by_role = {}
    for key, pt in moved_points.items():
        by_role.setdefault(key[0], {})[key[1]] = QgsGeometry.fromPointXY(pt)
    for role, lyr in point_layers.items():
        changes = by_role.get(role, {})
        if changes:
            loaded = _begin(lyr)
            _end(lyr, loaded, changes)
    return {
        "points_moved": len(moved_points),
        "line_ends_moved": moved_ends,
        "vertices_inserted": inserted,
    }


def _feat(fid, geom):
    from qgis.core import QgsFeature

    f = QgsFeature()
    f.setId(fid)
    f.setGeometry(geom)
    return f


def _box(pt, r):
    return QgsGeometry.fromPointXY(pt).buffer(max(r, 1e-9), 2).boundingBox()


def _global_vertex(parts, part_index, local):
    return sum(len(p) for p in parts[:part_index]) + local


def _nearest_point(pt, points, pidx, pkeys, radius):
    best = None
    for i in pidx.intersects(_box(pt, radius)):
        p = points[pkeys[i]]
        d = p.distance(pt)
        if d <= radius and (best is None or d < best[0]):
            best = (d, p)
    return best


def _connected(pt, key, points, pidx, pkeys, lines, lidx, lkeys, tol):
    for i in pidx.intersects(_box(pt, tol)):
        if points[pkeys[i]].distance(pt) <= tol:
            return True
    for i in lidx.intersects(_box(pt, tol)):
        other = lkeys[i]
        if other == key:
            continue
        _v, _a, _b, _c, d2 = lines[other].closestVertex(pt)
        if d2**0.5 <= tol:
            return True
    return False


# ---------------------------------------------------------------- associations
def associations(cfg, attach_distance=1.0, containment=True, attachment=True):
    """Create containment and structural attachment associations automatically.

    Existing connectivity associations are kept; containment / attachment are
    rebuilt.
    """
    layers = class_layers(cfg)
    polys = {r: l for r, l in layers.items() if gkind(l) == "polygon"}
    pts = {r: l for r, l in layers.items() if gkind(l) == "point"}
    keep = [a for a in cfg.associations if a[0] == "connectivity"]
    found = []
    if containment:
        for prole, play in polys.items():
            pcls = cfg.classes[prole]
            for pf in play.getFeatures():
                if not pf.hasGeometry():
                    continue
                pg = pf.geometry()
                for role, lyr in pts.items():
                    cls = cfg.classes[role]
                    for f in lyr.getFeatures(pg.boundingBox()):
                        if f.hasGeometry() and pg.contains(f.geometry()):
                            found.append(
                                ("containment", (pcls, pf.id()), (cls, f.id()))
                            )
    if attachment and "StructureJunction" in layers and "Device" in layers:
        s_lyr, d_lyr = layers["StructureJunction"], layers["Device"]
        s_cls, d_cls = cfg.classes["StructureJunction"], cfg.classes["Device"]
        idx = QgsSpatialIndex(s_lyr.getFeatures())
        geoms = {
            f.id(): f.geometry()
            for f in s_lyr.getFeatures()
            if f.hasGeometry()
        }
        for f in d_lyr.getFeatures():
            if not f.hasGeometry():
                continue
            p = _point_of(f.geometry())
            for sid in idx.nearestNeighbor(QgsPointXY(p), 1, attach_distance):
                if sid in geoms:
                    found.append(("attachment", (s_cls, sid), (d_cls, f.id())))
    S.save_associations(cfg.path, keep + found)
    return {
        "containment": sum(1 for a in found if a[0] == "containment"),
        "attachment": sum(1 for a in found if a[0] == "attachment"),
        "connectivity": len(keep),
    }
