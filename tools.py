"""Daily tools: asset id generator, bulk field calculator, extra quality
checks."""

import re

from qgis.core import (
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsGeometry,
    QgsSpatialIndex,
)

from .qgis_io import (
    _int,
    _val,
    class_layers,
    gkind,
    layer_source,
    project_layer,
)


def _abbr(text, n=3):
    words = re.findall(r"[A-Za-z0-9]+", str(text or ""))
    if not words:
        return "X" * n
    if len(words) == 1:
        return words[0][:n].upper()
    return "".join(w[0] for w in words)[:n].upper().ljust(n, "X")


def _apply(lyr, updates):
    """updates {fid: {field index: value}} through the edit buffer when the
    layer is loaded."""
    if not updates:
        return 0
    path, name = layer_source(lyr)
    if len(updates) > 2000:
        # computed values for a whole network (risk, ids...): straight into the
        # data, one call
        lyr.dataProvider().changeAttributeValues(updates)
        if not lyr.isEditable():
            lyr.reload()
        lyr.triggerRepaint()
        return len(updates)
    if project_layer(path, name) is lyr:
        if not lyr.isEditable():
            lyr.startEditing()
        lyr.beginEditCommand("Network Studio")
        for fid, attrs in updates.items():
            for i, v in attrs.items():
                lyr.changeAttributeValue(fid, i, v)
        lyr.endEditCommand()
        lyr.triggerRepaint()
    else:
        lyr.dataProvider().changeAttributeValues(updates)
    return len(updates)


def generate_ids(
    cfg, role, pattern, only_empty=True, selected_only=False, digits=6
):
    """Pattern tokens: {NET} network prefix, {CLASS} class, {GROUP} / {TYPE}
    3-letter codes,
    {N} running number (per pattern prefix). Example: WTR-{GROUP}-{N} ->
    WTR-SYS-000123.
    """
    lyr = class_layers(cfg)[role]
    cls = cfg.classes[role]
    i = lyr.fields().indexFromName("assetid")
    if i < 0:
        raise ValueError("The class has no assetid field.")
    net = _abbr(cfg.tpl["prefix"])
    counters = {}
    existing = set()
    feats = list(lyr.getFeatures())
    for f in feats:
        v = _val(f, "assetid")
        if v:
            existing.add(str(v))
            m = re.match(r"^(.*?)(\d+)$", str(v))
            if m:
                counters[m.group(1)] = max(
                    counters.get(m.group(1), 0), int(m.group(2))
                )
    targets = (
        lyr.selectedFeatureIds() if selected_only else [f.id() for f in feats]
    )
    tset = set(targets)
    updates = {}
    for f in feats:
        if f.id() not in tset:
            continue
        if only_empty and _val(f, "assetid"):
            continue
        ag, at = _int(_val(f, "assetgroup")), _int(_val(f, "assettype"))
        base = (
            pattern.replace("{NET}", net)
            .replace("{CLASS}", _abbr(cls))
            .replace("{GROUP}", _abbr(cfg.label(cls, ag)))
            .replace("{TYPE}", _abbr(cfg.type_label(cls, ag, at)))
        )
        prefix = base.split("{N}")[0]
        n = counters.get(prefix, 0)
        while True:
            n += 1
            new = base.replace("{N}", str(n).zfill(digits))
            if new not in existing:
                break
        counters[prefix] = n
        existing.add(new)
        updates[f.id()] = {i: new}
    return _apply(lyr, updates)


def calculate(
    cfg, role, field, expression, selected_only=False, only_empty=False
):
    lyr = class_layers(cfg)[role]
    i = lyr.fields().indexFromName(field)
    if i < 0:
        raise ValueError("Field %s not found." % field)
    exp = QgsExpression(expression)
    if exp.hasParserError():
        raise ValueError("Expression error: %s" % exp.parserErrorString())
    ctx = QgsExpressionContext()
    ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
    exp.prepare(ctx)
    feats = lyr.getSelectedFeatures() if selected_only else lyr.getFeatures()
    updates = {}
    for f in feats:
        if only_empty and _val(f, field) not in (None, ""):
            continue
        ctx.setFeature(f)
        v = exp.evaluate(ctx)
        if exp.hasEvalError():
            raise ValueError("Expression error: %s" % exp.evalErrorString())
        updates[f.id()] = {i: v}
    return _apply(lyr, updates)


def extra_checks(cfg, min_length=0.5, check_crossings=True):
    """E17 duplicate asset id, E18 overlapping lines, E19 lines crossing
    without a junction,
    E20 very short line."""
    tol = cfg.tolerance
    issues = []

    def add(code, sev, msg, pt, key):
        issues.append(
            {
                "code": code,
                "severity": sev,
                "message": msg,
                "x": pt.x(),
                "y": pt.y(),
                "key": key,
                "geom": "point",
            }
        )

    seen = {}
    for role, lyr in class_layers(cfg).items():
        cls = cfg.classes[role]
        for f in lyr.getFeatures():
            v = _val(f, "assetid")
            if v and f.hasGeometry():
                seen.setdefault(str(v), []).append(
                    (cls, f.id(), f.geometry().pointOnSurface().asPoint())
                )
    for v, items in seen.items():
        if len(items) > 1:
            for cls, fid, pt in items:
                add(
                    "E17",
                    "error",
                    "Asset id %s is used %d times" % (v, len(items)),
                    pt,
                    (cls, fid),
                )
    for role, lyr in class_layers(cfg).items():
        if gkind(lyr) != "line":
            continue
        cls = cfg.classes[role]
        geoms = {
            f.id(): QgsGeometry(f.geometry())
            for f in lyr.getFeatures()
            if f.hasGeometry()
        }
        idx = QgsSpatialIndex()
        from qgis.core import QgsFeature

        for fid, g in geoms.items():
            q = QgsFeature()
            q.setId(fid)
            q.setGeometry(g)
            idx.addFeature(q)
            if g.length() < min_length:
                add(
                    "E20",
                    "warning",
                    "Very short line (%.3f)" % g.length(),
                    g.interpolate(0).asPoint(),
                    (cls, fid),
                )
        done = set()
        for fid, g in geoms.items():
            for oid in idx.intersects(g.boundingBox()):
                if oid == fid or (oid, fid) in done:
                    continue
                done.add((fid, oid))
                og = geoms[oid]
                if not g.intersects(og):
                    continue
                inter = g.intersection(og)
                if inter.isEmpty():
                    continue
                if inter.length() > tol:
                    add(
                        "E18",
                        "error",
                        "Overlaps line %s:%s over %.2f"
                        % (cls, oid, inter.length()),
                        inter.interpolate(0).asPoint(),
                        (cls, fid),
                    )
                elif check_crossings:
                    pts = (
                        inter.asMultiPoint()
                        if inter.isMultipart()
                        else [inter.asPoint()]
                    )
                    for p in pts:
                        on_v1 = g.closestVertex(p)[4] ** 0.5 <= tol
                        on_v2 = og.closestVertex(p)[4] ** 0.5 <= tol
                        if not (on_v1 and on_v2):
                            add(
                                "E19",
                                "warning",
                                "Crosses line %s:%s without a junction"
                                % (cls, oid),
                                p,
                                (cls, fid),
                            )
    return issues
