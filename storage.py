"""Storage of a network inside GeoPackage, SpatiaLite or File Geodatabase (GDAL/OGR).

Everything the plugin needs travels inside the data file:
    un_network       key / value settings (network type, tolerance, flow model...)
    un_asset_types   asset groups and asset types per class with their categories and tier
    un_rules         connectivity rules
    un_associations  connectivity / containment / attachment associations
    un_subnetworks   result of the last "Update subnetworks"
    un_errors_point / un_errors_line   result of the last validation
The asset groups / types are also written to the sd_* tables of the
"Subtypes and Domains Manager" plugin (same schema), so when that plugin is installed
the forms get subtype-dependent drop-down lists automatically.
"""
import functools
import os
from datetime import datetime

from osgeo import gdal, ogr, osr

from . import templates as T

MIN_GDAL_FGDB = 3060000
VERSION = "1"


class StoreError(Exception):
    pass


def _guard(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StoreError:
            raise
        except (RuntimeError, OSError) as e:
            msg = str(e)
            if "permission" in msg.lower() or "locked" in msg.lower():
                msg += "\n\nClose edit mode and make sure no other program has the file open."
            raise StoreError(msg) from e

    return wrapper


class Fmt:
    def __init__(self, key, label, driver, ext, directory=False):
        self.key, self.label, self.driver, self.ext, self.directory = key, label, driver, ext, directory


FORMATS = [
    Fmt("gpkg", "GeoPackage (.gpkg)", "GPKG", ".gpkg"),
    Fmt("spatialite", "SpatiaLite (.sqlite)", "SQLite", ".sqlite"),
    Fmt("filegdb", "File Geodatabase (.gdb)", "OpenFileGDB", ".gdb", True),
    Fmt("postgis", "PostgreSQL / PostGIS (server, many users)", "PostgreSQL", ""),
]


def split_domain(path):
    """'file.gpkg#electric' -> ('file.gpkg', 'electric'); the default network has no domain part."""
    path = path or ""
    if "#" in path and not is_pg(path.split("#")[0]) or (is_pg(path) and "#" in path):
        base, dom = path.rsplit("#", 1)
        return base, dom.strip().lower() or None
    return path, None


def table_name(path, name):
    """System tables (un_*) of an additional domain network get the domain as suffix."""
    _base, dom = split_domain(path)
    return "%s__%s" % (name, dom) if dom and name.startswith("un_") else name


def list_domains(path):
    """[None (default network), 'electric', ...] networks stored in one file / schema."""
    base, _d = split_domain(path)
    ds = open_ds(base)
    try:
        out = []
        for i in range(ds.GetLayerCount()):
            n = ds.GetLayerByIndex(i).GetName()
            if n.lower() == "un_network":
                out.append(None)
            elif n.lower().startswith("un_network__"):
                out.append(n[len("un_network__"):])
        ds2 = ds.GetLayerByName("un_network")
        if ds2 is not None and None not in out:
            out.append(None)
        return out
    finally:
        ds = None


def is_pg(path):
    return (path or "").strip().upper().startswith("PG:")


def pg_parts(path):
    """{key: value} of a 'PG:host=... dbname=... active_schema=...' connection string."""
    import shlex
    out = {}
    for tok in shlex.split(path.strip()[3:]):
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def pg_string(host="localhost", port="5432", dbname="", user="", password="", schema="network"):
    parts = ["host=%s" % host, "port=%s" % port, "dbname=%s" % dbname]
    if user:
        parts.append("user=%s" % user)
    if password:
        parts.append("password=%s" % password)
    parts.append("active_schema=%s" % schema.lower())
    return "PG:" + " ".join(parts)


def pg_label(path):
    p = pg_parts(path)
    return "%s/%s (schema %s)" % (p.get("host", ""), p.get("dbname", ""), p.get("active_schema", "public"))


def detect_format(path):
    path = split_domain(path)[0]
    if is_pg(path):
        return FORMATS[3]
    low = (path or "").lower().rstrip("/\\")
    if low.endswith(".gdb"):
        return FORMATS[2]
    if low.endswith(".gpkg"):
        return FORMATS[0]
    if low.endswith((".sqlite", ".db", ".sqlite3", ".spatialite")):
        return FORMATS[1]
    return None


def uri(path, name):
    name = table_name(path, name)
    path = split_domain(path)[0]
    if is_pg(path):
        return "%s|layername=%s" % (path, name.lower())
    return "%s|layername=%s" % (path, name)


def _check_gdal(fmt):
    if fmt.directory and int(gdal.VersionInfo()) < MIN_GDAL_FGDB:
        raise StoreError("File Geodatabase needs GDAL 3.6 or newer (QGIS 3.28 or newer).")


def open_ds(path, update=False):
    ns = split_domain(path)[1]
    path = split_domain(path)[0]
    fmt = detect_format(path)
    if fmt is None:
        raise StoreError("Unsupported file: %s" % path)
    _check_gdal(fmt)
    flag = gdal.OF_UPDATE if update else gdal.OF_READONLY
    try:
        ds = gdal.OpenEx(path, gdal.OF_VECTOR | flag, allowed_drivers=[fmt.driver])
    except RuntimeError:
        ds = None
    if ds is None and not update:
        ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_UPDATE, allowed_drivers=[fmt.driver])
    if ds is None:
        raise StoreError("Could not open %s" % path)
    ds._ns = ns
    return ds


