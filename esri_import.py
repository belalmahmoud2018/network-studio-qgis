"""Import from ArcGIS.

Network Analyst street data (TomTom / HERE / StreetMap style: ONEWAY FT/TF/N, F_ELEV/T_ELEV,
FRC or FUNC_CLASS, KPH, MINUTES) -> Road network.

Utility Network classes (ASSETGROUP / ASSETTYPE / GLOBALID fields) -> the class of the same
role; asset group / type codes are kept as they are, and coded value domains found in the
geodatabase name the asset types. Associations exported with the ArcGIS tool
'Export Associations' (CSV) are matched through GLOBALID."""
import csv
import os

from osgeo import gdal, ogr
from qgis.core import QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer, QgsWkbTypes

from . import storage as S
from .qgis_io import class_layers, gkind, layer_source, project_layer

FRC_GROUPS = {0: 1, 1: 1, 2: 2, 3: 2, 4: 3, 5: 3, 6: 4, 7: 4, 8: 5}   # FRC -> Road asset group
UN_ROLE_HINTS = (("structurejunction", "StructureJunction"), ("structureline", "StructureLine"),
                 ("structureboundary", "StructureBoundary"), ("device", "Device"), ("junction", "Junction"),
                 ("assembly", "Assembly"), ("line", "Line"))
ASSOC_TYPES = {1: "connectivity", 2: "containment", 3: "attachment", 4: "connectivity", 5: "connectivity",
               6: "connectivity"}


def _fields(lyr):
    d = lyr.GetLayerDefn()
    return [d.GetFieldDefn(i).GetName() for i in range(d.GetFieldCount())]


def inspect(path):
    """[(layer name, kind, feature count, geometry)] kind: streets / utility / other."""
    ds = gdal.OpenEx(path, gdal.OF_VECTOR)
    if ds is None:
        raise S.StoreError("Could not open %s" % path)
    out = []
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayer(i)
        names = {f.upper() for f in _fields(lyr)}
        geom = ogr.GeometryTypeToName(lyr.GetGeomType())
        if lyr.GetName().upper().startswith(("N_1_", "ND_")) or geom == "None":
            continue
        if "ASSETGROUP" in names and "ASSETTYPE" in names:
            kind = "utility"
        elif "ONEWAY" in names and ({"FRC", "FUNC_CLASS", "FUNCCLASS"} & names) and "Line" in geom:
            kind = "streets"
        else:
            kind = "other"
        out.append((lyr.GetName(), kind, lyr.GetFeatureCount(), geom))
    ds = None
    return out


def _coded_domains(path):
    """{domain name: {code: label}} from the geodatabase (GDAL field domains)."""
    out = {}
    ds = gdal.OpenEx(path, gdal.OF_VECTOR)
    try:
        for name in (ds.GetFieldDomainNames() or []) if hasattr(ds, "GetFieldDomainNames") else []:
            dom = ds.GetFieldDomain(name)
            if dom is not None and dom.GetDomainType() == ogr.OFDT_CODED:
                out[name] = {int(k) if str(k).lstrip("-").isdigit() else k: v for k, v in dom.GetEnumeration().items()}
    finally:
        ds = None
    return out


def _target(cfg, role):
    lyr = class_layers(cfg)[role]
    return lyr


def _write(target, feats):
    path, name = layer_source(target)
    if project_layer(path, name) is target:
        if not target.isEditable():
            target.startEditing()
        target.addFeatures(feats)
        target.triggerRepaint()
    else:
        ok, _out = target.dataProvider().addFeatures(feats)
        if not ok:
            raise S.StoreError("Writing failed: %s" % target.dataProvider().lastError())


