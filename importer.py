"""Import existing data into the network classes.

The asset group / type of every imported feature comes either from a fixed
choice or from
the values of a field of the source layer. For a field, suggest_mapping()
proposes the
matching asset group / type for each distinct value by comparing the texts
(e.g. a value
"Gate Valve" -> group "System valve", type "Gate"). Fields with the same name
are copied.
"""

import difflib
import re

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsWkbTypes,
)

from .qgis_io import _int, class_layers, gkind, layer_source, project_layer

SKIP_FIELDS = {
    "fid",
    "objectid",
    "ogc_fid",
    "assetgroup",
    "assettype",
    "globalid",
    "shape_length",
    "shape_area",
    "shape__length",
    "shape__area",
}


def _words(text):
    return set(re.findall(r"[a-z\u0600-\u06ff0-9]+", str(text).lower()))


def _score(value, name):
    a, b = str(value).lower(), str(name).lower()
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    wa, wb = _words(a), _words(b)
    overlap = len(wa & wb) / max(len(wb), 1)
    return max(ratio, overlap)


def suggest_mapping(cfg, role, values):
    """{value: (ag, at, score)} best guess for each distinct value."""
    cls = cfg.classes[role]
    out = {}
    for v in values:
        best = (None, None, 0.0)
        for ag, gname in cfg.groups(cls):
            gs = _score(v, gname)
            for at, tname in cfg.types(cls, ag):
                ts = _score(v, tname)
                s = max(gs, ts, 0.6 * gs + 0.6 * ts)
                if s > best[2]:
                    best = (ag, at, s)
        out[v] = best if best[2] >= 0.35 else (None, None, best[2])
    return out


def distinct_values(layer, field, limit=500):
    i = layer.fields().indexFromName(field)
    if i < 0:
        return []
    vals = layer.uniqueValues(i, limit)
    return sorted(
        (v for v in vals if v is not None and str(v) != "NULL"), key=str
    )


def run(
    cfg,
    role,
    source,
    group_field=None,
    mapping=None,
    fixed=None,
    selected_only=False,
    lifecycle=3,
    feedback=None,
):
    """Append `source` features to the class of `role`.

    mapping: {value: (ag, at)} when group_field is set, else `fixed` = (ag,
    at).
    """
    target = class_layers(cfg)[role]
    tk, sk = gkind(target), gkind(source)
    if tk != sk:
        raise ValueError(
            "Geometry mismatch: the source has %s features, %s needs %s."
            % (sk, role, tk)
        )
    xform = None
    if source.crs() != target.crs():
        xform = QgsCoordinateTransform(
            source.crs(), target.crs(), QgsProject.instance()
        )
    tfields = target.fields()
    copy = []
    for sf in source.fields():
        name = sf.name()
        if name.lower() in SKIP_FIELDS:
            continue
        ti = tfields.indexFromName(name)
        if ti < 0:
            ti = next(
                (
                    i
                    for i, f in enumerate(tfields)
                    if f.name().lower() == name.lower()
                ),
                -1,
            )
        if ti >= 0:
            copy.append((source.fields().indexFromName(name), ti))
    gi = source.fields().indexFromName(group_field) if group_field else -1
    feats = (
        source.getSelectedFeatures() if selected_only else source.getFeatures()
    )
    total = (
        source.selectedFeatureCount()
        if selected_only
        else source.featureCount()
    )
    new, skipped, unmapped = [], 0, 0
    multi = QgsWkbTypes.isMultiType(target.wkbType())
    for n, f in enumerate(feats):
        if feedback is not None:
            if feedback.isCanceled():
                break
            feedback.setProgress(100.0 * n / max(total, 1))
        if not f.hasGeometry():
            skipped += 1
            continue
        if gi >= 0:
            ag_at = (mapping or {}).get(f.attribute(gi))
            if ag_at is None:
                ag_at = (mapping or {}).get(str(f.attribute(gi)))
        else:
            ag_at = fixed
        if not ag_at or ag_at[0] is None:
            unmapped += 1
            continue
        g = QgsGeometry(f.geometry())
        if xform is not None:
            g.transform(xform)
        if QgsWkbTypes.isCurvedType(g.wkbType()):
            g = QgsGeometry(g.constGet().segmentize())
        if g.constGet().is3D() or g.constGet().isMeasure():
            g.get().dropZValue()
            g.get().dropMValue()
        if multi and not g.isMultipart():
            g.convertToMultiType()
        elif not multi and g.isMultipart():
            parts = g.asGeometryCollection()
            if len(parts) != 1:
                skipped += 1
                continue
            g = parts[0]
        nf = QgsFeature(tfields)
        nf.setAttributes([None] * len(tfields))
        for si, ti in copy:
            nf.setAttribute(ti, f.attribute(si))
        nf.setAttribute(tfields.indexFromName("assetgroup"), int(ag_at[0]))
        nf.setAttribute(tfields.indexFromName("assettype"), int(ag_at[1] or 1))
        li = tfields.indexFromName("lifecyclestatus")
        if li >= 0 and nf.attribute(li) in (None, ""):
            nf.setAttribute(li, lifecycle)
        oi = tfields.indexFromName("operatingstatus")
        if oi >= 0 and _int(nf.attribute(oi)) is None:
            nf.setAttribute(oi, 1)
        nf.setGeometry(g)
        new.append(nf)
    path, name = layer_source(target)
    if project_layer(path, name) is target:
        if not target.isEditable():
            target.startEditing()
        target.beginEditCommand("Network Studio: import")
        target.addFeatures(new)
        target.endEditCommand()
        target.triggerRepaint()
    else:
        target.dataProvider().addFeatures(new)
    return {"imported": len(new), "unmapped": unmapped, "skipped": skipped}