def _tn(ds, name):
    ns = getattr(ds, "_ns", None)
    return "%s__%s" % (name, ns) if ns and name.startswith("un_") else name


# ------------------------------------------------------------------ tables
SYSTEM_TABLES = {
    "un_network": [("key", ogr.OFTString), ("value", ogr.OFTString)],
    "un_asset_types": [("class_name", ogr.OFTString), ("role", ogr.OFTString), ("ag_code", ogr.OFTInteger),
                       ("ag_name", ogr.OFTString), ("at_code", ogr.OFTInteger), ("at_name", ogr.OFTString),
                       ("categories", ogr.OFTString), ("tier", ogr.OFTString)],
    "un_rules": [("from_class", ogr.OFTString), ("from_ag", ogr.OFTInteger),
                 ("to_class", ogr.OFTString), ("to_ag", ogr.OFTInteger)],
    "un_associations": [("assoc_type", ogr.OFTString), ("from_class", ogr.OFTString),
                        ("from_fid", ogr.OFTInteger64), ("to_class", ogr.OFTString),
                        ("to_fid", ogr.OFTInteger64)],
    "un_terminals": [("class_name", ogr.OFTString), ("ag_code", ogr.OFTInteger),
                     ("terminal_name", ogr.OFTString), ("tier", ogr.OFTString)],
    "un_network_attributes": [("name", ogr.OFTString), ("class_name", ogr.OFTString), ("field_name", ogr.OFTString)],
    "un_tiers": [("name", ogr.OFTString), ("rank", ogr.OFTInteger), ("tier_group", ogr.OFTString),
                 ("multi_controllers", ogr.OFTInteger), ("require_controller", ogr.OFTInteger)],
    "un_controllers": [("class_name", ogr.OFTString), ("feature_fid", ogr.OFTInteger64),
                       ("subnetwork_name", ogr.OFTString),
                       ("tier", ogr.OFTString)],
    "un_error_exceptions": [("code", ogr.OFTString), ("class_name", ogr.OFTString),
                            ("feature_fid", ogr.OFTInteger64)],
    "un_unit_prices": [("class_name", ogr.OFTString), ("ag_code", ogr.OFTInteger), ("at_code", ogr.OFTInteger),
                       ("size", ogr.OFTReal), ("unit", ogr.OFTString), ("price", ogr.OFTReal),
                       ("currency", ogr.OFTString), ("description", ogr.OFTString)],
    "un_history": [("changed_at", ogr.OFTString), ("user_name", ogr.OFTString), ("class_name", ogr.OFTString),
                   ("feature_fid", ogr.OFTInteger64), ("action", ogr.OFTString), ("field_name", ogr.OFTString),
                   ("old_value", ogr.OFTString), ("new_value", ogr.OFTString)],
    "un_subnetworks": [("name", ogr.OFTString), ("tier", ogr.OFTString), ("controller_class", ogr.OFTString),
                       ("controller_fid", ogr.OFTInteger64), ("line_count", ogr.OFTInteger),
                       ("point_count", ogr.OFTInteger), ("customers", ogr.OFTInteger),
                       ("updated", ogr.OFTString)],
}
ERROR_FIELDS = [("code", ogr.OFTString), ("severity", ogr.OFTString), ("message", ogr.OFTString),
                ("class_name", ogr.OFTString), ("feature_fid", ogr.OFTInteger64), ("created", ogr.OFTString)]
ERROR_LAYERS = {"un_errors_point": ogr.wkbPoint, "un_errors_line": ogr.wkbMultiLineString}
DIRTY_FIELDS = [("created", ogr.OFTString), ("reason", ogr.OFTString)]