def import_streets(cfg, path, layer_name, feedback=None):
    """Network Analyst streets -> Line class of a Road network."""
    if cfg.flow != "undirected":
        raise ValueError("Street data goes into a Road network (create one with network type Roads).")
    src = QgsVectorLayer("%s|layername=%s" % (path, layer_name), layer_name, "ogr")
    if not src.isValid():
        raise S.StoreError("Could not open %s" % layer_name)
    target = _target(cfg, "Line")
    xf = QgsCoordinateTransform(src.crs(), target.crs(), QgsProject.instance()) if src.crs() != target.crs() else None
    names = {f.name().upper(): f.name() for f in src.fields()}
    frc = names.get("FRC") or names.get("FUNC_CLASS") or names.get("FUNCCLASS")
    tf = target.fields()
    feats, total, n = [], src.featureCount(), 0
    multi = QgsWkbTypes.isMultiType(target.wkbType())
    for f in src.getFeatures():
        n += 1
        if feedback and n % 2000 == 0:
            feedback(n, total)
        if not f.hasGeometry():
            continue
        g = QgsGeometry(f.geometry())
        if xf is not None:
            g.transform(xf)
        if multi and not g.isMultipart():
            g.convertToMultiType()
        try:
            cls = int(f[frc]) if frc and f[frc] is not None else 6
        except (TypeError, ValueError):
            cls = 6
        ow = str(f[names["ONEWAY"]] or "").strip().upper()
        nf = QgsFeature(tf)
        nf.setAttributes([None] * len(tf))
        vals = {"assetgroup": FRC_GROUPS.get(cls, 4), "assettype": 1, "lifecyclestatus": 4 if ow == "N" else 3,
                "oneway": 1 if ow == "FT" else 2 if ow == "TF" else 0, "assetid": str(f[names["ID"]]) if "ID" in names
                and f[names["ID"]] is not None else None}
        for ours, theirs in (("roadname", "NAME"), ("speed", "KPH"), ("lanes", "LANES"), ("f_elev", "F_ELEV"),
                             ("t_elev", "T_ELEV"), ("minutes", "MINUTES")):
            if theirs in names:
                v = f[names[theirs]]
                vals[ours] = None if v is None or (isinstance(v, str) and not v.strip()) else v
        vals["measuredlength"] = round(g.length(), 2)
        for k, v in vals.items():
            i = tf.indexFromName(k)
            if i >= 0:
                nf.setAttribute(i, v)
        nf.setGeometry(g)
        feats.append(nf)
    _write(target, feats)
    return {"imported": len(feats), "closed to traffic (ONEWAY=N, out of service)": sum(1 for x in feats if x["lifecyclestatus"] == 4)}


