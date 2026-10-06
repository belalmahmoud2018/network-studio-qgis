"""QGIS side of the plugin: layers, forms, styles, reading the network, writing results."""
import os
import random
from datetime import datetime

from qgis.core import (
    Qgis, QgsCategorizedSymbolRenderer, QgsDefaultValue, QgsEditorWidgetSetup, QgsFeature,
    QgsFeatureRequest, QgsGeometry, QgsMarkerSymbol, QgsMessageLog,
    QgsPointXY, QgsProject, QgsRendererCategory, QgsSymbol, QgsVectorLayer, QgsWkbTypes,
)
from qgis.PyQt.QtGui import QColor

from . import storage as S
from . import templates as T
from .engine import Network

TAG = "Network Studio"
SYSTEM_GROUP = "System tables"


def log(msg, warning=False):
    level = getattr(Qgis, "Warning" if warning else "Info", None)
    if level is None:
        level = getattr(Qgis.MessageLevel, "Warning" if warning else "Info")
    QgsMessageLog.logMessage(str(msg), TAG, level)


def gkind(layer_or_geom):
    v = layer_or_geom.geometryType() if hasattr(layer_or_geom, "geometryType") else layer_or_geom.type()
    try:
        v = int(v)
    except TypeError:
        v = v.value
    return {0: "point", 1: "line", 2: "polygon"}.get(v, "none")


def _norm(path):
    path = S.split_domain(path)[0]
    if S.is_pg(path):
        p = S.pg_parts(path)
        return "PG:" + "|".join("%s=%s" % (k, p.get(k, "")) for k in ("host", "port", "dbname", "active_schema"))
    return os.path.normcase(os.path.abspath(path.rstrip("/\\")))


def layer_source(layer):
    src = layer.source().split("|")
    path = src[0]
    name = None
    for part in src[1:]:
        if part.lower().startswith("layername="):
            name = part.split("=", 1)[1]
    return path, name


def project_layer(path, name):
    """A layer of the current project pointing at `path|layername=name`, or None."""
    name = S.table_name(path, name)
    for lyr in QgsProject.instance().mapLayers().values():
        if not isinstance(lyr, QgsVectorLayer) or lyr.providerType() not in ("ogr", "spatialite"):
            continue
        p, n = layer_source(lyr)
        if n and n.lower().split(".")[-1] == name.lower() and _norm(p) == _norm(path):
            return lyr
    return None


_CACHE = {}


def clear_cache(path=None):
    """Forget the layers opened outside the project (after schema changes or file replacement)."""
    for k in list(_CACHE):
        if path is None or k[0] == _norm(path):
            _CACHE.pop(k, None)


def get_layer(path, name, add=False):
    """Project layer when loaded, otherwise one cached layer per table (a single file handle each)."""
    lyr = project_layer(path, name)
    if lyr is not None:
        return lyr
    key = (_norm(path), S.table_name(path, name).lower())
    lyr = _CACHE.get(key)
    try:
        ok = lyr is not None and lyr.isValid()
    except RuntimeError:
        ok = False
    if not ok:
        lyr = QgsVectorLayer(S.uri(path, name), name, "ogr")
        if not lyr.isValid():
            raise S.StoreError("Could not open the layer %s." % name)
        _CACHE[key] = lyr
    if add:
        _CACHE.pop(key, None)
        QgsProject.instance().addMapLayer(lyr)
    return lyr


def class_layers(cfg):
    """{role: QgsVectorLayer} for every class of the network (project layer if loaded)."""
    out = {}
    for role, cls in cfg.classes.items():
        try:
            out[role] = get_layer(cfg.path, cls)
        except S.StoreError:
            log("Layer %s is missing" % cls, True)
    return out


