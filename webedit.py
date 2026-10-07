"""Editing the network from the web (QGIS Server WFS-T / Lizmap) and keeping
it consistent.

* lizmap_config()      writes the Lizmap configuration (<project>.qgs.cfg)
                       with the editable layers and their capabilities.
* use_postgres_provider() switches the PostGIS layers of the saved web
                       project to the native PostgreSQL provider (needed by
                       Lizmap editing). The open project is not changed.
* install_triggers()   PostGIS only: database triggers that fill globalid and
                       the editor tracking fields and write dirty areas for
                       every insert / update / delete, whoever makes it (web,
                       QField, another GIS, SQL).
* check_external_edits() any format: compares the network with the state
                       saved at the previous check and marks dirty areas
                       where features were added, changed or deleted outside
                       this QGIS session; changes go to the edit history.
"""

import json
import xml.etree.ElementTree as ET  # nosec B405 - our own saved project
from datetime import datetime

from osgeo import ogr

from . import storage as S
from . import versioning as V

STATE = "un_feature_state"
STATE_FIELDS = [
    ("class_name", ogr.OFTString),
    ("globalid", ogr.OFTString),
    ("hash", ogr.OFTString),
    ("minx", ogr.OFTReal),
    ("miny", ogr.OFTReal),
    ("maxx", ogr.OFTReal),
    ("maxy", ogr.OFTReal),
]
NETWORK_FIELDS = (
    "assetgroup",
    "assettype",
    "operatingstatus",
    "lifecyclestatus",
    "flowdirection",
    "oneway",
    "f_elev",
    "t_elev",
)
COMPUTED = (
    "subnetwork",
    "isconnected",
    "crit_customers",
    "likelihood",
    "consequence",
    "risk_score",
    "risk_class",
    "last_edited_user",
    "last_edited_date",
)


# ------------------------------------------------------------------ Lizmap
def _geom_name(lyr):
    from .qgis_io import gkind

    return {"point": "point", "line": "line", "polygon": "polygon"}.get(
        gkind(lyr), "none"
    )


def lizmap_config(qgs_path, layers, editable, capabilities, title=""):
    """Write <project>.qgs.cfg for Lizmap Web Client.

    layers: published QgsVectorLayers; editable: ids of the layers editable
    on the web; capabilities: dict create / attributes / geometry / delete.
    Open the project once with the Lizmap plugin to review it."""
    if not layers:
        raise ValueError("No layer to publish.")
    ext = None
    for lyr in layers:
        e = lyr.extent()
        if not e.isEmpty():
            ext = e if ext is None else (ext.combineExtentWith(e) or ext)
    crs = layers[0].crs()
    bbox = (
        [
            str(ext.xMinimum()),
            str(ext.yMinimum()),
            str(ext.xMaximum()),
            str(ext.yMaximum()),
        ]
        if ext is not None
        else ["0", "0", "0", "0"]
    )

    def tf(v):
        return "True" if v else "False"

    cfg = {
        "metadata": {
            "lizmap_plugin_version_str": "network-studio",
            "lizmap_web_client_target_version": 30700,
            "project_valid": True,
        },
        "options": {
            "projection": {
                "proj4": crs.toProj(),
                "ref": crs.authid(),
            },
            "bbox": bbox,
            "initialExtent": [float(v) for v in bbox],
            "mapScales": [1000, 2500, 5000, 10000, 25000, 50000, 100000],
            "minScale": 1,
            "maxScale": 1000000000,
            "pointTolerance": 25,
            "lineTolerance": 10,
            "polygonTolerance": 5,
            "popupLocation": "dock",
            "hideProject": "False",
        },
        "layers": {},
        "editionLayers": {},
    }
    for order, lyr in enumerate(layers):
        name = lyr.name()
        cfg["layers"][name] = {
            "id": lyr.id(),
            "name": name,
            "type": "layer",
            "geometryType": _geom_name(lyr),
            "crs": lyr.crs().authid(),
            "title": name,
            "abstract": "",
            "link": "",
            "minScale": 1,
            "maxScale": 1000000000000,
            "toggled": "True",
            "popup": "True",
            "popupSource": "auto",
            "popupTemplate": "",
            "popupMaxFeatures": 10,
            "popupDisplayChildren": "False",
            "noLegendImage": "False",
            "groupAsLayer": "False",
            "baseLayer": "False",
            "displayInLegend": "True",
            "singleTile": "True",
            "imageFormat": "image/png",
            "cached": "False",
            "clientCacheExpiration": 300,
        }
        if lyr.id() in editable:
            snap = [x.id() for x in layers if x.id() in editable]
            cfg["editionLayers"][name] = {
                "layerId": lyr.id(),
                "geometryType": _geom_name(lyr),
                "capabilities": {
                    "createFeature": tf(capabilities.get("create", True)),
                    "allow_without_geom": "False",
                    "modifyAttribute": tf(
                        capabilities.get("attributes", True)
                    ),
                    "modifyGeometry": tf(capabilities.get("geometry", True)),
                    "deleteFeature": tf(capabilities.get("delete", False)),
                },
                "acl": capabilities.get("groups", ""),
                "snap_layers": snap,
                "snap_vertices": "True",
                "snap_segments": "True",
                "snap_intersections": "True",
                "snap_vertices_tolerance": 10,
                "snap_segments_tolerance": 10,
                "snap_intersections_tolerance": 10,
                "provider": (
                    "postgres"
                    if lyr.providerType() in ("postgres", "ogr")
                    and lyr.source().upper().startswith("PG:")
                    else lyr.providerType()
                ),
                "order": order,
            }
    path = qgs_path + ".cfg"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    return path


