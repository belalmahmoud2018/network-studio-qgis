"""Reports: network summary, bill of quantities (BOQ), feature lists, Excel
export.

Excel files are written with the GDAL XLSX driver (no extra Python package
needed);
one sheet per table.
"""

import os
from datetime import datetime

from osgeo import ogr
from qgis.core import QgsFeatureRequest

from . import templates as T
from .qgis_io import _int, _num, _val, class_layers, gkind

LIFECYCLE = dict(T.LIFECYCLE)


def _length(f):
    g = f.geometry()
    return g.length() if g is not None and not g.isNull() else 0.0


def _features(lyr, selected_only):
    return lyr.getSelectedFeatures() if selected_only else lyr.getFeatures()


def summary(cfg, lifecycle=None, selected_only=False):
    """Rows per class / asset group / asset type: count and length (lines)."""
    rows = []
    totals = {"lines_m": 0.0, "lines": 0, "points": 0, "polygons": 0}
    for role, lyr in class_layers(cfg).items():
        cls = cfg.classes[role]
        kind = gkind(lyr)
        agg = {}
        for f in _features(lyr, selected_only):
            lc = _int(_val(f, "lifecyclestatus"))
            if lifecycle and lc not in lifecycle:
                continue
            ag, at = _int(_val(f, "assetgroup")), _int(_val(f, "assettype"))
            key = (ag, at)
            a = agg.setdefault(key, [0, 0.0])
            a[0] += 1
            if kind == "line":
                a[1] += _length(f)
        for (ag, at), (n, length) in sorted(
            agg.items(), key=lambda x: (x[0][0] or 0, x[0][1] or 0)
        ):
            rows.append(
                {
                    "Class": cls,
                    "Role": role,
                    "Asset group": (
                        cfg.label(cls, ag) if ag is not None else "(empty)"
                    ),
                    "Asset type": (
                        cfg.type_label(cls, ag, at)
                        if at is not None
                        else "(empty)"
                    ),
                    "Count": n,
                    "Length": round(length, 2) if kind == "line" else None,
                }
            )
            if kind == "line":
                totals["lines_m"] += length
                totals["lines"] += n
            elif kind == "point":
                totals["points"] += n
            else:
                totals["polygons"] += n
    return rows, totals


def boq(cfg, lifecycle=None, selected_only=False):
    """Bill of quantities: lines by group / type / size (length), points by
    group / type (count)."""
    size_field = cfg.settings.get("size_field") or cfg.tpl["size_field"][0]
    size_label = cfg.tpl["size_field"][1]
    lines, points = {}, {}
    for role, lyr in class_layers(cfg).items():
        cls = cfg.classes[role]
        kind = gkind(lyr)
        if kind == "polygon":
            continue
        for f in _features(lyr, selected_only):
            lc = _int(_val(f, "lifecyclestatus"))
            if lifecycle and lc not in lifecycle:
                continue
            ag, at = _int(_val(f, "assetgroup")), _int(_val(f, "assettype"))
            g = cfg.label(cls, ag) if ag is not None else "(empty)"
            t = cfg.type_label(cls, ag, at) if at is not None else "(empty)"
            if kind == "line":
                size = _num(_val(f, size_field))
                k = (cls, g, t, size)
                a = lines.setdefault(k, [0, 0.0])
                a[0] += 1
                a[1] += _length(f)
            else:
                k = (cls, g, t)
                points[k] = points.get(k, 0) + 1
    line_rows = [
        {
            "Class": c,
            "Asset group": g,
            "Asset type": t,
            size_label: s,
            "Pieces": n,
            "Length": round(l, 2),
        }
        for (c, g, t, s), (n, l) in sorted(lines.items(), key=str)
    ]
    point_rows = [
        {"Class": c, "Asset group": g, "Asset type": t, "Quantity": n}
        for (c, g, t), n in sorted(points.items(), key=str)
    ]
    return line_rows, point_rows


