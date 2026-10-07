"""Branch versions and design projects (long transactions).

A version is a complete copy of the network taken at one moment:
    GeoPackage / SpatiaLite  <file>_versions/<name>.gpkg (.sqlite)
    File Geodatabase         <file>_versions/<name>.gdb
    PostGIS                  schema <schema>_v_<name> in the same database
so every tool of the plugin (editing, validation, traces, reports, cost...)
works in a version exactly as in Default.

Features are matched by globalid. When a version is created the state of
every feature (a hash of its geometry and attributes) is stored inside the
version (un_version_base). From that base:
    changed in the version  = version state  != base
    changed in Default      = Default state  != base
    conflict                = changed on both sides, differently
Reconcile pulls the Default changes into the version (conflicts are resolved
in favour of the version or of Default, or one by one) and moves the base
to the current Default. Post pushes the version changes into Default; it
needs a version reconciled with the current Default. Associations and
subnetwork controllers are versioned too.

The list of versions (un_versions) lives in Default, together with the
design project information: description, owner, status (Design, In review,
Approved, Posted, Abandoned), access and work order.
"""

import getpass
import hashlib
import os
import re
import shutil
import uuid
from datetime import datetime

from osgeo import ogr

from . import storage as S

STATUSES = ["Design", "In review", "Approved", "Posted", "Abandoned"]
ACCESS = ["Public", "Protected", "Private"]
IGNORE = {
    "fid",
    "objectid",
    "globalid",
    "last_edited_user",
    "last_edited_date",
    "isconnected",
    "subnetwork",
}
REGISTRY = "un_versions"
REGISTRY_FIELDS = [
    ("name", ogr.OFTString),
    ("description", ogr.OFTString),
    ("owner", ogr.OFTString),
    ("status", ogr.OFTString),
    ("access", ogr.OFTString),
    ("work_order", ogr.OFTString),
    ("location", ogr.OFTString),
    ("created", ogr.OFTString),
    ("modified", ogr.OFTString),
    ("reconciled", ogr.OFTString),
    ("posted", ogr.OFTString),
    ("posted_counts", ogr.OFTString),
    ("parent", ogr.OFTString),
]
INFO = "un_version_info"
BASE = "un_version_base"
BASE_FIELDS = [
    ("class_name", ogr.OFTString),
    ("globalid", ogr.OFTString),
    ("hash", ogr.OFTString),
    ("fields", ogr.OFTString),
]
ROW_TABLES = {  # versioned system rows: (table, fid fields)
    "un_associations": (("from_class", "from_fid"), ("to_class", "to_fid")),
    "un_controllers": (("class_name", "feature_fid"),),
}


class VersionError(S.StoreError):
    pass


def current_user():
    try:
        return getpass.getuser()
    except Exception:  # nosec B110 - no user name on this system
        return ""


def _now():
    return datetime.now().isoformat(timespec="seconds")


def slug(name):
    s = re.sub(r"[^A-Za-z0-9_]+", "_", (name or "").strip()).strip("_")
    if not s:
        raise VersionError("Give the version a name (letters and digits).")
    return s.lower()[:40]


# ------------------------------------------------------------------ paths
def _strip_ns(path):
    return S.split_domain(path)


def info(path):
    """{key: value} of un_version_info when `path` is a version, else {}."""
    ds = S.open_ds(_strip_ns(path)[0])
    try:
        lyr = ds.GetLayerByName(INFO)
        if lyr is None:
            return {}
        return {r["key"]: r["value"] for r in S._read_rows(ds, INFO)}
    finally:
        ds = None


def is_version(path):
    return bool(info(path).get("version"))


def default_path(path):
    """Path of Default for `path` (itself when it is Default)."""
    inf = info(path)
    if not inf.get("version"):
        return path
    base, ns = _strip_ns(path)
    parent = inf.get("parent", "")
    if not S.is_pg(parent) and not os.path.isabs(parent):
        parent = os.path.normpath(
            os.path.join(os.path.dirname(base.rstrip("/\\")), parent)
        )
    return parent + ("#%s" % ns if ns else "")


def parent_path(path):
    """The version or Default a version was made from."""
    return default_path(path)


def root_path(path):
    """Default at the top of the version tree of `path`."""
    p = path
    for _i in range(50):
        if not info(p).get("version"):
            return p
        p = default_path(p)
    raise VersionError("The version tree is broken (a version is missing).")


def versions_dir(path):
    base = _strip_ns(path)[0].rstrip("/\\")
    return base + "_versions"