SD_TABLES = {
    "sd_domains": [("dom_name", ogr.OFTString), ("dom_kind", ogr.OFTString), ("dom_type", ogr.OFTString),
                   ("dom_desc", ogr.OFTString), ("range_min", ogr.OFTReal), ("range_max", ogr.OFTReal)],
    "sd_domain_values": [("dom_name", ogr.OFTString), ("code_int", ogr.OFTInteger), ("code_real", ogr.OFTReal),
                         ("code_text", ogr.OFTString), ("label", ogr.OFTString)],
    "sd_layers": [("lyr_name", ogr.OFTString), ("subtype_field", ogr.OFTString)],
    "sd_subtypes": [("lyr_name", ogr.OFTString), ("st_code", ogr.OFTInteger), ("st_name", ogr.OFTString),
                    ("is_default", ogr.OFTInteger)],
    "sd_rules": [("lyr_name", ogr.OFTString), ("field_name", ogr.OFTString), ("st_code", ogr.OFTInteger),
                 ("dom_name", ogr.OFTString), ("default_val", ogr.OFTString)],
}
HIDDEN = set(SYSTEM_TABLES) | set(SD_TABLES) | set(ERROR_LAYERS) | {"un_dirty_areas", "un_work_orders",
    "feature_datasets", "feature_dataset_members", "layer_styles"}


TRACKING_FIELDS = [("globalid", ogr.OFTString, 38), ("created_user", ogr.OFTString, 100), ("created_date", ogr.OFTDateTime, 0),
                   ("last_edited_user", ogr.OFTString, 100), ("last_edited_date", ogr.OFTDateTime, 0),
                   ("isconnected", ogr.OFTInteger, 0)]
DEFAULT_REQUIRED = "assetgroup,assettype,lifecyclestatus"
ASSET_FIELDS = [("condition", ogr.OFTInteger, 0), ("crit_customers", ogr.OFTInteger, 0),
                ("likelihood", ogr.OFTInteger, 0), ("consequence", ogr.OFTInteger, 0),
                ("risk_score", ogr.OFTInteger, 0), ("risk_class", ogr.OFTString, 20)]


def class_fields(tpl, role):
    """[(name, ogr type, width)] for a network class."""
    f = list(TRACKING_FIELDS) + [("assetgroup", ogr.OFTInteger, 0), ("assettype", ogr.OFTInteger, 0), ("assetid", ogr.OFTString, 50),
         ("lifecyclestatus", ogr.OFTInteger, 0), ("installdate", ogr.OFTDate, 0),
         ("subnetwork", ogr.OFTString, 254), ("notes", ogr.OFTString, 254)]
    size = tpl.get("size_field") or ("size", "Size")
    if role in ("Device", "Line"):
        f += list(ASSET_FIELDS)
    if role == "Device":
        f += [("operatingstatus", ogr.OFTInteger, 0), ("customerid", ogr.OFTString, 50),
              (size[0], ogr.OFTReal, 0)]
    elif role == "Line":
        f += [(size[0], ogr.OFTReal, 0), ("measuredlength", ogr.OFTReal, 0), ("customerid", ogr.OFTString, 50)]
        if tpl["flow"] == "gravity":
            f += [("flowdirection", ogr.OFTInteger, 0), ("us_invert", ogr.OFTReal, 0),
                  ("ds_invert", ogr.OFTReal, 0)]
        if tpl["flow"] == "undirected":
            f += [("oneway", ogr.OFTInteger, 0), ("roadname", ogr.OFTString, 100), ("speed", ogr.OFTReal, 0),
                  ("f_elev", ogr.OFTInteger, 0), ("t_elev", ogr.OFTInteger, 0), ("minutes", ogr.OFTReal, 0)]
    elif role == "Junction":
        f += [("elevation", ogr.OFTReal, 0)]
    elif role in ("Assembly", "StructureBoundary"):
        f += [("name", ogr.OFTString, 100)]
    return f


GEOM_OGR = {"Point": ogr.wkbPoint, "LineString": ogr.wkbMultiLineString, "Polygon": ogr.wkbMultiPolygon}


def _create_container(fmt, path):
    if fmt.key == "postgis":
        parts = pg_parts(path)
        schema = parts.get("active_schema") or "public"
        base = "PG:" + " ".join("%s=%s" % (k, v) for k, v in parts.items() if k != "active_schema")
        ds = gdal.OpenEx(base, gdal.OF_VECTOR | gdal.OF_UPDATE, allowed_drivers=["PostgreSQL"])
        if ds is None:
            raise StoreError("Could not connect to PostgreSQL: %s" % gdal.GetLastErrorMsg())
        ds.ExecuteSQL('CREATE SCHEMA IF NOT EXISTS "%s"' % schema)
        try:
            ds.ExecuteSQL("CREATE EXTENSION IF NOT EXISTS postgis")
        except Exception:
            pass
        ds = None
        ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_UPDATE, allowed_drivers=["PostgreSQL"])
        if ds is None:
            raise StoreError("Could not open the schema %s: %s" % (schema, gdal.GetLastErrorMsg()))
        return ds
    drv = ogr.GetDriverByName(fmt.driver)
    if drv is None:
        raise StoreError("GDAL driver %s is not available." % fmt.driver)
    opts = ["SPATIALITE=YES"] if fmt.key == "spatialite" else []
    ds = drv.CreateDataSource(path, options=opts)
    if ds is None:
        raise StoreError("Could not create %s" % path)
    return ds