def import_utility(cfg, path, layer_name, role):
    """An ArcGIS utility network class -> our class of the same role, codes kept, model extended."""
    src = QgsVectorLayer("%s|layername=%s" % (path, layer_name), layer_name, "ogr")
    if not src.isValid():
        raise S.StoreError("Could not open %s" % layer_name)
    target = _target(cfg, role)
    if gkind(src) != gkind(target):
        raise ValueError("%s has %s geometry but %s needs %s." % (layer_name, gkind(src), role, gkind(target)))
    cls = cfg.classes[role]
    up = {f.name().upper(): f.name() for f in src.fields()}
    ag_f, at_f, gid_f = up.get("ASSETGROUP"), up.get("ASSETTYPE"), up.get("GLOBALID")
    domains = _coded_domains(path)
    type_domain = {}
    ds = gdal.OpenEx(path, gdal.OF_VECTOR)
    try:
        d = ds.GetLayerByName(layer_name).GetLayerDefn()
        for i in range(d.GetFieldCount()):
            fd = d.GetFieldDefn(i)
            if fd.GetName().upper() == "ASSETTYPE" and fd.GetDomainName():
                type_domain = domains.get(fd.GetDomainName(), {})
            if fd.GetName().upper() == "ASSETGROUP" and fd.GetDomainName():
                type_domain.setdefault("__groups__", domains.get(fd.GetDomainName(), {}))
    finally:
        ds = None
    xf = QgsCoordinateTransform(src.crs(), target.crs(), QgsProject.instance()) if src.crs() != target.crs() else None
    tf = target.fields()
    lower = {f.name().lower(): i for i, f in enumerate(tf)}
    copy = [(sf.name(), lower[sf.name().lower()]) for sf in src.fields()
            if sf.name().lower() in lower and sf.name().lower() not in ("objectid", "fid", "assetgroup", "assettype")]
    pairs, feats = set(), []
    multi = QgsWkbTypes.isMultiType(target.wkbType())
    for f in src.getFeatures():
        if not f.hasGeometry():
            continue
        g = QgsGeometry(f.geometry())
        if xf is not None:
            g.transform(xf)
        if QgsWkbTypes.isCurvedType(g.wkbType()):
            g = QgsGeometry(g.constGet().segmentize())
        if g.constGet().is3D():
            g.get().dropZValue()
        if g.constGet().isMeasure():
            g.get().dropMValue()
        if multi and not g.isMultipart():
            g.convertToMultiType()
        nf = QgsFeature(tf)
        nf.setAttributes([None] * len(tf))
        for sname, ti in copy:
            nf.setAttribute(ti, f[sname])
        ag = int(f[ag_f]) if f[ag_f] is not None else None
        at = int(f[at_f]) if f[at_f] is not None else None
        nf.setAttribute(tf.indexFromName("assetgroup"), ag)
        nf.setAttribute(tf.indexFromName("assettype"), at)
        if gid_f and tf.indexFromName("globalid") >= 0:
            nf.setAttribute(tf.indexFromName("globalid"), str(f[gid_f]).strip("{}").upper() if f[gid_f] else None)
        nf.setGeometry(g)
        feats.append(nf)
        if ag is not None:
            pairs.add((ag, at))
    _write(target, feats)
    # extend the model with the codes found
    have = {(r["class_name"], r["ag_code"], r["at_code"]) for r in cfg.assets}
    groups = type_domain.get("__groups__", {})
    assets = list(cfg.assets)
    added = 0
    for ag, at in sorted(pairs, key=lambda x: (x[0], x[1] or 0)):
        if (cls, ag, at) in have:
            continue
        assets.append({"class_name": cls, "role": role, "ag_code": ag,
                       "ag_name": groups.get(ag) or cfg.group_names.get((cls, ag)) or "Group %d" % ag,
                       "at_code": at if at is not None else 0,
                       "at_name": type_domain.get(at) or "Type %s" % at, "categories": "", "tier": None})
        added += 1
    if added:
        S.save_model(cfg.path, cfg.classes, assets, cfg.terminal_rows, cfg.flow)
    return {"imported": len(feats), "new asset group / type pairs": added}


def guess_role(layer_name, geom):
    low = layer_name.lower().replace("_", "")
    for hint, role in UN_ROLE_HINTS:
        if hint in low:
            return role
    return {"Point": "Device", "Line": "Line", "Polygon": "Assembly"}.get(
        "Point" if "Point" in geom else "Line" if "Line" in geom else "Polygon", "Device")


def import_associations(cfg, csv_path):
    """CSV from ArcGIS 'Export Associations' -> associations (matched by GLOBALID)."""
    gid = {}
    for role, lyr in class_layers(cfg).items():
        if lyr.fields().indexFromName("globalid") < 0:
            continue
        for f in lyr.getFeatures():
            if f["globalid"]:
                gid[str(f["globalid"]).strip("{}").upper()] = (cfg.classes[role], f.id())
    if not gid:
        raise ValueError("No GLOBALID values in the network: import the ArcGIS classes first.")
    added, missing, out = 0, 0, list(cfg.associations)
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            u = {k.upper().strip(): (v or "").strip() for k, v in r.items() if k}
            a = gid.get(u.get("FROMGLOBALID", "").strip("{}").upper())
            b = gid.get(u.get("TOGLOBALID", "").strip("{}").upper())
            if not a or not b:
                missing += 1
                continue
            t = u.get("ASSOCIATIONTYPE", "1")
            kind = ASSOC_TYPES.get(int(t), "connectivity") if t.isdigit() else t.lower()
            out.append((kind if kind in ("connectivity", "containment", "attachment") else "connectivity", a, b))
            added += 1
    S.save_associations(cfg.path, out)
    return {"associations added": added, "not matched": missing}


def gdb_from_zip_or_path(path):
    return path if os.path.isdir(path) else path