# ------------------------------------------------------------------ loading
def load_network(cfg, iface=None):
    """Add the classes, the error layers and the asset type lookup to the project."""
    clear_cache(cfg.path)
    root = QgsProject.instance().layerTreeRoot()
    title = cfg.settings.get("name") or cfg.tpl["label"]
    group = root.findGroup(title) or root.insertGroup(0, title)
    order = ["Device", "Junction", "Line", "Assembly", "StructureJunction", "StructureLine",
             "StructureBoundary"]
    lookup = get_layer(cfg.path, "un_asset_types")
    if project_layer(cfg.path, "un_asset_types") is None:
        QgsProject.instance().addMapLayer(lookup, False)
        sysg = group.findGroup(SYSTEM_GROUP) or group.addGroup(SYSTEM_GROUP)
        sysg.addLayer(lookup)
        sysg.setExpanded(False)
        sysg.setItemVisibilityChecked(False)
    for name in ("un_errors_point", "un_errors_line"):
        if project_layer(cfg.path, name) is None:
            lyr = get_layer(cfg.path, name)
            lyr.setName("Errors (%s)" % ("points" if name.endswith("point") else "lines"))
            _style_errors(lyr)
            QgsProject.instance().addMapLayer(lyr, False)
            group.insertLayer(0, lyr)
    if project_layer(cfg.path, "un_work_orders") is None:
        try:
            from . import workorders
            wol = workorders.layer(cfg)
            wol.setName("Work orders")
            sym = QgsMarkerSymbol.createSimple({"name": "diamond", "color": "#ef9f27", "size": "4.5",
                                                 "outline_color": "#633806"})
            wol.renderer().setSymbol(sym)
            QgsProject.instance().addMapLayer(wol, False)
            group.insertLayer(0, wol)
        except Exception as e:
            log("Work orders layer: %s" % e, True)
    for role in order:
        cls = cfg.classes.get(role)
        if not cls or project_layer(cfg.path, cls) is not None:
            continue
        lyr = get_layer(cfg.path, cls)
        setup_layer(cfg, role, cls, lyr, lookup)
        QgsProject.instance().addMapLayer(lyr, False)
        group.addLayer(lyr)
    return group


def refresh_forms(cfg):
    """Re-apply forms and styles to the network layers loaded in the project."""
    lookup = project_layer(cfg.path, "un_asset_types")
    if lookup is not None:
        lookup.dataProvider().reloadData()
    for role, cls in cfg.classes.items():
        lyr = project_layer(cfg.path, cls)
        if lyr is not None:
            setup_layer(cfg, role, cls, lyr, lookup)
            lyr.triggerRepaint()


def setup_layer(cfg, role, cls, lyr, lookup=None):
    fields = lyr.fields()

    def idx(name):
        return fields.indexFromName(name)

    groups = cfg.groups(cls)
    if idx("assetgroup") >= 0:
        lyr.setEditorWidgetSetup(idx("assetgroup"), QgsEditorWidgetSetup(
            "ValueMap", {"map": [{name: code} for code, name in groups]}))
        lyr.setDefaultValueDefinition(idx("assetgroup"), QgsDefaultValue(str(groups[0][0]) if groups else "1"))
        lyr.setFieldAlias(idx("assetgroup"), "Asset group")
    if idx("assettype") >= 0 and lookup is not None:
        lyr.setEditorWidgetSetup(idx("assettype"), QgsEditorWidgetSetup("ValueRelation", {
            "Layer": lookup.id(), "LayerName": lookup.name(), "Key": "at_code", "Value": "at_name",
            "AllowNull": False, "OrderByValue": False,
            "FilterExpression": "\"class_name\" = '%s' AND \"ag_code\" = current_value('assetgroup')" % cls,
        }))
        lyr.setDefaultValueDefinition(idx("assettype"), QgsDefaultValue("1"))
        lyr.setFieldAlias(idx("assettype"), "Asset type")
    maps = {"lifecyclestatus": (T.LIFECYCLE, "3"), "operatingstatus": (T.OPERATING, "1"),
            "flowdirection": (T.FLOW_DIRECTION, "1"), "oneway": (T.ONEWAY, "0")}
    for fname, (values, default) in maps.items():
        if idx(fname) >= 0:
            lyr.setEditorWidgetSetup(idx(fname), QgsEditorWidgetSetup(
                "ValueMap", {"map": [{label: code} for code, label in values]}))
            lyr.setDefaultValueDefinition(idx(fname), QgsDefaultValue(default))
    tracking = (("created_user", "@user_account_name", False), ("created_date", "now()", False),
                ("last_edited_user", "@user_account_name", True), ("last_edited_date", "now()", True))
    for fname, expr, on_update in tracking:
        if idx(fname) >= 0:
            lyr.setDefaultValueDefinition(idx(fname), QgsDefaultValue(expr, on_update))
            lyr.setEditorWidgetSetup(idx(fname), QgsEditorWidgetSetup("Hidden", {}))
    if idx("measuredlength") >= 0:
        lyr.setDefaultValueDefinition(idx("measuredlength"), QgsDefaultValue("round($length, 3)", True))
    _style_classes(lyr, groups, role)