def _make_table(ds, name, fields, geom=ogr.wkbNone, srs=None, fmt=None):
    name = _tn(ds, name)
    lyr = ds.GetLayerByName(name)
    if lyr is not None:
        return lyr
    opts = []
    if fmt is not None and fmt.key == "spatialite" and geom != ogr.wkbNone:
        opts = ["SPATIAL_INDEX=YES"]
    if fmt is not None and fmt.key == "postgis":
        opts = ["GEOMETRY_NAME=geom", "FID=fid", "LAUNDER=YES"] + (["SPATIAL_INDEX=GIST"] if geom != ogr.wkbNone else [])
    lyr = ds.CreateLayer(name, srs if geom != ogr.wkbNone else None, geom, opts)
    if lyr is None:
        raise StoreError("Could not create %s." % name)
    for spec in fields:
        fname, ftype = spec[0], spec[1]
        fd = ogr.FieldDefn(fname, ftype)
        if len(spec) > 2 and spec[2]:
            fd.SetWidth(spec[2])
        lyr.CreateField(fd)
    return lyr


def _rewrite(ds, name, fields, rows):
    lyr = _make_table(ds, name, fields)
    drv = ds.GetDriver()
    sqlite = (getattr(drv, "ShortName", None) or drv.GetName()) in ("GPKG", "SQLite", "PostgreSQL")
    if sqlite:
        ds.StartTransaction()
    try:
        lyr.ResetReading()
        for fid in [f.GetFID() for f in lyr]:
            lyr.DeleteFeature(fid)
        for row in rows:
            feat = ogr.Feature(lyr.GetLayerDefn())
            for k, v in row.items():
                if v is not None:
                    feat.SetField(k, v)
            lyr.CreateFeature(feat)
    except Exception:
        if sqlite:
            ds.RollbackTransaction()
        raise
    if sqlite:
        ds.CommitTransaction()


def _read_rows(ds, name):
    lyr = ds.GetLayerByName(_tn(ds, name))
    if lyr is None:
        return []
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    lyr.ResetReading()
    return [{n: (f.GetField(i) if f.IsFieldSetAndNotNull(i) else None) for i, n in enumerate(names)}
            for f in lyr]


def make_srs(authid="", wkt=""):
    srs = osr.SpatialReference()
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    if authid and authid.upper().startswith("EPSG:"):
        srs.ImportFromEPSG(int(authid.split(":")[1]))
    elif wkt:
        srs.ImportFromWkt(wkt)
    else:
        raise StoreError("Choose a coordinate reference system.")
    return srs


