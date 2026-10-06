"""Audit trail: every saved change (added / deleted features, changed
attributes with the old and
new value, changed geometries) is written to un_history with the user and the
time.
"""

from datetime import datetime

from qgis.core import QgsApplication, QgsFeature, QgsFeatureRequest
from qgis.PyQt.QtCore import QTimer

from .qgis_io import get_layer, project_layer


def _user():
    try:
        from qgis.core import QgsExpressionContextUtils

        return (
            QgsExpressionContextUtils.globalScope().variable(
                "user_account_name"
            )
            or ""
        )
    except Exception:
        return ""


def _txt(v):
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return None
    if hasattr(v, "toString"):
        return (
            v.toString("yyyy-MM-dd HH:mm:ss")
            if hasattr(v, "time")
            else v.toString()
        )
    return str(v)[:1000]


class Recorder:
    def __init__(self):
        self.cfg = None
        self.enabled = True
        self._hooks = []
        self._pending = []

    def attach(self, cfg):
        self.detach()
        self.cfg = cfg
        for role, cls in cfg.classes.items():
            lyr = project_layer(cfg.path, cls)
            if lyr is None:
                continue
            before = lambda *_a, l=lyr, c=cls: self._before(l, c)  # noqa
            added = lambda _lid, feats, l=lyr, c=cls: self._added(c, feats)  # noqa
            after = lambda *_a: QTimer.singleShot(0, self._flush)  # noqa: E731
            lyr.beforeCommitChanges.connect(before)
            lyr.committedFeaturesAdded.connect(added)
            lyr.afterCommitChanges.connect(after)
            self._hooks.append((lyr, before, added, after))

    def detach(self):
        for lyr, b, a, f in self._hooks:
            try:
                lyr.beforeCommitChanges.disconnect(b)
                lyr.committedFeaturesAdded.disconnect(a)
                lyr.afterCommitChanges.disconnect(f)
            except (TypeError, RuntimeError):
                pass
        self._hooks = []

    def _before(self, lyr, cls):
        if not self.enabled or self.cfg is None:
            return
        buf = lyr.editBuffer()
        if buf is None:
            return
        now, user = datetime.now().isoformat(timespec="seconds"), _user()
        rows = []
        changed = buf.changedAttributeValues()
        geoms = buf.changedGeometries()
        deleted = buf.deletedFeatureIds()
        ids = [
            fid for fid in set(changed) | set(geoms) | set(deleted) if fid >= 0
        ]
        old = (
            {
                f.id(): f
                for f in lyr.dataProvider().getFeatures(
                    QgsFeatureRequest().setFilterFids(ids)
                )
            }
            if ids
            else {}
        )
        names = [f.name() for f in lyr.fields()]
        for fid, attrs in changed.items():
            if fid < 0 or fid in deleted:
                continue
            for i, new in attrs.items():
                name = names[i] if i < len(names) else str(i)
                if name in ("last_edited_user", "last_edited_date"):
                    continue
                o = _txt(old[fid].attribute(i)) if fid in old else None
                if o != _txt(new):
                    rows.append(
                        (now, user, cls, fid, "update", name, o, _txt(new))
                    )
        for fid in geoms:
            if fid >= 0 and fid not in deleted:
                rows.append(
                    (now, user, cls, fid, "geometry", None, None, None)
                )
        for fid in deleted:
            a = old.get(fid)
            rows.append(
                (
                    now,
                    user,
                    cls,
                    fid,
                    "delete",
                    "assetid",
                    (
                        _txt(a["assetid"])
                        if a is not None and "assetid" in names
                        else None
                    ),
                    None,
                )
            )
        self._write(rows)

    def _added(self, cls, feats):
        if not self.enabled or self.cfg is None:
            return
        now, user = datetime.now().isoformat(timespec="seconds"), _user()
        self._write(
            [(now, user, cls, f.id(), "add", None, None, None) for f in feats]
        )

    def _write(self, rows):
        """Rows are kept until the commit is finished: the file is locked while
        QGIS saves."""
        self._pending.extend(rows)

    def _flush(self):
        rows, self._pending = self._pending, []
        if not rows or self.cfg is None:
            return
        try:
            lyr = get_layer(self.cfg.path, "un_history")
            feats = []
            for r in rows:
                f = QgsFeature(lyr.fields())
                f.setAttributes([None] * len(lyr.fields()))
                for k, v in zip(
                    (
                        "changed_at",
                        "user_name",
                        "class_name",
                        "feature_fid",
                        "action",
                        "field_name",
                        "old_value",
                        "new_value",
                    ),
                    r,
                ):
                    f.setAttribute(k, v)
                feats.append(f)
            lyr.dataProvider().addFeatures(feats)
        except Exception as e:
            QgsApplication.messageLog().logMessage(
                "History: %s" % e, "Network Studio"
            )


def rows(cfg, cls=None, fids=None, user=None, limit=5000):
    lyr = get_layer(cfg.path, "un_history")
    out = []
    for f in lyr.getFeatures():
        r = {
            n: _txt(f[n])
            for n in (
                "changed_at",
                "user_name",
                "class_name",
                "action",
                "field_name",
                "old_value",
                "new_value",
            )
        }
        r["feature_fid"] = f["feature_fid"]
        if cls and r["class_name"] != cls:
            continue
        if (
            fids is not None
            and (r["class_name"], int(r["feature_fid"] or -1)) not in fids
        ):
            continue
        if user and (r["user_name"] or "").lower() != user.lower():
            continue
        out.append(r)
    out.sort(key=lambda r: r["changed_at"] or "", reverse=True)
    return out[:limit]