def _pg_uri(pg_path, table, geom="geom", key="fid", wkb=""):
    from qgis.core import QgsDataSourceUri

    p = S.pg_parts(pg_path)
    u = QgsDataSourceUri()
    u.setConnection(
        p.get("host", ""),
        p.get("port", "5432"),
        p.get("dbname", ""),
        p.get("user", ""),
        p.get("password", ""),
    )
    u.setDataSource(
        p.get("active_schema", "public"), table.lower(), geom or None
    )
    u.setKeyColumn(key)
    if wkb:
        u.setWkbType(wkb)
    return u.uri(False)


def use_postgres_provider(qgs_path):
    """In the saved .qgs, layers read through GDAL from PostgreSQL ('PG:'
    strings) get the native PostgreSQL provider. Returns the number of
    layers switched."""
    tree = ET.parse(qgs_path)  # nosec B314 - the project we just saved
    root = tree.getroot()
    n = 0
    for ml in root.iter("maplayer"):
        prov = ml.find("provider")
        ds = ml.find("datasource")
        if prov is None or ds is None or (prov.text or "") != "ogr":
            continue
        src = ds.text or ""
        if not src.upper().startswith("PG:"):
            continue
        parts = src.split("|")
        table = None
        for part in parts[1:]:
            if part.lower().startswith("layername="):
                table = part.split("=", 1)[1]
        if not table:
            continue
        table = table.split(".")[-1]
        geom = (
            "geom"
            if ml.get("type") == "vector"
            and ml.get("geometry", "No geometry")
            not in ("No geometry", "Unknown geometry")
            else ""
        )
        ds.text = _pg_uri(parts[0], table, geom)
        prov.text = "postgres"
        n += 1
    tree.write(qgs_path, encoding="utf-8", xml_declaration=False)
    return n


# ------------------------------------------------------------------ triggers
def _pg_exec(cfg, sql_list):
    from osgeo import gdal

    base = S.split_domain(cfg.path)[0]
    ds = gdal.OpenEx(
        base, gdal.OF_VECTOR | gdal.OF_UPDATE, allowed_drivers=["PostgreSQL"]
    )
    if ds is None:
        raise S.StoreError("Could not connect to PostgreSQL.")
    try:
        for sql in sql_list:
            gdal.ErrorReset()
            r = ds.ExecuteSQL(sql)
            if r is not None:
                ds.ReleaseResultSet(r)
            if gdal.GetLastErrorType() >= 3:
                raise S.StoreError("PostgreSQL: %s" % gdal.GetLastErrorMsg())
    finally:
        ds = None