# ------------------------------------------------------------------ create
@_guard
def create_network(path, tpl_key, authid="", wkt="", tolerance=0.001, gap=0.5,
                   include_structures=True, name=""):
    """Create (or add to) a data file with the classes and tables of a network type.

    Returns {role: class name}."""
    fmt = detect_format(path)
    if fmt is None:
        raise StoreError("The file must end with .gpkg, .sqlite or .gdb")
    _check_gdal(fmt)
    tpl = T.BY_KEY[tpl_key]
    srs = make_srs(authid, wkt)
    base = split_domain(path)[0]
    exists = os.path.exists(base) and not is_pg(base)
    if not exists and not is_pg(base):
        for extra in ("-wal", "-shm", "-journal"):   # leftovers of a deleted file would bring old data back
            if os.path.exists(base + extra):
                os.remove(base + extra)
    ds = open_ds(path, update=True) if exists else _create_container(fmt, base)
    ds._ns = split_domain(path)[1]
    try:
        if ds.GetLayerByName(_tn(ds, "un_network")) is not None:
            settings = {r["key"]: r["value"] for r in _read_rows(ds, "un_network")}
            if settings.get("network_type"):
                raise StoreError("This file already holds a network (%s). Use a new file."
                                 % settings.get("network_type"))
        names = T.class_names(tpl, include_structures)
        if fmt.key in ("spatialite", "postgis"):  # these store table names in lower case
            names = {r: c.lower() for r, c in names.items()}
        rename = {c: names[r] for r, c in T.class_names(tpl, include_structures).items()}
        shared = set()        # structure network classes are shared by every domain network of the file
        for role, cls in names.items():
            if ds.GetLayerByName(cls) is not None:
                if role.startswith("Structure") and ds._ns:
                    shared.add(cls)
                    continue
                raise StoreError("The layer %s already exists in the file." % cls)
        for role, cls in names.items():
            if cls not in shared:
                _make_table(ds, cls, class_fields(tpl, role), GEOM_OGR[T.GEOMETRY[role]], srs, fmt)
        for tname, fields in SYSTEM_TABLES.items():
            _make_table(ds, tname, fields)
        for tname, geom in ERROR_LAYERS.items():
            _make_table(ds, tname, ERROR_FIELDS, geom, srs, fmt)
        _make_table(ds, "un_dirty_areas", DIRTY_FIELDS, ogr.wkbPolygon, srs, fmt)
        settings = {"required_fields": DEFAULT_REQUIRED, "network_type": tpl_key, "name": name or tpl["label"], "flow": tpl["flow"],
                    "tolerance": repr(float(tolerance)), "gap": repr(float(gap)), "version": VERSION,
                    "size_field": tpl["size_field"][0], "created": datetime.now().isoformat(timespec="seconds")}
        for role, cls in names.items():
            settings["class_" + role] = cls
        _rewrite(ds, "un_network", SYSTEM_TABLES["un_network"],
                 [{"key": k, "value": v} for k, v in settings.items()])
        assets = T.asset_rows(tpl, include_structures)
        for r in assets:
            r["class_name"] = rename[r["class_name"]]
        if shared:            # keep the structure asset groups the file already has
            ns, ds._ns = ds._ns, None
            existing = [r for r in _read_rows(ds, "un_asset_types") if r["class_name"] in shared]
            ds._ns = ns
            assets = [r for r in assets if r["class_name"] not in shared] + existing
        rules = {tuple(sorted(((rename[a[0]], a[1]), (rename[b[0]], b[1])), key=str))
                 for a, b in T.default_rules(tpl, include_structures)}
        terms = T.terminal_rows(tpl, include_structures)
        for r in terms:
            r["class_name"] = rename[r["class_name"]]
        _rewrite(ds, "un_asset_types", SYSTEM_TABLES["un_asset_types"], assets)
        _rewrite(ds, "un_terminals", SYSTEM_TABLES["un_terminals"], terms)
        _rewrite(ds, "un_rules", SYSTEM_TABLES["un_rules"], _rule_rows(rules))
        _rewrite(ds, "un_network_attributes", SYSTEM_TABLES["un_network_attributes"], default_attributes(tpl, names))
        _rewrite(ds, "un_tiers", SYSTEM_TABLES["un_tiers"], default_tiers(tpl))
        _write_sd(ds, {r: c for r, c in names.items() if c not in shared},
                  [a for a in assets if a["class_name"] not in shared], tpl["flow"])
    finally:
        ds = None
    return names


def default_attributes(tpl, names):
    rows = []
    size, size_label = tpl.get("size_field") or ("size", "Size")
    for role, cls in names.items():
        rows.append({"name": "Asset group", "class_name": cls, "field_name": "assetgroup"})
        rows.append({"name": "Asset type", "class_name": cls, "field_name": "assettype"})
        rows.append({"name": "Lifecycle status", "class_name": cls, "field_name": "lifecyclestatus"})
        if role == "Device":
            rows.append({"name": "Operating status", "class_name": cls, "field_name": "operatingstatus"})
        if role in ("Device", "Line"):
            rows.append({"name": size_label, "class_name": cls, "field_name": size})
        if role == "Line" and tpl["flow"] == "gravity":
            rows.append({"name": "Flow direction", "class_name": cls, "field_name": "flowdirection"})
        if role == "Line" and tpl["flow"] == "undirected":
            rows.append({"name": "One way", "class_name": cls, "field_name": "oneway"})
            rows.append({"name": "Speed", "class_name": cls, "field_name": "speed"})
        if role == "Junction":
            rows.append({"name": "Elevation", "class_name": cls, "field_name": "elevation"})
    return rows


def default_tiers(tpl):
    seen = []
    for role in ("Device", "Line", "Junction"):
        for g in tpl["classes"].get(role, []):
            if g["tier"] and g["tier"] not in seen:
                seen.append(g["tier"])
    multi = 0 if tpl["key"] in ("electric", "fiber", "telecom") else 1
    return [{"name": t, "rank": i + 1, "tier_group": "", "multi_controllers": multi, "require_controller": 0}
            for i, t in enumerate(seen)]


def _rule_rows(rules):
    return [{"from_class": a[0], "from_ag": a[1], "to_class": b[0], "to_ag": b[1]} for a, b in sorted(rules, key=str)]