def _location(default, name):
    """(storage location, path to open) of a new version."""
    base, ns = _strip_ns(root_path(default))
    sfx = "#%s" % ns if ns else ""
    if S.is_pg(base):
        parts = S.pg_parts(base)
        schema = "%s_v_%s" % (parts.get("active_schema", "public"), name)
        schema = schema.lower()[:63]
        p = dict(parts)
        p["active_schema"] = schema
        conn = "PG:" + " ".join("%s=%s" % (k, v) for k, v in p.items())
        return schema, conn + sfx
    folder = versions_dir(base)
    ext = os.path.splitext(base.rstrip("/\\"))[1]
    fname = name + ext
    return (
        os.path.join(os.path.basename(folder), fname),
        os.path.join(folder, fname) + sfx,
    )


def version_path(default, row):
    """Path to open the version described by a registry row."""
    base, ns = _strip_ns(root_path(default))
    sfx = "#%s" % ns if ns else ""
    loc = row.get("location") or ""
    if S.is_pg(base):
        p = dict(S.pg_parts(base))
        p["active_schema"] = loc
        return "PG:" + " ".join("%s=%s" % (k, v) for k, v in p.items()) + sfx
    return (
        os.path.normpath(
            os.path.join(os.path.dirname(base.rstrip("/\\")), loc)
        )
        + sfx
    )


# ------------------------------------------------------------------ registry
def _ensure_registry(ds):
    lyr = S._make_table(ds, REGISTRY, REGISTRY_FIELDS)
    _add_missing_fields(lyr, REGISTRY_FIELDS)
    return lyr


def _add_missing_fields(lyr, fields):
    d = lyr.GetLayerDefn()
    have = {
        d.GetFieldDefn(i).GetName().lower() for i in range(d.GetFieldCount())
    }
    for name, ftype in fields:
        if name.lower() not in have:
            lyr.CreateField(ogr.FieldDefn(name, ftype))


def list_versions(default, user=None):
    """Registry rows; private versions of other users are left out when
    `user` is given."""
    base = _strip_ns(root_path(default))[0]
    ds = S.open_ds(base)
    try:
        rows = S._read_rows(ds, REGISTRY)
    finally:
        ds = None
    if user is not None:
        rows = [
            r
            for r in rows
            if r.get("access") != "Private" or r.get("owner") == user
        ]
    return sorted(rows, key=lambda r: r.get("created") or "")


def get_version(default, name):
    for r in list_versions(default):
        if r["name"] == name:
            return r
    raise VersionError("There is no version named %s." % name)


def _update_registry(default, name, **values):
    base = _strip_ns(root_path(default))[0]
    ds = S.open_ds(base, update=True)
    try:
        _ensure_registry(ds)
        rows = S._read_rows(ds, REGISTRY)
        for r in rows:
            if r["name"] == name:
                r.update(values)
                r["modified"] = _now()
        S._rewrite(ds, REGISTRY, REGISTRY_FIELDS, rows)
    finally:
        ds = None


def set_status(default, name, status):
    if status not in STATUSES:
        raise VersionError("Unknown status %s." % status)
    _update_registry(default, name, status=status)


def update_details(default, name, **values):
    keep = {
        k: v
        for k, v in values.items()
        if k in ("description", "access", "work_order", "owner")
    }
    _update_registry(default, name, **keep)


# ------------------------------------------------------------------ reading
def _round_wkt(wkt, digits=3):
    fmt = "%%.%df" % digits
    return re.sub(
        r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?",
        lambda m: fmt % float(m.group(0)),
        wkt,
    )


def _hash(feat, names, digits=3):
    h = hashlib.sha1()  # nosec B324 - change detection, not security
    g = feat.GetGeometryRef()
    h.update(_round_wkt(g.ExportToIsoWkt(), digits).encode() if g else b"-")
    for i, n in names:
        if n.lower() in IGNORE or not feat.IsFieldSetAndNotNull(i):
            continue
        v = feat.GetField(i)
        if isinstance(v, float):
            v = round(v, 6)
        h.update(("|%s=%s" % (n.lower(), v)).encode())
    return h.hexdigest()


def _field_hashes(feat, names, digits=3):
    """{field: short hash} of one feature, '@shape' for the geometry."""
    out = {}
    g = feat.GetGeometryRef()
    out["@shape"] = hashlib.sha1(  # nosec B324 - change detection
        _round_wkt(g.ExportToIsoWkt(), digits).encode() if g else b"-"
    ).hexdigest()[:10]
    for i, n in names:
        if n.lower() in IGNORE:
            continue
        v = feat.GetField(i) if feat.IsFieldSetAndNotNull(i) else None
        if isinstance(v, float):
            v = round(v, 6)
        out[n.lower()] = hashlib.sha1(  # nosec B324 - change detection
            repr(v).encode()
        ).hexdigest()[:10]
    return out