def _style_classes(lyr, groups, role):
    kind = gkind(lyr)
    cats = []
    rnd = random.Random(hash(lyr.name()) & 0xFFFF)
    for code, name in groups:
        sym = QgsSymbol.defaultSymbol(lyr.geometryType())
        sym.setColor(QColor.fromHsv(rnd.randint(0, 359), 200, 200))
        if kind == "line":
            low = name.lower()
            sym.setWidth(0.35 if any(w in low for w in ("service", "lateral", "drop", "access")) else
                         0.9 if any(w in low for w in ("transmission", "feeder", "trunk", "highway", "mv ")) else
                         0.6 if "Structure" not in role else 0.4)
        elif kind == "point":
            try:
                from . import symbols
                sym = symbols.marker(name, role, sym.color(), 4.2 if role == "Device" else
                                     2.6 if role == "Junction" else 3.2)
            except Exception as e:
                log("Symbol: %s" % e, True)
                sym.setSize(2.6 if role == "Device" else 1.8)
        elif kind == "polygon":
            sym.setOpacity(0.35)
        cats.append(QgsRendererCategory(code, sym, name))
    if cats:
        lyr.setRenderer(QgsCategorizedSymbolRenderer("assetgroup", cats))


def _style_errors(lyr):
    if gkind(lyr) == "point":
        sym = QgsMarkerSymbol.createSimple({"name": "cross2", "color": "#e31a1c", "size": "4",
                                             "outline_color": "#e31a1c", "outline_width": "0.6"})
    else:
        sym = QgsSymbol.defaultSymbol(lyr.geometryType())
        sym.setColor(QColor("#e31a1c"))
        sym.setWidth(1.4)
        sym.setOpacity(0.7)
    lyr.renderer().setSymbol(sym)


# ------------------------------------------------------------------ reading
def _lines_of(geom):
    g = QgsGeometry(geom)
    if QgsWkbTypes.isCurvedType(g.wkbType()):
        g = QgsGeometry(g.constGet().segmentize())
    if g.isMultipart():
        parts = g.asMultiPolyline()
    else:
        parts = [g.asPolyline()]
    return [[(p.x(), p.y()) for p in part] for part in parts if part]


def _point_of(geom):
    if geom.isMultipart():
        pts = geom.asMultiPoint()
        return pts[0] if pts else None
    return geom.asPoint()


def _val(f, name):
    i = f.fields().indexFromName(name)
    if i < 0:
        return None
    v = f.attribute(i)
    try:
        return None if v is None or (hasattr(v, "isNull") and v.isNull()) else v
    except Exception:
        return v


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def read_network(cfg, layers=None, feedback=None):
    """Build an engine Network from the current content of the class layers (edits included)."""
    layers = layers or class_layers(cfg)
    size_field = cfg.settings.get("size_field") or cfg.tpl["size_field"][0]
    points, lines = [], []
    for role, lyr in layers.items():
        kind = gkind(lyr)
        if kind == "polygon":
            continue
        cls = cfg.classes[role]
        has_levels = lyr.fields().indexFromName("f_elev") >= 0
        attr_fields = [(name, by_cls[cls]) for name, by_cls in cfg.network_attributes.items()
                       if cls in by_cls and lyr.fields().indexFromName(by_cls[cls]) >= 0]
        for f in lyr.getFeatures():
            g = f.geometry()
            if g is None or g.isNull():
                continue
            ag, at = _int(_val(f, "assetgroup")), _int(_val(f, "assettype"))
            attrs = {name: _val(f, field) for name, field in attr_fields}
            if kind == "point":
                p = _point_of(g)
                if p is None:
                    continue
                points.append({"key": (cls, f.id()), "x": p.x(), "y": p.y(), "ag": ag, "at": at,
                               "closed": _int(_val(f, "operatingstatus")) == 0,
                               "assetid": _val(f, "assetid"), "lifecycle": _int(_val(f, "lifecyclestatus")),
                               "attrs": attrs})
            else:
                lines.append({"key": (cls, f.id()), "parts": _lines_of(g), "ag": ag, "at": at,
                              "reverse": _int(_val(f, "flowdirection")) == 2,
                              "oneway": _int(_val(f, "oneway")) or 0,
                              "lifecycle": _int(_val(f, "lifecyclestatus")), "size": _num(_val(f, size_field)),
                              "attrs": attrs, "levels": (_int(_val(f, "f_elev")), _int(_val(f, "t_elev")))
                              if has_levels else None})
    return Network(points, lines, cfg.tolerance, cats=cfg.cats, tiers=cfg.tiers, flow=cfg.flow,
                   associations=cfg.associations, gap=cfg.gap, terminals=cfg.terminals,
                   controllers=cfg.controllers, tier_settings=cfg.tier_settings)