def _columns(cfg, table):
    ds = S.open_ds(cfg.path)
    try:
        lyr = ds.GetLayerByName(table)
        if lyr is None:
            return set()
        d = lyr.GetLayerDefn()
        return {
            d.GetFieldDefn(i).GetName().lower()
            for i in range(d.GetFieldCount())
        }
    finally:
        ds = None


def install_triggers(cfg):
    """Create / replace the tracking and dirty area triggers on every class
    (PostGIS only). Returns the number of tables."""
    if not S.is_pg(cfg.path):
        raise ValueError("Database triggers need PostgreSQL / PostGIS.")
    schema = S.ident(
        S.pg_parts(S.split_domain(cfg.path)[0]).get("active_schema", "public")
    )
    dirty = S.table_name(cfg.path, "un_dirty_areas").lower()
    gap = max(cfg.gap, 0.001)
    V.backfill_globalids(cfg.path, list(cfg.classes.values()))
    computed = ",".join("'%s'" % c for c in COMPUTED + ("globalid",))
    sql = []
    sql_setup, skipped, in_db = [], [], []
    n = 0
    for cls in cfg.classes.values():
        t = S.ident(cls.lower())
        cols = _columns(cfg, t)
        if not cols:
            continue
        net = [c for c in NETWORK_FIELDS if c in cols]
        changed_net = (
            " OR ".join("NEW.%s IS DISTINCT FROM OLD.%s" % (c, c) for c in net)
            or "FALSE"
        )
        track = []
        if "globalid" in cols:
            track.append(
                "IF NEW.globalid IS NULL OR NEW.globalid = '' THEN"
                " NEW.globalid := '{' || upper(gen_random_uuid()::text)"
                " || '}'; END IF;"
            )
        if "created_user" in cols:
            track.append(
                "IF TG_OP = 'INSERT' THEN NEW.created_user :="
                " coalesce(NEW.created_user, current_user);"
                " NEW.created_date := coalesce(NEW.created_date, now());"
                " END IF;"
            )
        if "last_edited_user" in cols:
            track.append(
                "IF TG_OP = 'INSERT' OR (to_jsonb(NEW) - ARRAY[%s]::text[])"
                " IS DISTINCT FROM (to_jsonb(OLD) - ARRAY[%s]::text[]) THEN"
                " NEW.last_edited_user := current_user;"
                " NEW.last_edited_date := now(); END IF;"
                % (computed, computed)
            )
        for rule in getattr(cfg, "attribute_rules", []):
            if (
                not rule.get("enabled")
                or rule.get("rule_type") != "calculation"
                or (rule.get("class_name") or "").lower() != t
            ):
                continue
            _RULE_CFG["cfg"] = cfg
            got = sql_rule(rule, cols, schema, t)
            if got is None:
                skipped.append(rule["name"])
                continue
            stmt, setup = got
            sql_setup.extend(setup)
            if "update" in (rule.get("triggers") or "insert"):
                track.append(stmt)
            else:
                track.append("IF TG_OP = 'INSERT' THEN %s END IF;" % stmt)
            in_db.append(rule["name"])
        fn_t = "ns_track_%s" % t
        fn_d = "ns_dirty_%s" % t
        sql.append(
            'CREATE OR REPLACE FUNCTION "%s".%s() RETURNS trigger AS $$'
            " BEGIN %s RETURN NEW; END $$ LANGUAGE plpgsql"
            % (schema, fn_t, " ".join(track))
        )
        sql.append(
            'CREATE OR REPLACE FUNCTION "%s".%s() RETURNS trigger'  # nosec
            " AS $$ BEGIN"
            " IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.geom IS NOT NULL"
            " AND (TG_OP = 'DELETE' OR NEW.geom IS DISTINCT FROM OLD.geom"
            " OR %s) THEN"
            ' INSERT INTO "%s"."%s" (geom, created, reason) VALUES'
            " (ST_Envelope(ST_Expand(OLD.geom, %r)), now()::text,"
            " 'database ' || lower(TG_OP));"
            " END IF;"
            " IF TG_OP IN ('INSERT', 'UPDATE') AND NEW.geom IS NOT NULL"
            " AND (TG_OP = 'INSERT' OR NEW.geom IS DISTINCT FROM OLD.geom"
            " OR %s) THEN"
            ' INSERT INTO "%s"."%s" (geom, created, reason) VALUES'
            " (ST_Envelope(ST_Expand(NEW.geom, %r)), now()::text,"
            " 'database ' || lower(TG_OP));"
            " END IF;"
            " RETURN NULL; END $$ LANGUAGE plpgsql"
            % (
                schema,
                fn_d,
                changed_net,
                schema,
                dirty,
                gap,
                changed_net,
                schema,
                dirty,
                gap,
            )
        )
        sql.append(
            'DROP TRIGGER IF EXISTS ns_track ON "%s"."%s"' % (schema, t)
        )
        sql.append(
            'DROP TRIGGER IF EXISTS ns_dirty ON "%s"."%s"' % (schema, t)
        )
        sql.append(
            'CREATE TRIGGER ns_track BEFORE INSERT OR UPDATE ON "%s"."%s"'
            ' FOR EACH ROW EXECUTE FUNCTION "%s".%s()'
            % (schema, t, schema, fn_t)
        )
        if cfg.topology_enabled:
            sql.append(
                "CREATE TRIGGER ns_dirty AFTER INSERT OR UPDATE OR DELETE"
                ' ON "%s"."%s" FOR EACH ROW EXECUTE FUNCTION "%s".%s()'
                % (schema, t, schema, fn_d)
            )
        n += 1
    _pg_exec(cfg, sql_setup + sql)
    S.set_setting(cfg.path, "db_triggers", "1")
    install_triggers.last = {
        "rules in the database": in_db,
        "rules not translated": skipped,
    }
    return n