def _names(lyr):
    d = lyr.GetLayerDefn()
    return [(i, d.GetFieldDefn(i).GetName()) for i in range(d.GetFieldCount())]


def _gid_index(lyr):
    d = lyr.GetLayerDefn()
    for i in range(d.GetFieldCount()):
        if d.GetFieldDefn(i).GetName().lower() == "globalid":
            return i
    return -1


def new_gid():
    return "{%s}" % str(uuid.uuid4()).upper()


def _norm_gid(v):
    return str(v or "").strip("{}").upper()


def backfill_globalids(path, classes):
    """Give every feature without a globalid a new one. Returns the count."""
    ds = S.open_ds(_strip_ns(path)[0], update=True)
    n = 0
    try:
        for cls in classes:
            lyr = ds.GetLayerByName(cls)
            if lyr is None:
                continue
            gi = _gid_index(lyr)
            if gi < 0:
                fd = ogr.FieldDefn("globalid", ogr.OFTString)
                fd.SetWidth(38)
                lyr.CreateField(fd)
                gi = _gid_index(lyr)
            todo = []
            lyr.ResetReading()
            seen = set()
            for f in lyr:
                g = (
                    _norm_gid(f.GetField(gi))
                    if f.IsFieldSetAndNotNull(gi)
                    else ""
                )
                if not g or g in seen:  # missing or copied feature
                    todo.append(f.GetFID())
                else:
                    seen.add(g)
            if not todo:
                continue
            tx = _begin(ds)
            for fid in todo:
                f = lyr.GetFeature(fid)
                f.SetField(gi, new_gid())
                lyr.SetFeature(f)
                n += 1
            _commit(ds, tx)
    finally:
        ds = None
    return n


def _begin(ds):
    if _driver(ds) == "OpenFileGDB":
        return False
    try:
        return ds.StartTransaction() == 0
    except Exception:  # driver without transactions
        return False


def _commit(ds, tx):
    if tx:
        ds.CommitTransaction()


def read_states(path, classes, with_fields=False):
    """{(class, gid): (fid, hash[, field hashes])} of the network features
    in `path`."""
    ds = S.open_ds(_strip_ns(path)[0])
    out = {}
    try:
        for cls in classes:
            lyr = ds.GetLayerByName(cls)
            if lyr is None:
                continue
            gi = _gid_index(lyr)
            names = _names(lyr)
            srs = lyr.GetSpatialRef()
            digits = 8 if srs is not None and srs.IsGeographic() else 3
            lyr.ResetReading()
            for f in lyr:
                g = _norm_gid(f.GetField(gi)) if gi >= 0 else ""
                if not g:
                    continue
                if with_fields:
                    out[(cls, g)] = (
                        f.GetFID(),
                        _hash(f, names, digits),
                        _field_hashes(f, names, digits),
                    )
                else:
                    out[(cls, g)] = (f.GetFID(), _hash(f, names, digits))
    finally:
        ds = None
    return out


def _fid_maps(states):
    to_gid, to_fid = {}, {}
    for (cls, g), st in states.items():
        fid = st[0]
        to_gid[(cls, fid)] = g
        to_fid[(cls, g)] = fid
    return to_gid, to_fid


def read_rowsets(path, states):
    """Associations / controllers as sets of tuples with globalids instead of
    fids."""
    to_gid, _f = _fid_maps(states)
    ds = S.open_ds(path)
    out = set()
    try:
        for table, refs in ROW_TABLES.items():
            for r in S._read_rows(ds, table):
                key = [table]
                ok = True
                for cfield, ffield in refs:
                    g = to_gid.get((r.get(cfield), r.get(ffield)))
                    if g is None:
                        ok = False
                    key += [r.get(cfield), g]
                if not ok:
                    continue
                for k in sorted(r):
                    if k not in {x for pair in refs for x in pair}:
                        key += [k, r[k]]
                out.add(tuple(key))
    finally:
        ds = None
    return out


def _write_rowsets(path, rowsets, states):
    _g, to_fid = _fid_maps(states)
    by_table = {t: [] for t in ROW_TABLES}
    for key in rowsets:
        table = key[0]
        refs = ROW_TABLES[table]
        row = {}
        pos = 1
        ok = True
        for cfield, ffield in refs:
            cls, g = key[pos], key[pos + 1]
            pos += 2
            fid = to_fid.get((cls, g))
            if fid is None:
                ok = False
            row[cfield], row[ffield] = cls, fid
        rest = key[pos:]
        for i in range(0, len(rest), 2):
            row[rest[i]] = rest[i + 1]
        if ok:
            by_table[table].append(row)
    ds = S.open_ds(path, update=True)
    try:
        for table, rows in by_table.items():
            S._rewrite(ds, table, S.SYSTEM_TABLES[table], rows)
    finally:
        ds = None