# ------------------------------------------------------------------ results
def select_result(cfg, res, layers=None, zoom_canvas=None):
    layers = layers or class_layers(cfg)
    by_cls = {}
    for cls, fid in res.lines | res.points:
        by_cls.setdefault(cls, []).append(fid)
    extent = None
    for role, lyr in layers.items():
        fids = by_cls.get(cfg.classes[role], [])
        if project_layer(cfg.path, cfg.classes[role]) is None:
            continue
        lyr.selectByIds(fids)
        if fids:
            box = lyr.boundingBoxOfSelected()
            if extent is None:
                extent = box
            else:
                extent.combineExtentWith(box)
    if zoom_canvas is not None and extent is not None and not extent.isEmpty():
        extent.scale(1.15)
        zoom_canvas.setExtent(extent)
        zoom_canvas.refresh()
    return extent


def result_layers(cfg, res, title, layers=None):
    """Copy the traced features into two memory layers (lines / points)."""
    layers = layers or class_layers(cfg)
    by_cls = {}
    for cls, fid in res.lines | res.points:
        by_cls.setdefault(cls, []).append(fid)
    crs = next(iter(layers.values())).crs().authid() if layers else ""
    made = []
    for kind, geom in (("line", "MultiLineString"), ("point", "Point")):
        mem = QgsVectorLayer("%s?crs=%s&field=class_name:string&field=fid:long&field=assetgroup:string"
                             % (geom, crs), "%s (%ss)" % (title, kind), "memory")
        feats = []
        for role, lyr in layers.items():
            cls = cfg.classes[role]
            if gkind(lyr) != kind or cls not in by_cls:
                continue
            for f in lyr.getFeatures(QgsFeatureRequest().setFilterFids(by_cls[cls])):
                nf = QgsFeature(mem.fields())
                nf.setGeometry(f.geometry())
                nf.setAttributes([cls, f.id(), cfg.label(cls, _int(_val(f, "assetgroup")))])
                feats.append(nf)
        if feats:
            mem.dataProvider().addFeatures(feats)
            sym = mem.renderer().symbol()
            sym.setColor(QColor("#ff9f1c"))
            if kind == "line":
                sym.setWidth(1.6)
            else:
                sym.setSize(4)
            QgsProject.instance().addMapLayer(mem)
            made.append(mem)
    return made