def remove_triggers(cfg):
    if not S.is_pg(cfg.path):
        raise ValueError("Database triggers need PostgreSQL / PostGIS.")
    schema = S.ident(
        S.pg_parts(S.split_domain(cfg.path)[0]).get("active_schema", "public")
    )
    sql = []
    for cls in cfg.classes.values():
        t = S.ident(cls.lower())
        sql.append(
            'DROP TRIGGER IF EXISTS ns_track ON "%s"."%s"' % (schema, t)
        )
        sql.append(
            'DROP TRIGGER IF EXISTS ns_dirty ON "%s"."%s"' % (schema, t)
        )
        sql.append('DROP FUNCTION IF EXISTS "%s".ns_track_%s()' % (schema, t))
        sql.append('DROP FUNCTION IF EXISTS "%s".ns_dirty_%s()' % (schema, t))
    _pg_exec(cfg, sql)
    S.set_setting(cfg.path, "db_triggers", "0")
    return len(cfg.classes)


# ------------------------------------------------------------------ external
def _states_with_boxes(path, classes):
    ds = S.open_ds(S.split_domain(path)[0])
    out = {}
    try:
        for cls in classes:
            lyr = ds.GetLayerByName(cls)
            if lyr is None:
                continue
            gi = V._gid_index(lyr)
            names = V._names(lyr)
            srs = lyr.GetSpatialRef()
            digits = 8 if srs is not None and srs.IsGeographic() else 3
            lyr.ResetReading()
            for f in lyr:
                g = V._norm_gid(f.GetField(gi)) if gi >= 0 else ""
                if not g:
                    continue
                geom = f.GetGeometryRef()
                env = geom.GetEnvelope() if geom is not None else None
                out[(cls, g)] = (
                    f.GetFID(),
                    V._hash(f, names, digits),
                    env,
                )
    finally:
        ds = None
    return out