# ------------------------------------------------------------------ base
def _driver(ds):
    drv = ds.GetDriver()
    return getattr(drv, "ShortName", None) or drv.GetName()


def _replace_table(ds, name, fields, rows):
    """Empty a table with one statement (much faster than deleting row by
    row), then write `rows`."""
    tname = S._tn(ds, name)
    lyr = ds.GetLayerByName(tname)
    if lyr is not None:
        if _driver(ds) in ("GPKG", "SQLite", "PostgreSQL"):
            ds.ExecuteSQL('DELETE FROM "%s"' % S.ident(tname))  # nosec B608
        else:
            for i in range(ds.GetLayerCount()):
                if ds.GetLayerByIndex(i).GetName().lower() == tname.lower():
                    ds.DeleteLayer(i)
                    break
    lyr = S._make_table(ds, name, fields)
    _add_missing_fields(lyr, fields)
    if lyr.GetFeatureCount() > 0:  # could not be emptied: row by row
        lyr.ResetReading()
        for fid in [f.GetFID() for f in lyr]:
            lyr.DeleteFeature(fid)
    tx = _begin(ds)
    defn = lyr.GetLayerDefn()
    for r in rows:
        f = ogr.Feature(defn)
        for k, v in r.items():
            if v is not None:
                f.SetField(k, v)
        lyr.CreateFeature(f)
    _commit(ds, tx)


def _write_base(vpath, states, rowsets, extra=None):
    ds = S.open_ds(vpath, update=True)
    try:
        import json

        rows = [
            {
                "class_name": cls,
                "globalid": g,
                "hash": st[1],
                "fields": (
                    json.dumps(st[2], separators=(",", ":"))
                    if len(st) > 2
                    else None
                ),
            }
            for (cls, g), st in states.items()
        ]
        rows += [
            {"class_name": "__rows__", "globalid": _key_text(k), "hash": ""}
            for k in rowsets
        ]
        _replace_table(ds, BASE, BASE_FIELDS, rows)
    finally:
        ds = None
    ds = S.open_ds(_strip_ns(vpath)[0], update=True)
    try:
        if extra:
            inf = {r["key"]: r["value"] for r in S._read_rows(ds, INFO)}
            inf.update(extra)
            S._rewrite(
                ds,
                INFO,
                S.SYSTEM_TABLES["un_network"],
                [{"key": k, "value": v} for k, v in inf.items()],
            )
    finally:
        ds = None


def _key_text(key):
    import json

    return json.dumps(list(key), default=str)


def _text_key(text):
    import json

    return tuple(json.loads(text))


def read_base(vpath, with_fields=False):
    import json

    ds = S.open_ds(vpath)
    try:
        rows = S._read_rows(ds, BASE)
    finally:
        ds = None
    states, rowsets, fields = {}, set(), {}
    for r in rows:
        if r["class_name"] == "__rows__":
            rowsets.add(_text_key(r["globalid"]))
        else:
            states[(r["class_name"], r["globalid"])] = r["hash"]
            if with_fields and r.get("fields"):
                fields[(r["class_name"], r["globalid"])] = json.loads(
                    r["fields"]
                )
    if with_fields:
        return states, rowsets, fields
    return states, rowsets


