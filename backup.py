"""Backups of the network file (taken by hand or automatically before automatic
edits)."""

import os
import shutil
from datetime import datetime


def backup_dir(path):
    from . import storage as S

    path = S.split_domain(path)[0]
    if S.is_pg(path):
        p = S.pg_parts(path)
        return os.path.join(
            os.path.expanduser("~"),
            "NetworkStudio_backups",
            "%s_%s_%s"
            % (
                p.get("host", ""),
                p.get("dbname", ""),
                p.get("active_schema", "public"),
            ),
        )
    base = path.rstrip("/\\")
    return base + "_backups"


def make_backup(path, reason="manual"):
    """Copy the network file (or .gdb folder) to
    <file>_backups/<name>_<date>_<reason>.<ext>."""
    from . import storage as _S

    path = _S.split_domain(path)[0]
    from . import storage as S

    if S.is_pg(path):
        from osgeo import gdal

        folder = backup_dir(path)
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(
            folder,
            "%s_%s.gpkg"
            % (stamp, "".join(c for c in reason if c.isalnum())[:20]),
        )
        src = gdal.OpenEx(
            path, gdal.OF_VECTOR, open_options=["LIST_ALL_TABLES=YES"]
        )
        res = gdal.VectorTranslate(dst, src, format="GPKG")
        src = None
        if res is None:
            raise OSError("PostGIS backup failed: %s" % gdal.GetLastErrorMsg())
        res = None
        return dst
    src = path.rstrip("/\\")
    folder = backup_dir(src)
    os.makedirs(folder, exist_ok=True)
    name, ext = os.path.splitext(os.path.basename(src))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(
        folder,
        "%s_%s_%s%s"
        % (name, stamp, "".join(c for c in reason if c.isalnum())[:20], ext),
    )
    if os.path.isdir(src):
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.lock"))
    else:
        shutil.copy2(src, dst)
        for extra in ("-wal", "-shm"):
            if os.path.exists(src + extra):
                shutil.copy2(src + extra, dst + extra)
    return dst


def list_backups(path):
    folder = backup_dir(path)
    if not os.path.isdir(folder):
        return []
    items = [
        os.path.join(folder, n)
        for n in os.listdir(folder)
        if not n.endswith(("-wal", "-shm"))
    ]
    return sorted(items, key=os.path.getmtime, reverse=True)


def prune(path, keep=20):
    """Keep only the newest `keep` automatic backups."""
    autos = [
        p for p in list_backups(path) if "_manual" not in os.path.basename(p)
    ]
    for old in autos[keep:]:
        if os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)
        else:
            for extra in ("", "-wal", "-shm"):
                if os.path.exists(old + extra):
                    os.remove(old + extra)