def _save_state(path, states):
    ds = S.open_ds(path, update=True)
    try:
        rows = []
        for (cls, g), (_fid, h, env) in states.items():
            r = {"class_name": cls, "globalid": g, "hash": h}
            if env:
                r.update(
                    {
                        "minx": env[0],
                        "maxx": env[1],
                        "miny": env[2],
                        "maxy": env[3],
                    }
                )
            rows.append(r)
        V._replace_table(ds, STATE, STATE_FIELDS, rows)
    finally:
        ds = None


def _read_state(path):
    ds = S.open_ds(path)
    try:
        if ds.GetLayerByName(S._tn(ds, STATE)) is None:
            return None
        rows = S._read_rows(ds, STATE)
    finally:
        ds = None
    return {
        (r["class_name"], r["globalid"]): (
            r["hash"],
            (r.get("minx"), r.get("maxx"), r.get("miny"), r.get("maxy")),
        )
        for r in rows
    }


def check_external_edits(cfg, write=True):
    """Features added / changed / deleted since the previous check (or since
    the state was first saved). Marks dirty areas around them, records them
    in the edit history and saves the new state.

    Returns {"first": bool, "insert": n, "update": n, "delete": n,
    "areas": n}."""
    from qgis.core import QgsRectangle

    from .qgis_io import write_dirty_areas

    classes = list(cfg.classes.values())
    V.backfill_globalids(cfg.path, classes)
    now_states = _states_with_boxes(cfg.path, classes)
    old = _read_state(cfg.path)
    out = {
        "first": old is None,
        "insert": 0,
        "update": 0,
        "delete": 0,
        "areas": 0,
        "changed": {},
        "rects": [],
    }
    if old is None:
        if write:
            _save_state(cfg.path, now_states)
        return out
    rects, hist = [], []
    gap = max(cfg.gap, 0.001)

    def rect(env):
        if not env or env[0] is None:
            return None
        r = QgsRectangle(env[0], env[2], env[1], env[3])
        r.grow(gap)
        return r

    for k in set(old) | set(now_states):
        o = old.get(k)
        n = now_states.get(k)
        if o and n and o[0] == n[1]:
            continue
        kind = "insert" if o is None else ("delete" if n is None else "update")
        out[kind] += 1
        for env in ((o[1] if o else None), (n[2] if n else None)):
            r = rect(env)
            if r is not None:
                rects.append(r)
        hist.append((k, kind, n[0] if n else None))
        if n is not None:
            out["changed"].setdefault(k[0], []).append(n[0])
    if write:
        if rects and cfg.topology_enabled:
            if len(rects) > 200:
                box = rects[0]
                for r in rects[1:]:
                    box.combineExtentWith(r)
                rects = [box]
            write_dirty_areas(cfg, rects, reason="external edit")
            out["areas"] = len(rects)
        _history(cfg, hist)
        _save_state(cfg.path, now_states)
    out["rects"] = rects
    return out


def process_external_edits(cfg, apply_rules=True):
    """Check for edits made outside QGIS (web, QField, other programs), mark
    dirty areas and run the attribute rules on the features added or
    changed there: calculation rules fill their fields, constraint and
    validation rules go to the error layers. Returns a report dict."""
    from . import attribute_rules as AR
    from .qgis_io import class_layers, write_errors

    res = check_external_edits(cfg)
    res["rules: values filled"] = 0
    res["rules: broken"] = 0
    rules = [
        r for r in getattr(cfg, "attribute_rules", []) if r.get("enabled")
    ]
    if apply_rules and rules and res["changed"]:
        layers = class_layers(cfg)
        AR.register_layers(cfg, layers)
        issues, changed, _errors = AR.evaluate(
            cfg, layers, rules, only=res["changed"]
        )
        kinds = {r["name"]: r["rule_type"] for r in rules}
        res["rules: values filled"] = sum(
            n
            for name, n in changed.items()
            if kinds.get(name) == "calculation"
        )
        res["rules: broken"] = len(issues)
        if issues or res["rects"]:
            write_errors(cfg, issues, res["rects"] or None, codes=("AR",))
        if res["rules: values filled"]:
            reset_external_state(cfg)  # our own changes are not outside edits
    return res