def required_issues(cfg):
    """E13: features with an empty required field (Model page -> required
    fields)."""
    issues = []
    req = cfg.required
    for role, lyr in class_layers(cfg).items():
        cls = cfg.classes[role]
        names = [n for n in req if lyr.fields().indexFromName(n) >= 0]
        if not names:
            continue
        for f in lyr.getFeatures():
            empty = [n for n in names if _val(f, n) in (None, "")]
            if not empty or not f.hasGeometry():
                continue
            g = f.geometry()
            p = (
                g.asPoint()
                if gkind(lyr) == "point" and not g.isMultipart()
                else (
                    g.pointOnSurface().asPoint()
                    if gkind(lyr) == "polygon"
                    else g.interpolate(0).asPoint()
                )
            )
            issues.append(
                {
                    "code": "E13",
                    "severity": "error",
                    "message": "Required field empty: %s" % ", ".join(empty),
                    "x": p.x(),
                    "y": p.y(),
                    "key": (cls, f.id()),
                    "geom": "point",
                }
            )
    return issues


def feature_rows(cfg, keys):
    """All attributes of the given (class, fid) features, one list of rows per
    class."""
    by_cls = {}
    for cls, fid in keys:
        by_cls.setdefault(cls, []).append(fid)
    out = {}
    for role, lyr in class_layers(cfg).items():
        cls = cfg.classes[role]
        if cls not in by_cls:
            continue
        rows = []
        names = [f.name() for f in lyr.fields()]
        for f in lyr.getFeatures(
            QgsFeatureRequest().setFilterFids(by_cls[cls])
        ):
            row = {"fid": f.id()}
            for n in names:
                v = f[n]
                row[n] = (
                    None
                    if v is None or (hasattr(v, "isNull") and v.isNull())
                    else v
                )
            ag = _int(row.get("assetgroup"))
            row["asset_group_name"] = (
                cfg.label(cls, ag) if ag is not None else None
            )
            if gkind(lyr) == "line":
                row["length"] = round(_length(f), 2)
            rows.append(row)
        out[cls] = rows
    return out


def write_xlsx(path, sheets):
    """sheets: {sheet name: [row dict, ...]} -> .xlsx (GDAL XLSX driver)."""
    if not path.lower().endswith(".xlsx"):
        path += ".xlsx"
    if os.path.exists(path):
        os.remove(path)
    drv = ogr.GetDriverByName("XLSX")
    if drv is None:
        raise ValueError(
            "The GDAL XLSX driver is not available; export as CSV instead."
        )
    ds = drv.CreateDataSource(path)
    if ds is None:
        raise ValueError("Could not create %s (is it open in Excel?)" % path)
    for name, rows in sheets.items():
        lyr = ds.CreateLayer(name[:31], None, ogr.wkbNone)
        cols = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        if not cols:
            cols = ["(empty)"]
        types = {}
        for c in cols:
            vals = [r.get(c) for r in rows if r.get(c) is not None]
            if vals and all(
                isinstance(v, bool) is False and isinstance(v, int)
                for v in vals
            ):
                types[c] = ogr.OFTInteger64
            elif vals and all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in vals
            ):
                types[c] = ogr.OFTReal
            else:
                types[c] = ogr.OFTString
            lyr.CreateField(ogr.FieldDefn(str(c), types[c]))
        defn = lyr.GetLayerDefn()
        for r in rows:
            feat = ogr.Feature(defn)
            for i, c in enumerate(cols):
                v = r.get(c)
                if v is None:
                    continue
                if types[c] == ogr.OFTString:
                    v = (
                        v.toString("yyyy-MM-dd HH:mm:ss")
                        if hasattr(v, "toString")
                        else str(v)
                    )
                feat.SetField(i, v)
            lyr.CreateFeature(feat)
    ds = None
    return path