def write_errors(cfg, issues, areas=None):
    """Replace the content of the error layers with `issues`.

    areas: list of QgsRectangle -> only errors inside these areas are replaced (dirty areas).
    Errors marked as exceptions are left out."""
    issues = [it for it in issues if (it["code"], it["key"][0], it["key"][1]) not in cfg.exceptions]
    if areas:
        def inside(x, y):
            return any(r.contains(QgsPointXY(x, y)) for r in areas)
        issues = [it for it in issues if inside(it["x"], it["y"])]
    now = datetime.now().isoformat(timespec="seconds")
    layers = class_layers(cfg)
    by_key = {cfg.classes[r]: l for r, l in layers.items()}
    counts = {}
    for name in ("un_errors_point", "un_errors_line"):
        lyr = get_layer(cfg.path, name)
        prov = lyr.dataProvider()
        old = list(lyr.getFeatures())
        if areas:
            old = [f for f in old if f.hasGeometry() and any(
                r.intersects(f.geometry().boundingBox()) for r in areas)]
        prov.deleteFeatures([f.id() for f in old])
        feats = []
        want = "line" if name.endswith("line") else "point"
        for it in issues:
            if it["geom"] != want:
                continue
            f = QgsFeature(lyr.fields())
            if want == "point":
                f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(it["x"], it["y"])))
            else:
                src = by_key.get(it["key"][0])
                feat = next(src.getFeatures(QgsFeatureRequest(it["key"][1])), None) if src else None
                if feat is None or not feat.hasGeometry():
                    continue
                f.setGeometry(feat.geometry())
            f.setAttributes([None] * len(lyr.fields()))
            for fname, v in (("code", it["code"]), ("severity", it["severity"]), ("message", it["message"]),
                             ("class_name", it["key"][0]), ("feature_fid", it["key"][1]), ("created", now)):
                f.setAttribute(fname, v)
            feats.append(f)
        prov.addFeatures(feats)
        counts[name] = len(feats)
        loaded = project_layer(cfg.path, name)
        if loaded is not None:
            loaded.dataProvider().reloadData()
            loaded.triggerRepaint()
    return counts


def write_flag_field(cfg, field, keys, layers=None):
    """Set an integer field to 1 for `keys` and 0 for every other feature (e.g. isconnected)."""
    layers = layers or class_layers(cfg)
    changed = 0
    for role, lyr in layers.items():
        cls = cfg.classes[role]
        i = lyr.fields().indexFromName(field)
        if i < 0:
            continue
        updates = {}
        for f in lyr.getFeatures():
            new = 1 if (cls, f.id()) in keys else 0
            if _int(_val(f, field)) != new:
                updates[f.id()] = {i: new}
        if updates:
            if lyr.isEditable():
                for fid, attrs in updates.items():
                    lyr.changeAttributeValue(fid, i, attrs[i])
            else:
                lyr.dataProvider().changeAttributeValues(updates)
                lyr.triggerRepaint()
            changed += len(updates)
    return changed


def write_dirty_areas(cfg, rects, reason="edit", replace=False):
    lyr = get_layer(cfg.path, "un_dirty_areas")
    prov = lyr.dataProvider()
    if replace:
        prov.deleteFeatures([f.id() for f in lyr.getFeatures()])
    now = datetime.now().isoformat(timespec="seconds")
    feats = []
    for r in rects:
        f = QgsFeature(lyr.fields())
        f.setAttributes([None] * len(lyr.fields()))
        f.setAttribute("created", now)
        f.setAttribute("reason", reason)
        f.setGeometry(QgsGeometry.fromRect(r))
        feats.append(f)
    prov.addFeatures(feats)
    loaded = project_layer(cfg.path, "un_dirty_areas")
    if loaded is not None:
        loaded.dataProvider().reloadData()
        loaded.triggerRepaint()
    return len(feats)


def read_dirty_areas(cfg):
    try:
        lyr = get_layer(cfg.path, "un_dirty_areas")
    except S.StoreError:
        return []
    return [f.geometry().boundingBox() for f in lyr.getFeatures() if f.hasGeometry()]


def export_subnetwork(cfg, info, path):
    """Write the features of one subnetwork into a new GeoPackage (one layer per class)."""
    from qgis.core import QgsCoordinateTransformContext, QgsVectorFileWriter
    by_cls = {}
    for cls, fid in info["lines"] | info["points"]:
        by_cls.setdefault(cls, []).append(fid)
    written = 0
    first = True
    for role, lyr in class_layers(cfg).items():
        cls = cfg.classes[role]
        if cls not in by_cls:
            continue
        keep = lyr.selectedFeatureIds()
        lyr.selectByIds(by_cls[cls])
        opt = QgsVectorFileWriter.SaveVectorOptions()
        opt.driverName = "GPKG"
        opt.layerName = cls
        opt.onlySelectedFeatures = True
        opt.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile if first else
                                    QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer) \
            if hasattr(QgsVectorFileWriter, "ActionOnExistingFile") else \
            (QgsVectorFileWriter.CreateOrOverwriteFile if first else QgsVectorFileWriter.CreateOrOverwriteLayer)
        res = QgsVectorFileWriter.writeAsVectorFormatV3(lyr, path, QgsCoordinateTransformContext(), opt)
        lyr.selectByIds(keep)
        if res[0] != 0:
            raise S.StoreError("Could not write %s: %s" % (cls, res[1]))
        written += len(by_cls[cls])
        first = False
    return written