# --------------------------------------------- Subtypes and Domains (sd_*)
def _write_sd(ds, names, assets, flow):
    """Merge asset groups / types (from un_asset_types rows) into the sd_* tables of
    Subtypes and Domains Manager, replacing only the rows of the network classes."""
    for t, fields in SD_TABLES.items():
        _make_table(ds, t, fields)
    doms = _read_rows(ds, "sd_domains")
    vals = _read_rows(ds, "sd_domain_values")
    lyrs = _read_rows(ds, "sd_layers")
    sts = _read_rows(ds, "sd_subtypes")
    rules = _read_rows(ds, "sd_rules")
    ours = set(names.values())
    lyrs = [r for r in lyrs if r["lyr_name"] not in ours]
    sts = [r for r in sts if r["lyr_name"] not in ours]
    rules = [r for r in rules if r["lyr_name"] not in ours]
    old_prefixes = tuple("%s_" % c for c in ours)
    new_doms = {}
    new_doms["UN_Lifecycle"] = (T.LIFECYCLE, "Lifecycle status")
    new_doms["UN_OperatingStatus"] = (T.OPERATING, "Operating status")
    if flow == "gravity":
        new_doms["UN_FlowDirection"] = (T.FLOW_DIRECTION, "Flow direction")
    if flow == "undirected":
        new_doms["UN_OneWay"] = (T.ONEWAY, "One way")
    for role, cls in names.items():
        groups = {}
        for r in assets:
            if r["class_name"] == cls:
                g = groups.setdefault(r["ag_code"], {"name": r["ag_name"], "types": []})
                g["types"].append((r["at_code"], r["at_name"]))
        if not groups:
            continue
        lyrs.append({"lyr_name": cls, "subtype_field": "assetgroup"})
        first = min(groups)
        for ag in sorted(groups):
            g = groups[ag]
            sts.append({"lyr_name": cls, "st_code": ag, "st_name": g["name"], "is_default": 1 if ag == first else 0})
            dname = "%s_%d_AssetType" % (cls, ag)
            new_doms[dname] = (sorted(g["types"]), "%s asset types" % g["name"])
            rules.append({"lyr_name": cls, "field_name": "assettype", "st_code": ag, "dom_name": dname,
                          "default_val": str(min(c for c, _n in g["types"]))})
        rules.append({"lyr_name": cls, "field_name": "lifecyclestatus", "st_code": None,
                      "dom_name": "UN_Lifecycle", "default_val": "3"})
        if role == "Device":
            rules.append({"lyr_name": cls, "field_name": "operatingstatus", "st_code": None,
                          "dom_name": "UN_OperatingStatus", "default_val": "1"})
        if role == "Line" and flow == "gravity":
            rules.append({"lyr_name": cls, "field_name": "flowdirection", "st_code": None,
                          "dom_name": "UN_FlowDirection", "default_val": "1"})
        if role == "Line" and flow == "undirected":
            rules.append({"lyr_name": cls, "field_name": "oneway", "st_code": None,
                          "dom_name": "UN_OneWay", "default_val": "0"})
    doms = [d for d in doms if d["dom_name"] not in new_doms and not d["dom_name"].startswith(old_prefixes)]
    vals = [v for v in vals if v["dom_name"] not in new_doms and not v["dom_name"].startswith(old_prefixes)]
    for dname, (values, desc) in new_doms.items():
        doms.append({"dom_name": dname, "dom_kind": "coded", "dom_type": "integer", "dom_desc": desc,
                     "range_min": None, "range_max": None})
        for code, label in values:
            vals.append({"dom_name": dname, "code_int": code, "code_real": None, "code_text": None,
                         "label": label})
    for t, rows in (("sd_domains", doms), ("sd_domain_values", vals), ("sd_layers", lyrs),
                    ("sd_subtypes", sts), ("sd_rules", rules)):
        _rewrite(ds, t, SD_TABLES[t], rows)


@_guard
def save_model(path, classes, assets, terminals, flow):
    """Save edited asset groups / types / categories / tiers and terminals (Model page)."""
    ds = open_ds(path, update=True)
    try:
        _rewrite(ds, "un_asset_types", SYSTEM_TABLES["un_asset_types"], assets)
        _rewrite(ds, "un_terminals", SYSTEM_TABLES["un_terminals"], terminals)
        _write_sd(ds, classes, assets, flow)
    finally:
        ds = None


