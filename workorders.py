"""Work orders: repairs, maintenance, inspections, installations, outages.

Stored as the point layer un_work_orders inside the network file, so they are on the map,
can be edited with the normal QGIS tools / forms and travel with the data."""
import json
from datetime import date, datetime

from osgeo import ogr
from qgis.core import QgsEditorWidgetSetup, QgsFeature, QgsGeometry, QgsPointXY

from . import storage as S
from .qgis_io import get_layer, project_layer

TYPES = ["Repair", "Maintenance", "Inspection", "Installation", "Replacement", "Outage", "Survey", "Other"]
STATUSES = ["Open", "Assigned", "In progress", "On hold", "Completed", "Cancelled"]
PRIORITIES = ["Low", "Normal", "High", "Urgent"]
FIELDS = [("wo_id", ogr.OFTString, 30), ("title", ogr.OFTString, 200), ("wo_type", ogr.OFTString, 30),
          ("status", ogr.OFTString, 30), ("priority", ogr.OFTString, 20), ("assigned_to", ogr.OFTString, 100),
          ("created_date", ogr.OFTDate, 0), ("due_date", ogr.OFTDate, 0), ("completed_date", ogr.OFTDate, 0),
          ("description", ogr.OFTString, 1000), ("features", ogr.OFTString, 4000),
          ("customers_affected", ogr.OFTInteger, 0), ("cost", ogr.OFTReal, 0), ("notes", ogr.OFTString, 1000)]
LAYER = "un_work_orders"


@S._guard
def ensure_layer(path, srs_from):
    ds = S.open_ds(path, update=True)
    try:
        if ds.GetLayerByName(S._tn(ds, LAYER)) is None:
            ref = ds.GetLayerByName(srs_from)
            S._make_table(ds, LAYER, FIELDS, ogr.wkbPoint, ref.GetSpatialRef() if ref else None, S.detect_format(path))
    finally:
        ds = None


def layer(cfg):
    ensure_layer(cfg.path, cfg.classes.get("Line") or list(cfg.classes.values())[0])
    lyr = get_layer(cfg.path, LAYER)
    setup_form(lyr)
    return lyr


def setup_form(lyr):
    for name, values in (("wo_type", TYPES), ("status", STATUSES), ("priority", PRIORITIES)):
        i = lyr.fields().indexFromName(name)
        if i >= 0:
            lyr.setEditorWidgetSetup(i, QgsEditorWidgetSetup("ValueMap", {"map": [{v: v} for v in values]}))
    i = lyr.fields().indexFromName("features")
    if i >= 0:
        lyr.setEditorWidgetSetup(i, QgsEditorWidgetSetup("Hidden", {}))


def next_id(lyr):
    year = date.today().year
    n = 0
    for f in lyr.getFeatures():
        v = str(f["wo_id"] or "")
        if v.startswith("WO-%d-" % year):
            try:
                n = max(n, int(v.rsplit("-", 1)[1]))
            except ValueError:
                pass
    return "WO-%d-%04d" % (year, n + 1)


def create(cfg, values, keys, location=None):
    """Create a work order for the features `keys` [(class, fid)]; location = QgsPointXY or None."""
    lyr = layer(cfg)
    if location is None:
        location = _centre(cfg, keys)
    f = QgsFeature(lyr.fields())
    f.setAttributes([None] * len(lyr.fields()))
    data = {"wo_id": next_id(lyr), "status": "Open", "priority": "Normal", "wo_type": "Repair",
            "created_date": date.today().isoformat(), "features": json.dumps(["%s:%s" % k for k in sorted(keys)])}
    data.update({k: v for k, v in values.items() if v not in (None, "")})
    for k, v in data.items():
        i = lyr.fields().indexFromName(k)
        if i >= 0:
            f.setAttribute(i, v)
    if location is not None:
        f.setGeometry(QgsGeometry.fromPointXY(location))
    target = project_layer(cfg.path, LAYER) or lyr
    if target.isEditable():
        target.addFeature(f)
    else:
        target.dataProvider().addFeatures([f])
        target.triggerRepaint()
    return data["wo_id"]


def _centre(cfg, keys):
    from .qgis_io import class_layers
    from qgis.core import QgsFeatureRequest
    layers = class_layers(cfg)
    by = {}
    for cls, fid in keys:
        by.setdefault(cls, []).append(fid)
    box = None
    for role, lyr in layers.items():
        fids = by.get(cfg.classes[role])
        if not fids:
            continue
        for feat in lyr.getFeatures(QgsFeatureRequest().setFilterFids(fids)):
            if feat.hasGeometry():
                b = feat.geometry().boundingBox()
                box = b if box is None else (box.combineExtentWith(b) or box)
    return QgsPointXY(box.center()) if box is not None else None


def rows(cfg, status=None):
    lyr = layer(cfg)
    out = []
    for f in lyr.getFeatures():
        r = {"fid": f.id()}
        for fld in lyr.fields():
            v = f[fld.name()]
            r[fld.name()] = None if v is None or (hasattr(v, "isNull") and v.isNull()) else v
        if status and r.get("status") not in status:
            continue
        out.append(r)
    return sorted(out, key=lambda r: str(r.get("wo_id") or ""), reverse=True)


def feature_keys(row):
    try:
        items = json.loads(row.get("features") or "[]")
    except ValueError:
        return []
    out = []
    for it in items:
        cls, _s, fid = it.rpartition(":")
        if cls and fid.lstrip("-").isdigit():
            out.append((cls, int(fid)))
    return out


def update(cfg, fid, values):
    lyr = project_layer(cfg.path, LAYER) or layer(cfg)
    if values.get("status") == "Completed" and not values.get("completed_date"):
        values["completed_date"] = date.today().isoformat()
    changes = {lyr.fields().indexFromName(k): v for k, v in values.items() if lyr.fields().indexFromName(k) >= 0}
    if lyr.isEditable():
        for i, v in changes.items():
            lyr.changeAttributeValue(fid, i, v)
    else:
        lyr.dataProvider().changeAttributeValues({fid: changes})
        lyr.triggerRepaint()


def delete(cfg, fids):
    lyr = project_layer(cfg.path, LAYER) or layer(cfg)
    if lyr.isEditable():
        lyr.deleteFeatures(list(fids))
    else:
        lyr.dataProvider().deleteFeatures(list(fids))
        lyr.triggerRepaint()


def to_date(v):
    """date from QDate / QDateTime / string (File Geodatabase stores dates as date-times)."""
    if v is None:
        return None
    if hasattr(v, "date") and callable(v.date) and hasattr(v, "toPyDateTime"):
        v = v.date()
    if hasattr(v, "toPyDate"):
        return v.toPyDate() if v.isValid() else None
    try:
        return datetime.fromisoformat(str(v)[:10]).date()
    except ValueError:
        return None


def stats(cfg):
    out = {s: 0 for s in STATUSES}
    overdue = 0
    today = date.today()
    for r in rows(cfg):
        out[r.get("status") or "Open"] = out.get(r.get("status") or "Open", 0) + 1
        d = to_date(r.get("due_date"))
        if d is not None and r.get("status") not in ("Completed", "Cancelled") and d < today:
            overdue += 1
    out["Overdue"] = overdue
    return out