def report_text(cfg, rows, totals):
    lines = [
        "%s - %s"
        % (
            cfg.settings.get("name", ""),
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        ),
        "",
        "Lines: %d features, total length %.2f"
        % (totals["lines"], totals["lines_m"]),
        "Points: %d features" % totals["points"],
        "Polygons: %d features" % totals["polygons"],
        "",
    ]
    cur = None
    for r in rows:
        if r["Class"] != cur:
            cur = r["Class"]
            lines.append(cur)
        extra = (
            ("   length %.2f" % r["Length"]) if r["Length"] is not None else ""
        )
        lines.append(
            "   %-28s %-24s %6d%s"
            % (r["Asset group"][:28], r["Asset type"][:24], r["Count"], extra)
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# dashboard / QA
def _optional_layer(cfg, name):
    from .qgis_io import get_layer

    try:
        return get_layer(cfg.path, name)
    except Exception as e:
        from .qgis_io import log

        log("%s: %s" % (name, e))
        return None


def dashboard(cfg):
    """Numbers for the project dashboard."""
    out = {
        "classes": [],
        "lifecycle": {},
        "errors": {},
        "completeness": [],
        "isconnected": None,
    }
    connected = total = 0
    req = cfg.required
    for role, lyr in class_layers(cfg).items():
        cls = cfg.classes[role]
        n = 0
        filled = {f: 0 for f in req if lyr.fields().indexFromName(f) >= 0}
        has_ic = (
            lyr.fields().indexFromName("isconnected") >= 0
            and gkind(lyr) != "polygon"
        )
        for f in lyr.getFeatures():
            n += 1
            lc = LIFECYCLE.get(_int(_val(f, "lifecyclestatus")), "(empty)")
            out["lifecycle"][lc] = out["lifecycle"].get(lc, 0) + 1
            for fld in filled:
                if _val(f, fld) not in (None, ""):
                    filled[fld] += 1
            if has_ic and not role.startswith("Structure"):
                total += 1
                connected += 1 if _int(_val(f, "isconnected")) == 1 else 0
        out["classes"].append({"Class": cls, "Role": role, "Features": n})
        for fld, k in filled.items():
            out["completeness"].append(
                {
                    "Class": cls,
                    "Field": fld,
                    "Filled": k,
                    "Features": n,
                    "Percent": round(100.0 * k / n, 1) if n else 100.0,
                }
            )
    for name in ("un_errors_point", "un_errors_line"):
        lyr = _optional_layer(cfg, name)
        if lyr is None:  # error layer not created yet: nothing to count
            continue
        for f in lyr.getFeatures():
            key = (f["code"], f["severity"])
            out["errors"][key] = out["errors"].get(key, 0) + 1
    out["isconnected"] = (connected, total)
    return out


def dashboard_text(cfg, d, wo=None):
    lines = [
        "PROJECT DASHBOARD - %s" % cfg.settings.get("name", ""),
        "Network type: %s   |   updated %s"
        % (cfg.tpl["label"], datetime.now().strftime("%Y-%m-%d %H:%M")),
        "Last validation: %s"
        % (cfg.settings.get("last_validation") or "never"),
        "",
        "FEATURES",
    ]
    for r in d["classes"]:
        lines.append(
            "  %-26s %-20s %8d" % (r["Class"], r["Role"], r["Features"])
        )
    lines += ["", "LIFECYCLE"]
    for k, v in sorted(d["lifecycle"].items()):
        lines.append("  %-30s %8d" % (k, v))
    lines += ["", "VALIDATION ERRORS"]
    if not d["errors"]:
        lines.append("  none (or not validated yet)")
    for (code, sev), n in sorted(d["errors"].items(), key=str):
        lines.append("  %-5s %-8s %8d" % (code, sev, n))
    c, t = d["isconnected"]
    lines += [
        "",
        "CONNECTED TO A SOURCE (isconnected): %d of %d%s"
        % (c, t, "  (%.1f%%)" % (100.0 * c / t) if t else ""),
    ]
    lines += ["", "DATA COMPLETENESS (required fields)"]
    for r in d["completeness"]:
        if r["Features"]:
            lines.append(
                "  %-24s %-18s %6.1f%%"
                % (r["Class"], r["Field"], r["Percent"])
            )
    lines += ["", "SUBNETWORKS: %d" % len(cfg.subnetworks)]
    if wo:
        lines += ["", "WORK ORDERS"] + [
            "  %-14s %6d" % (k, v) for k, v in wo.items()
        ]
    return "\n".join(lines)


def qa_sheets(cfg, d):
    from .qgis_io import get_layer

    errors = []
    for name in ("un_errors_point", "un_errors_line"):
        lyr = get_layer(cfg.path, name)
        for f in lyr.getFeatures():
            errors.append(
                {
                    fld.name(): (
                        None
                        if v is None or (hasattr(v, "isNull") and v.isNull())
                        else v
                    )
                    for fld, v in zip(lyr.fields(), f.attributes())
                }
            )
    summary = [
        {"Item": "Network", "Value": cfg.settings.get("name", "")},
        {"Item": "Network type", "Value": cfg.tpl["label"]},
        {
            "Item": "Report date",
            "Value": datetime.now().strftime("%Y-%m-%d %H:%M"),
        },
        {
            "Item": "Last validation",
            "Value": cfg.settings.get("last_validation") or "never",
        },
        {
            "Item": "Connected to a source",
            "Value": "%d of %d" % d["isconnected"],
        },
    ]
    summary += [
        {"Item": "Features - %s" % r["Class"], "Value": r["Features"]}
        for r in d["classes"]
    ]
    err_sum = [
        {"Code": c, "Severity": s, "Count": n}
        for (c, s), n in sorted(d["errors"].items(), key=str)
    ]
    return {
        "Summary": summary,
        "Error summary": err_sum,
        "Errors": errors,
        "Completeness": d["completeness"],
        "Lifecycle": [
            {"Lifecycle": k, "Features": v}
            for k, v in sorted(d["lifecycle"].items())
        ],
    }


# ---------------------------------------------------------------------- data
# dictionary
FIELD_HELP = {
    "assetgroup": "Asset group (subtype) - what the feature is",
    "assettype": "Asset type inside the group",
    "assetid": "Asset identifier",
    "lifecyclestatus": "Proposed / under construction / in service / ...",
    "installdate": "Installation date",
    "subnetwork": "Subnetwork name (written by Update subnetworks)",
    "notes": "Free notes",
    "operatingstatus": "Open (1) or closed (0)",
    "customerid": "Customer / account id",
    "measuredlength": "Length in map units",
    "flowdirection": "With or against the digitized direction",
    "us_invert": "Upstream invert level",
    "ds_invert": "Downstream invert level",
    "oneway": "One-way rule",
    "roadname": "Road name",
    "speed": "Speed",
    "elevation": "Ground / node elevation",
    "name": "Name",
    "created_user": "Created by",
    "created_date": "Created on",
    "last_edited_user": "Last edited by",
    "last_edited_date": "Last edited on",
    "isconnected": "1 = connected to a controller (Update Is connected)",
}


def data_dictionary(cfg):
    sheets = {
        "Classes": [],
        "Fields": [],
        "Asset groups and types": [],
        "Terminals": [],
        "Rules": [],
        "Network attributes": [],
        "Tiers": [],
        "Domains": [],
    }
    for role, lyr in class_layers(cfg).items():
        cls = cfg.classes[role]
        sheets["Classes"].append(
            {
                "Class": cls,
                "Role": role,
                "Geometry": gkind(lyr),
                "Features": lyr.featureCount(),
                "CRS": lyr.crs().authid(),
            }
        )
        for fld in lyr.fields():
            sheets["Fields"].append(
                {
                    "Class": cls,
                    "Field": fld.name(),
                    "Type": fld.typeName(),
                    "Length": fld.length() or None,
                    "Description": FIELD_HELP.get(fld.name(), ""),
                }
            )
    for r in cfg.assets:
        sheets["Asset groups and types"].append(
            {
                "Class": r["class_name"],
                "Group code": r["ag_code"],
                "Asset group": r["ag_name"],
                "Type code": r["at_code"],
                "Asset type": r["at_name"],
                "Categories": r.get("categories") or "",
                "Tier": r.get("tier") or "",
            }
        )
    for r in cfg.terminal_rows:
        sheets["Terminals"].append(
            {
                "Class": r["class_name"],
                "Asset group": cfg.label(r["class_name"], r["ag_code"]),
                "Terminal": r["terminal_name"],
                "Tier": r.get("tier") or "",
            }
        )
    for a, b in sorted(cfg.rules, key=str):
        sheets["Rules"].append(
            {
                "From class": a[0],
                "From group": cfg.label(*a),
                "To class": b[0],
                "To group": cfg.label(*b),
            }
        )
    for r in cfg.attribute_rows:
        sheets["Network attributes"].append(
            {
                "Attribute": r["name"],
                "Class": r["class_name"],
                "Field": r["field_name"],
            }
        )
    for r in cfg.tier_rows:
        sheets["Tiers"].append(
            {
                "Tier": r["name"],
                "Rank": r.get("rank"),
                "Tier group": r.get("tier_group") or "",
                "Many controllers": r.get("multi_controllers"),
                "Controller required": r.get("require_controller"),
            }
        )
    for name, values in (
        ("Lifecycle status", T.LIFECYCLE),
        ("Operating status", T.OPERATING),
        ("Flow direction", T.FLOW_DIRECTION),
        ("One way", T.ONEWAY),
    ):
        for code, label in values:
            sheets["Domains"].append(
                {"Domain": name, "Code": code, "Value": label}
            )
    return sheets