# ------------------------------------------------------------------ read
@_guard
def load_config(path):
    ds = open_ds(path)
    try:
        settings = {r["key"]: r["value"] for r in _read_rows(ds, "un_network")}
        if not settings.get("network_type"):
            raise StoreError("No network in this file. Create one first (Network tab).")
        assets = _read_rows(ds, "un_asset_types")
        rules = {tuple(sorted(((r["from_class"], r["from_ag"]), (r["to_class"], r["to_ag"])), key=str))
                 for r in _read_rows(ds, "un_rules")}
        assoc = [(r["assoc_type"], (r["from_class"], r["from_fid"]), (r["to_class"], r["to_fid"]))
                 for r in _read_rows(ds, "un_associations")]
        subs = _read_rows(ds, "un_subnetworks")
        terms = _read_rows(ds, "un_terminals")
        attrs = _read_rows(ds, "un_network_attributes")
        tiers = _read_rows(ds, "un_tiers")
        ctrls = _read_rows(ds, "un_controllers")
        excs = _read_rows(ds, "un_error_exceptions")
    finally:
        ds = None
    classes = {k[len("class_"):]: v for k, v in settings.items() if k.startswith("class_")}
    cfg = Config(path, settings, classes, assets, rules, assoc, subs)
    cfg.terminal_rows = terms
    for r in terms:
        cfg.terminals.setdefault((r["class_name"], r["ag_code"]), []).append((r["terminal_name"], r["tier"]))
    cfg.attribute_rows = attrs
    for r in attrs:
        cfg.network_attributes.setdefault(r["name"], {})[r["class_name"]] = r["field_name"]
    cfg.tier_rows = sorted(tiers, key=lambda r: r.get("rank") or 0)
    for r in cfg.tier_rows:
        cfg.tier_settings[r["name"]] = {"multi": bool(r.get("multi_controllers")),
                                        "require": bool(r.get("require_controller")),
                                        "group": r.get("tier_group") or ""}
    cfg.controller_rows = ctrls
    cfg.controllers = {(r["class_name"], int(r["feature_fid"])): r.get("subnetwork_name") or ""
                       for r in ctrls if r.get("feature_fid") is not None}
    cfg.exceptions = {(r["code"], r["class_name"], int(r["feature_fid"])) for r in excs
                      if r.get("feature_fid") is not None}
    return cfg


class Config:
    def __init__(self, path, settings, classes, assets, rules, assoc, subs):
        self.path, self.settings, self.classes = path, settings, classes
        self.assets, self.rules, self.associations, self.subnetworks = assets, rules, assoc, subs
        self.tpl = T.BY_KEY.get(settings.get("network_type"), T.BY_KEY["generic"])
        self.flow = settings.get("flow", self.tpl["flow"])
        self.tolerance = float(settings.get("tolerance", 0.001))
        self.gap = float(settings.get("gap", 0.5))
        self.cats, self.tiers, self.group_names, self.type_names = {}, {}, {}, {}
        self.terminals, self.terminal_rows = {}, []
        self.network_attributes, self.attribute_rows = {}, []
        self.tier_rows, self.tier_settings = [], {}
        self.controllers, self.controller_rows, self.exceptions = {}, [], set()
        for r in assets:
            k = (r["class_name"], r["ag_code"])
            self.cats[k] = set((r.get("categories") or "").split())
            self.tiers[k] = r.get("tier") or None
            self.group_names[k] = r["ag_name"]
            self.type_names[(r["class_name"], r["ag_code"], r["at_code"])] = r["at_name"]

    @property
    def role_of(self):
        return {v: k for k, v in self.classes.items()}

    def groups(self, cls):
        return sorted({(r["ag_code"], r["ag_name"]) for r in self.assets if r["class_name"] == cls})

    def types(self, cls, ag):
        return sorted({(r["at_code"], r["at_name"]) for r in self.assets
                       if r["class_name"] == cls and r["ag_code"] == ag})

    def find_group(self, role, category=None, name=None):
        """(class, ag) of the first group of a role having `category` or `name`."""
        cls = self.classes.get(role)
        groups = self.groups(cls) if cls else []
        for ag, gname in groups:
            if name and gname.lower() == name.lower():
                return cls, ag
        for ag, gname in groups:
            if category and category in self.cats.get((cls, ag), set()):
                return cls, ag
        return None

    def label(self, cls, ag):
        return self.group_names.get((cls, ag), str(ag))

    def type_label(self, cls, ag, at):
        return self.type_names.get((cls, ag, at), str(at))

    @property
    def required(self):
        return [f.strip() for f in self.settings.get("required_fields", DEFAULT_REQUIRED).split(",") if f.strip()]

    def tier_names(self):
        names = [r["name"] for r in self.tier_rows]
        for r in self.assets:
            if r.get("tier") and r["tier"] not in names:
                names.append(r["tier"])
        return names

    @property
    def topology_enabled(self):
        return self.settings.get("topology_enabled", "1") == "1"

    def trace_configs(self):
        import json
        out = {}
        for k, v in self.settings.items():
            if k.startswith("trace_config:"):
                try:
                    out[k.split(":", 1)[1]] = json.loads(v)
                except ValueError:
                    pass
        return out


# ------------------------------------------------------------------ write
@_guard
def save_rules(path, rules):
    ds = open_ds(path, update=True)
    try:
        _rewrite(ds, "un_rules", SYSTEM_TABLES["un_rules"], _rule_rows(rules))
    finally:
        ds = None