# ------------------------------------------------------------------ SQL rules
def sql_rule(rule, cols, schema, table):
    """A calculation rule as a PL/pgSQL assignment, for the patterns that
    have an exact SQL equivalent; None for the others.
    Returns (statement, setup SQL list)."""
    import re

    field = (rule.get("field_name") or "").lower()
    expr = (rule.get("expression") or "").strip()
    if not field or field not in cols:
        return None
    m = re.fullmatch(r"round\(\s*\$length\s*,\s*(\d+)\s*\)", expr)
    if m:
        return (
            "NEW.%s := round(ST_Length(NEW.geom)::numeric, %d);"
            % (S.ident(field), int(m.group(1))),
            [],
        )
    if expr == "$length":
        return ("NEW.%s := ST_Length(NEW.geom);" % S.ident(field), [])
    if expr == "$area":
        return ("NEW.%s := ST_Area(NEW.geom);" % S.ident(field), [])
    m = re.fullmatch(
        r"coalesce\(\s*\"?(\w+)\"?\s*,\s*ns_next_id\(\s*'([^']*)'"
        r"\s*(?:,\s*(\d+)\s*)?\)\s*\)",
        expr,
        flags=re.I,
    )
    if m and m.group(1).lower() == field:
        prefix = m.group(2)
        width = int(m.group(3) or 6)
        if not re.fullmatch(r"[A-Za-z0-9_\-./ ]*", prefix):
            return None
        seq = S.ident(("ns_seq_%s_%s" % (table, field))[:60])
        top = _max_suffix(rule, prefix, field)
        setup = [
            'CREATE SEQUENCE IF NOT EXISTS "%s"."%s"' % (schema, seq),
            'SELECT setval(\'"%s"."%s"\', %d, %s)'
            % (schema, seq, max(top, 1), "true" if top > 0 else "false"),
        ]
        return (
            "IF NEW.%s IS NULL OR NEW.%s = '' THEN NEW.%s := '%s' ||"
            " lpad(nextval('\"%s\".\"%s\"')::text, %d, '0'); END IF;"
            % (field, field, field, prefix, schema, seq, width),
            setup,
        )
    return None


_RULE_CFG = {}


def _max_suffix(rule, prefix, field):
    import re

    cfg = _RULE_CFG.get("cfg")
    if cfg is None:
        return 0
    pat = re.compile(r"^%s(\d+)$" % re.escape(prefix))
    top = 0
    ds = S.open_ds(cfg.path)
    try:
        lyr = ds.GetLayerByName(rule["class_name"])
        if lyr is None:
            return 0
        lyr.ResetReading()
        for f in lyr:
            m = pat.match(str(f.GetField(field) or ""))
            if m:
                top = max(top, int(m.group(1)))
    finally:
        ds = None
    return top


def _history(cfg, items):
    if not items:
        return
    now = datetime.now().isoformat(timespec="seconds")
    ds = S.open_ds(cfg.path, update=True)
    try:
        lyr = S._make_table(ds, "un_history", S.SYSTEM_TABLES["un_history"])
        tx = V._begin(ds)
        for (cls, g), kind, fid in items:
            f = ogr.Feature(lyr.GetLayerDefn())
            f.SetField("changed_at", now)
            f.SetField("user_name", "outside QGIS")
            f.SetField("class_name", cls)
            if fid is not None:
                f.SetField("feature_fid", fid)
            f.SetField("action", "external " + kind)
            f.SetField("new_value", "{%s}" % g)
            lyr.CreateFeature(f)
        V._commit(ds, tx)
    finally:
        ds = None


def reset_external_state(cfg):
    """Save the current state as the reference (e.g. after a validation)."""
    classes = list(cfg.classes.values())
    V.backfill_globalids(cfg.path, classes)
    _save_state(cfg.path, _states_with_boxes(cfg.path, classes))