def verify(cfg):
    """Verify network topology: consistency of the data with the network definition."""
    out = []
    layers = class_layers(cfg)
    for role, cls in cfg.classes.items():
        if role not in layers:
            out.append("ERROR  class %s (%s) is missing from the file" % (cls, role))
    valid = {(r["class_name"], r["ag_code"], r["at_code"]) for r in cfg.assets}
    groups = {(r["class_name"], r["ag_code"]) for r in cfg.assets}
    fids = {}
    for role, lyr in layers.items():
        cls = cfg.classes[role]
        ids = set()
        bad_ag = bad_at = 0
        for f in lyr.getFeatures():
            ids.add(f.id())
            ag, at = _int(_val(f, "assetgroup")), _int(_val(f, "assettype"))
            if (cls, ag) not in groups:
                bad_ag += 1
            elif (cls, ag, at) not in valid:
                bad_at += 1
        fids[cls] = ids
        if bad_ag:
            out.append("ERROR  %s: %d feature(s) with an asset group that is not in the model" % (cls, bad_ag))
        if bad_at:
            out.append("ERROR  %s: %d feature(s) with an asset type that is not in their group" % (cls, bad_at))
    for a, b in cfg.rules:
        for g in (a, b):
            if tuple(g) not in groups:
                out.append("WARN   rule uses %s group %s, which is not in the model" % g)
    lost = sum(1 for _t, a, b in cfg.associations if a[1] not in fids.get(a[0], ()) or b[1] not in fids.get(b[0], ()))
    if lost:
        out.append("ERROR  %d association(s) point to features that no longer exist" % lost)
    dev = cfg.classes.get("Device")
    for (cls, fid), name in cfg.controllers.items():
        if fid not in fids.get(cls, ()):
            out.append("ERROR  controller %s (%s #%s) no longer exists" % (name, cls, fid))
        elif cls != dev:
            out.append("WARN   controller %s is not a device" % name)
    tiers = set(cfg.tier_names())
    for (cls, ag), terms in cfg.terminals.items():
        for name, tier in terms:
            if tier and tier not in tiers:
                out.append("WARN   terminal %s of %s uses the unknown tier %s" % (name, cfg.label(cls, ag), tier))
    for name, by_cls in cfg.network_attributes.items():
        for cls, field in by_cls.items():
            lyr = layers.get(cfg.role_of.get(cls))
            if lyr is not None and lyr.fields().indexFromName(field) < 0:
                out.append("ERROR  network attribute %s: field %s.%s does not exist" % (name, cls, field))
    if not out:
        out.append("OK     the network definition and the data are consistent.")
    return out


def write_subnetwork_field(cfg, subs, layers=None):
    """Write the subnetwork name(s) into the `subnetwork` field of every class."""
    layers = layers or class_layers(cfg)
    names = {}
    for k, ns in subs["by_key"].items():
        names[k] = ";".join(sorted(ns))[:254]
    changed = 0
    for role, lyr in layers.items():
        cls = cfg.classes[role]
        i = lyr.fields().indexFromName("subnetwork")
        if i < 0:
            continue
        updates = {}
        for f in lyr.getFeatures():
            new = names.get((cls, f.id()))
            if (_val(f, "subnetwork") or None) != new:
                updates[f.id()] = {i: new}
        if updates:
            if lyr.isEditable():
                for fid, attrs in updates.items():
                    lyr.changeAttributeValue(fid, i, attrs[i])
            else:
                lyr.dataProvider().changeAttributeValues(updates)
                lyr.triggerRepaint()
            changed += len(updates)
    return changed