@_guard
def save_associations(path, assoc):
    ds = open_ds(path, update=True)
    try:
        _rewrite(ds, "un_associations", SYSTEM_TABLES["un_associations"],
                 [{"assoc_type": t, "from_class": a[0], "from_fid": a[1], "to_class": b[0], "to_fid": b[1]}
                  for t, a, b in assoc])
    finally:
        ds = None


@_guard
def save_subnetworks(path, rows):
    ds = open_ds(path, update=True)
    try:
        _rewrite(ds, "un_subnetworks", SYSTEM_TABLES["un_subnetworks"], rows)
    finally:
        ds = None


@_guard
def set_setting(path, key, value):
    ds = open_ds(path, update=True)
    try:
        rows = [r for r in _read_rows(ds, "un_network") if r["key"] != key]
        if value is not None:
            rows.append({"key": key, "value": str(value)})
        _rewrite(ds, "un_network", SYSTEM_TABLES["un_network"], rows)
    finally:
        ds = None


@_guard
def save_table(path, table, rows):
    """Replace the rows of a system table (un_tiers, un_controllers, un_network_attributes...)."""
    ds = open_ds(path, update=True)
    try:
        _rewrite(ds, table, SYSTEM_TABLES[table], rows)
    finally:
        ds = None


@_guard
def read_table(path, table):
    ds = open_ds(path)
    try:
        return _read_rows(ds, table)
    finally:
        ds = None


def export_csv(rows, fields, csv_path):
    import csv
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def import_csv(csv_path, table):
    """Rows of a CSV file with the columns of a system table (types converted)."""
    import csv
    types = dict((n, t) for n, t in SYSTEM_TABLES[table])
    out = []
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            row = {}
            for k, v in r.items():
                k = (k or "").strip()
                if k not in types:
                    continue
                v = (v or "").strip()
                if v == "":
                    row[k] = None
                elif types[k] in (ogr.OFTInteger, ogr.OFTInteger64):
                    row[k] = int(float(v))
                elif types[k] == ogr.OFTReal:
                    row[k] = float(v)
                else:
                    row[k] = v
            missing = [n for n in types if n not in row]
            if missing:
                raise StoreError("The CSV file has no column(s): %s" % ", ".join(missing))
            out.append(row)
    return out


@_guard
def upgrade_network(path, classes):
    """Bring a network made by an older version up to date (tracking fields, new tables)."""
    ds = open_ds(path, update=True)
    added = 0
    try:
        for cls in classes.values():
            lyr = ds.GetLayerByName(cls)
            if lyr is None:
                continue
            defn = lyr.GetLayerDefn()
            have = {defn.GetFieldDefn(i).GetName().lower() for i in range(defn.GetFieldCount())}
            role = next((r for r, c in classes.items() if c == cls), "")
            wanted = list(TRACKING_FIELDS) + (list(ASSET_FIELDS) if role in ("Device", "Line") else [])
            for name, ftype, width in wanted:
                if name not in have:
                    fd = ogr.FieldDefn(name, ftype)
                    if width:
                        fd.SetWidth(width)
                    lyr.CreateField(fd)
                    added += 1
        for tname, fields in SYSTEM_TABLES.items():
            _make_table(ds, tname, fields)
        settings = {r["key"]: r["value"] for r in _read_rows(ds, "un_network")}
        if ds.GetLayerByName(_tn(ds, "un_dirty_areas")) is None:
            ref = ds.GetLayerByName(classes.get("Line") or list(classes.values())[0])
            _make_table(ds, "un_dirty_areas", DIRTY_FIELDS, ogr.wkbPolygon, ref.GetSpatialRef() if ref else None,
                        detect_format(path))
        tpl = T.BY_KEY.get(settings.get("network_type"), T.BY_KEY["generic"])
        if not _read_rows(ds, "un_network_attributes"):
            _rewrite(ds, "un_network_attributes", SYSTEM_TABLES["un_network_attributes"],
                     default_attributes(tpl, classes))
        if not _read_rows(ds, "un_tiers"):
            _rewrite(ds, "un_tiers", SYSTEM_TABLES["un_tiers"], default_tiers(tpl))
        if "required_fields" not in settings:
            rows = _read_rows(ds, "un_network") + [{"key": "required_fields", "value": DEFAULT_REQUIRED}]
            _rewrite(ds, "un_network", SYSTEM_TABLES["un_network"], rows)
    finally:
        ds = None
    return added


@_guard
def list_user_layers(path):
    ds = open_ds(path)
    try:
        return [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())
                if ds.GetLayerByIndex(i).GetName() not in HIDDEN]
    finally:
        ds = None