# ------------------------------------------------------------------ create
def _copy_container(src, dst):
    if S.is_pg(src):
        _copy_schema(src, dst)
        return
    if os.path.exists(dst):
        raise VersionError("The version data already exists: %s" % dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.lock"))
        return
    # VACUUM INTO through GDAL: a consistent copy (with the -wal content)
    # made by the same SQLite library QGIS uses for the open layers
    from osgeo import gdal

    ds = gdal.OpenEx(src, gdal.OF_VECTOR)
    if ds is None:
        raise VersionError("Could not open %s" % src)
    try:
        res = ds.ExecuteSQL("VACUUM INTO '%s'" % dst.replace("'", "''"))
        if res is not None:
            ds.ReleaseResultSet(res)
    finally:
        ds = None
    if not os.path.exists(dst):
        raise VersionError(
            "Could not copy the network: %s" % gdal.GetLastErrorMsg()
        )
    # same journal mode as a network QGIS has edited (WAL): read-only layers
    # opened before the first edit keep working after it
    ds = gdal.OpenEx(dst, gdal.OF_VECTOR | gdal.OF_UPDATE)
    if ds is not None:
        res = ds.ExecuteSQL("PRAGMA journal_mode = WAL")
        if res is not None:
            ds.ReleaseResultSet(res)
        ds = None


def _pg_tables(ds, schema):
    sql = ds.ExecuteSQL(
        "SELECT tablename FROM pg_tables WHERE schemaname = '%s'"  # nosec B608
        % S.ident(schema)
    )
    names = []
    if sql is not None:
        for f in sql:
            names.append(f.GetField(0))
        ds.ReleaseResultSet(sql)
    return names


def _copy_schema(src, dst):
    from osgeo import gdal

    s_schema = S.pg_parts(src).get("active_schema", "public")
    d_schema = S.pg_parts(dst).get("active_schema")
    ds = gdal.OpenEx(
        src, gdal.OF_VECTOR | gdal.OF_UPDATE, allowed_drivers=["PostgreSQL"]
    )
    if ds is None:
        raise VersionError("Could not connect to PostgreSQL.")
    try:
        if _pg_tables(ds, d_schema):
            raise VersionError("The schema %s already exists." % d_schema)
        ds.ExecuteSQL('CREATE SCHEMA IF NOT EXISTS "%s"' % S.ident(d_schema))
        for t in _pg_tables(ds, s_schema):
            ds.ExecuteSQL(
                'CREATE TABLE "%s"."%s" (LIKE "%s"."%s" INCLUDING ALL)'
                % tuple(S.ident(x) for x in (d_schema, t, s_schema, t))
            )
            ds.ExecuteSQL(
                'INSERT INTO "%s"."%s" SELECT * FROM "%s"."%s"'  # nosec B608
                % tuple(S.ident(x) for x in (d_schema, t, s_schema, t))
            )
    finally:
        ds = None


def create_version(
    default,
    name,
    classes,
    description="",
    access="Public",
    work_order="",
    owner=None,
):
    """Copy Default (or a version: the new version is its child) into a new
    version. Returns the path to open it."""
    parent_name = info(default).get("version") or ""
    name_s = slug(name)
    if any(r["name"] == name_s for r in list_versions(default)):
        raise VersionError("A version named %s already exists." % name_s)
    backfill_globalids(default, classes)
    loc, vpath = _location(default, name_s)
    base, ns = _strip_ns(default)
    _copy_container(base, _strip_ns(vpath)[0])
    try:
        states = read_states(default, classes, with_fields=True)
        rowsets = read_rowsets(default, states)
        vds = S.open_ds(_strip_ns(vpath)[0], update=True)
        try:
            S._make_table(vds, INFO, S.SYSTEM_TABLES["un_network"])
            lyr = vds.GetLayerByName(REGISTRY)
            if lyr is not None:  # the copy is not the registry
                S._rewrite(vds, REGISTRY, REGISTRY_FIELDS, [])
        finally:
            vds = None
        parent = base
        if not S.is_pg(base):
            parent = os.path.relpath(
                base.rstrip("/\\"),
                os.path.dirname(_strip_ns(vpath)[0].rstrip("/\\")),
            )
        _write_base(
            vpath,
            {k: v for k, v in states.items()},
            rowsets,
            {"version": name_s, "parent": parent, "base_time": _now()},
        )
    except Exception:
        _drop_container(_strip_ns(vpath)[0])
        raise
    ds = S.open_ds(_strip_ns(root_path(default))[0], update=True)
    try:
        _ensure_registry(ds)
        rows = S._read_rows(ds, REGISTRY)
        rows.append(
            {
                "name": name_s,
                "description": description,
                "owner": owner if owner is not None else current_user(),
                "status": "Design",
                "access": access if access in ACCESS else "Public",
                "work_order": work_order,
                "location": loc,
                "created": _now(),
                "modified": _now(),
                "parent": parent_name,
            }
        )
        S._rewrite(ds, REGISTRY, REGISTRY_FIELDS, rows)
    finally:
        ds = None
    return vpath


def _drop_container(path):
    if S.is_pg(path):
        from osgeo import gdal

        schema = S.pg_parts(path).get("active_schema")
        p = dict(S.pg_parts(path))
        p.pop("active_schema", None)
        ds = gdal.OpenEx(
            "PG:" + " ".join("%s=%s" % (k, v) for k, v in p.items()),
            gdal.OF_VECTOR | gdal.OF_UPDATE,
            allowed_drivers=["PostgreSQL"],
        )
        if ds is not None:
            ds.ExecuteSQL(
                'DROP SCHEMA IF EXISTS "%s" CASCADE' % S.ident(schema)
            )
            ds = None
        return
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        for extra in ("", "-wal", "-shm", "-journal"):
            if os.path.exists(path + extra):
                os.remove(path + extra)


def delete_version(default, name):
    row = get_version(default, name)
    kids = [
        r["name"] for r in list_versions(default) if r.get("parent") == name
    ]
    if kids:
        raise VersionError(
            "Delete the versions made from %s first: %s"
            % (name, ", ".join(kids))
        )
    vpath = version_path(default, row)
    _drop_container(_strip_ns(vpath)[0])
    base = _strip_ns(root_path(default))[0]
    ds = S.open_ds(base, update=True)
    try:
        rows = [r for r in S._read_rows(ds, REGISTRY) if r["name"] != name]
        S._rewrite(ds, REGISTRY, REGISTRY_FIELDS, rows)
    finally:
        ds = None


# ------------------------------------------------------------------ diff
class Diff:
    """Three-way comparison of a version, its base and its parent (Default
    or the version it was made from).

    Features changed on both sides are merged field by field: fields
    changed on one side only are taken from that side; only fields changed
    differently on both sides are conflicts."""

    def __init__(self, base, ver, dft, rb, rv, rd, base_fields=None):
        self.base, self.ver, self.dft = base, ver, dft
        self.rows_base, self.rows_ver, self.rows_dft = rb, rv, rd
        self.version_changes = {}  # (cls, gid) -> insert / update / delete
        self.default_changes = {}
        self.conflicts = {}  # (cls, gid) -> (version change, parent change)
        self.conflict_fields = {}  # (cls, gid) -> [fields changed on both]
        self.merge_fields = {}  # (cls, gid) -> [parent fields to take]
        base_fields = base_fields or {}
        for k in set(base) | set(ver) | set(dft):
            b = base.get(k)
            v = ver[k][1] if k in ver else None
            d = dft[k][1] if k in dft else None
            vc = _kind(b, v)
            dc = _kind(b, d)
            if vc:
                self.version_changes[k] = vc
            if dc:
                self.default_changes[k] = dc
            if not (vc and dc and v != d):
                continue
            bf = base_fields.get(k)
            if (
                vc == "update"
                and dc == "update"
                and bf
                and len(ver[k]) > 2
                and len(dft[k]) > 2
            ):
                vf, df = ver[k][2], dft[k][2]
                names = set(bf) | set(vf) | set(df)
                v_ch = {n for n in names if vf.get(n) != bf.get(n)}
                d_ch = {n for n in names if df.get(n) != bf.get(n)}
                both = sorted(n for n in v_ch & d_ch if vf.get(n) != df.get(n))
                self.merge_fields[k] = sorted(d_ch - v_ch)
                if both:
                    self.conflicts[k] = (vc, dc)
                    self.conflict_fields[k] = both
            else:
                self.conflicts[k] = (vc, dc)
        self.rows_version_added = rv - rb
        self.rows_version_removed = rb - rv
        self.rows_default_added = rd - rb
        self.rows_default_removed = rb - rd

    def counts(self):
        def tally(changes):
            out = {"insert": 0, "update": 0, "delete": 0}
            for v in changes.values():
                out[v] += 1
            return out

        return {
            "version": tally(self.version_changes),
            "parent": tally(self.default_changes),
            "conflicts": len(self.conflicts),
            "merged field by field": sum(
                1 for k in self.merge_fields if k not in self.conflicts
            ),
            "version rows": (
                len(self.rows_version_added) + len(self.rows_version_removed)
            ),
            "parent rows": (
                len(self.rows_default_added) + len(self.rows_default_removed)
            ),
        }

    def rows(self):
        """[(class, gid, version change, parent change, conflict, fields)]"""
        out = []
        for k in sorted(set(self.version_changes) | set(self.default_changes)):
            out.append(
                (
                    k[0],
                    k[1],
                    self.version_changes.get(k, ""),
                    self.default_changes.get(k, ""),
                    k in self.conflicts,
                    ", ".join(self.conflict_fields.get(k, [])),
                )
            )
        return out


def _kind(base_hash, new_hash):
    if base_hash == new_hash:
        return ""
    if base_hash is None:
        return "insert"
    if new_hash is None:
        return "delete"
    return "update"


def diff(vpath, classes):
    dpath = default_path(vpath)
    backfill_globalids(vpath, classes)
    backfill_globalids(dpath, classes)
    base, rb, bfields = read_base(vpath, with_fields=True)
    ver = read_states(vpath, classes, with_fields=True)
    dft = read_states(dpath, classes, with_fields=True)
    rv = read_rowsets(vpath, ver)
    rd = read_rowsets(dpath, dft)
    return Diff(base, ver, dft, rb, rv, rd, bfields)


# ------------------------------------------------------------------ apply
def _copy_features(src_path, dst_path, keys, src_states, dst_states):
    """Make the features `keys` of dst equal to src (insert, update or
    delete). Returns counts."""
    counts = {"insert": 0, "update": 0, "delete": 0}
    if not keys:
        return counts
    sds = S.open_ds(_strip_ns(src_path)[0])
    dds = S.open_ds(_strip_ns(dst_path)[0], update=True)
    try:
        by_cls = {}
        for k in keys:
            by_cls.setdefault(k[0], []).append(k)
        for cls, ks in by_cls.items():
            sl = sds.GetLayerByName(cls)
            dl = dds.GetLayerByName(cls)
            if dl is None or sl is None:
                continue
            ddef = dl.GetLayerDefn()
            dnames = {
                ddef.GetFieldDefn(i).GetName().lower(): i
                for i in range(ddef.GetFieldCount())
            }
            gi = _gid_index(dl)
            tx = _begin(dds)
            for k in ks:
                src = src_states.get(k)
                dst = dst_states.get(k)
                if src is None:
                    if dst is not None:
                        dl.DeleteFeature(dst[0])
                        counts["delete"] += 1
                    continue
                sf = sl.GetFeature(src[0])
                if sf is None:
                    continue
                if dst is not None:
                    df = dl.GetFeature(dst[0])
                    kind = "update"
                else:
                    df = ogr.Feature(ddef)
                    kind = "insert"
                for i, n in _names(sl):
                    j = dnames.get(n.lower())
                    if j is None or n.lower() in ("fid", "objectid"):
                        continue
                    if sf.IsFieldSetAndNotNull(i):
                        df.SetField(j, sf.GetField(i))
                    else:
                        df.SetFieldNull(j)
                g = sf.GetGeometryRef()
                df.SetGeometry(g.Clone() if g is not None else None)
                if gi >= 0:
                    df.SetField(gi, "{%s}" % k[1])
                if kind == "update":
                    dl.SetFeature(df)
                else:
                    dl.CreateFeature(df)
                counts[kind] += 1
            _commit(dds, tx)
    finally:
        sds = None
        dds = None
    return counts


def _apply_rows(target, target_states_after, current, added, removed):
    rows = (set(current) | set(added)) - set(removed)
    _write_rowsets(target, rows, target_states_after)


def _copy_fields(src_path, dst_path, items, src_states, dst_states):
    """Copy only some fields ('@shape' = geometry) of features from src to
    dst. items {(cls, gid): [fields]}. Returns the number of features."""
    n = 0
    if not items:
        return n
    sds = S.open_ds(_strip_ns(src_path)[0])
    dds = S.open_ds(_strip_ns(dst_path)[0], update=True)
    try:
        by_cls = {}
        for k, fields in items.items():
            if fields:
                by_cls.setdefault(k[0], []).append((k, fields))
        for cls, ks in by_cls.items():
            sl = sds.GetLayerByName(cls)
            dl = dds.GetLayerByName(cls)
            if sl is None or dl is None:
                continue
            sidx = {n.lower(): i for i, n in _names(sl)}
            didx = {n.lower(): i for i, n in _names(dl)}
            tx = _begin(dds)
            for k, fields in ks:
                if k not in src_states or k not in dst_states:
                    continue
                sf = sl.GetFeature(src_states[k][0])
                df = dl.GetFeature(dst_states[k][0])
                if sf is None or df is None:
                    continue
                for name in fields:
                    if name == "@shape":
                        g = sf.GetGeometryRef()
                        df.SetGeometry(g.Clone() if g is not None else None)
                        continue
                    i, j = sidx.get(name), didx.get(name)
                    if i is None or j is None:
                        continue
                    if sf.IsFieldSetAndNotNull(i):
                        df.SetField(j, sf.GetField(i))
                    else:
                        df.SetFieldNull(j)
                dl.SetFeature(df)
                n += 1
            _commit(dds, tx)
    finally:
        sds = None
        dds = None
    return n


def _side(choices, key, field, favor):
    v = choices.get((key[0], key[1], field), choices.get(key, favor))
    return "default" if v in ("default", "parent") else "version"


def reconcile(vpath, classes, favor="version", choices=None):
    """Bring the changes of the parent (Default or the parent version) into
    the version. Features changed on both sides are merged field by field;
    a field changed differently on both sides is a conflict resolved by
    `favor` ('version' or 'parent'), or per feature {(cls, gid): side} or
    per field {(cls, gid, field): side} in `choices`. Returns a report."""
    choices = choices or {}
    d = diff(vpath, classes)
    dpath = default_path(vpath)
    take, partial = [], {}
    kept = taken = 0
    for k in d.default_changes:
        if k not in d.version_changes:
            take.append(k)  # changed in the parent only
            continue
        if k in d.merge_fields or k in d.conflict_fields:
            fields = list(d.merge_fields.get(k, []))
            for f in d.conflict_fields.get(k, []):
                if _side(choices, k, f, favor) == "default":
                    fields.append(f)
                    taken += 1
                else:
                    kept += 1
            if fields:
                partial[k] = fields
            continue
        if k in d.conflicts:
            if _side(choices, k, None, favor) == "default":
                take.append(k)
                taken += 1
            else:
                kept += 1
    counts = _copy_features(dpath, vpath, take, d.dft, d.ver)
    merged = _copy_fields(dpath, vpath, partial, d.dft, d.ver)
    ver_after = read_states(vpath, classes)
    rv = read_rowsets(vpath, ver_after)
    if d.rows_default_added or d.rows_default_removed:
        _apply_rows(
            vpath,
            ver_after,
            rv,
            d.rows_default_added - d.rows_version_removed,
            d.rows_default_removed,
        )
    # the version is now based on the current parent
    dft = read_states(dpath, classes, with_fields=True)
    rd = read_rowsets(dpath, dft)
    _write_base(vpath, dft, rd, {"base_time": _now()})
    name = info(vpath).get("version")
    if name:
        _update_registry(dpath, name, reconciled=_now())
    return {
        "parent changes taken": counts,
        "features merged field by field": merged,
        "conflicts": len(d.conflicts),
        "conflicting values kept from the version": kept,
        "conflicting values taken from the parent": taken,
    }


def post(vpath, classes):
    """Push the version changes into Default (the version must be reconciled
    with the current Default)."""
    d = diff(vpath, classes)
    if d.default_changes or d.rows_default_added or d.rows_default_removed:
        raise VersionError(
            "The parent changed since the last reconcile (%d feature(s)):"
            " reconcile the version first."
            % len(d.default_changes)
        )
    dpath = default_path(vpath)
    counts = _copy_features(
        vpath, dpath, list(d.version_changes), d.ver, d.dft
    )
    dft_after = read_states(dpath, classes, with_fields=True)
    if d.rows_version_added or d.rows_version_removed:
        rd = read_rowsets(dpath, dft_after)
        _apply_rows(
            dpath,
            dft_after,
            rd,
            d.rows_version_added,
            d.rows_version_removed,
        )
    rd = read_rowsets(dpath, dft_after)
    _write_base(vpath, dft_after, rd, {"base_time": _now()})
    name = info(vpath).get("version")
    _log_history(dpath, name, d, counts)
    counts["associations / controllers"] = len(d.rows_version_added) + len(
        d.rows_version_removed
    )
    if name:
        _update_registry(
            dpath,
            name,
            posted=_now(),
            status="Posted",
            posted_counts=(
                "%(insert)d inserted, %(update)d updated, %(delete)d deleted"
            )
            % counts,
        )
    return counts


def reconcile_and_post(vpath, classes, favor="version", choices=None):
    rep = reconcile(vpath, classes, favor, choices)
    rep["posted"] = post(vpath, classes)
    return rep


def _log_history(dpath, name, d, counts):
    states = read_states(dpath, list({k[0] for k in d.version_changes}))
    rows = []
    now = _now()
    user = current_user()
    for k, kind in d.version_changes.items():
        fid = states.get(k, (d.dft.get(k, (None,))[0], None))[0]
        rows.append(
            {
                "changed_at": now,
                "user_name": user,
                "class_name": k[0],
                "feature_fid": fid,
                "action": "post " + kind,
                "field_name": "version %s" % (name or ""),
                "old_value": None,
                "new_value": "{%s}" % k[1],
            }
        )
    if not rows:
        return
    ds = S.open_ds(dpath, update=True)
    try:
        lyr = S._make_table(ds, "un_history", S.SYSTEM_TABLES["un_history"])
        tx = _begin(ds)
        for r in rows:
            f = ogr.Feature(lyr.GetLayerDefn())
            for k, v in r.items():
                if v is not None:
                    f.SetField(k, v)
            lyr.CreateFeature(f)
        _commit(ds, tx)
    finally:
        ds = None


# ------------------------------------------------------------------ protection
def is_protected(default):
    ds = S.open_ds(root_path(default))
    try:
        st = {r["key"]: r["value"] for r in S._read_rows(ds, "un_network")}
    finally:
        ds = None
    return st.get("default_protected") == "1"


def set_protected(default, on):
    S.set_setting(root_path(default), "default_protected", "1" if on else "0")
