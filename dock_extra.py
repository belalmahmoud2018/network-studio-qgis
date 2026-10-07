"""Pages of the main window for versions / design projects, attribute rules,
hydraulic analysis and web editing (mixed into NetworkDock)."""

from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer
from qgis.gui import (
    QgsFieldComboBox,
    QgsFieldExpressionWidget,
    QgsMapLayerComboBox,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import attribute_rules as AR
from . import hydraulics
from . import qgis_io as Q
from . import storage as S
from . import versioning as V
from . import webedit


def _spin(value, lo=0.0, hi=100000.0, dec=3, suffix=""):
    s = QDoubleSpinBox()
    s.setDecimals(dec)
    s.setRange(lo, hi)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    return s


def _table(headers, height=200):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(
        QHeaderView.ResizeMode.Interactive
    )
    t.horizontalHeader().setStretchLastSection(True)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setMinimumHeight(height)
    return t


def _set_row(table, row, values, data=None):
    for c, v in enumerate(values):
        it = QTableWidgetItem("" if v is None else str(v))
        if data is not None:
            it.setData(Qt.ItemDataRole.UserRole, data)
        table.setItem(row, c, it)


def _buttons(lay, items):
    row = QHBoxLayout()
    out = []
    for label, slot, tip in items:
        b = QPushButton(label)
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(slot)
        row.addWidget(b)
        out.append(b)
    lay.addLayout(row)
    return out


def _fmt(d, indent="  "):
    lines = []
    for k, v in d.items():
        if isinstance(v, dict):
            lines.append("%s%s:" % (indent, k))
            lines += _fmt(v, indent + "  ").split("\n")
        else:
            lines.append("%s%s: %s" % (indent, k, v))
    return "\n".join(lines)


class RuleDialog(QDialog):
    def __init__(self, parent, cfg, rule=None):
        super().__init__(parent)
        self.setWindowTitle("Attribute rule")
        self.cfg = cfg
        rule = dict(rule or {})
        lay = QFormLayout(self)
        self.name = QLineEdit(rule.get("name") or "")
        self.kind = QComboBox()
        for k in AR.TYPES:
            self.kind.addItem(k.capitalize(), k)
        self.kind.setCurrentIndex(
            max(0, self.kind.findData(rule.get("rule_type") or "calculation"))
        )
        self.cls = QComboBox()
        for role, cls in cfg.classes.items():
            self.cls.addItem("%s (%s)" % (cls, role), cls)
        if rule.get("class_name"):
            self.cls.setCurrentIndex(
                max(0, self.cls.findData(rule["class_name"]))
            )
        self.field = QgsFieldComboBox()
        self.field.setAllowEmptyFieldName(True)
        self.expr = QgsFieldExpressionWidget()
        self.trig = QComboBox()
        for t in AR.TRIGGERS:
            self.trig.addItem(t.replace(",", " and "), t)
        self.trig.setCurrentIndex(
            max(0, self.trig.findData(rule.get("triggers") or "insert"))
        )
        self.sev = QComboBox()
        self.sev.addItems(["error", "warning"])
        self.sev.setCurrentText(rule.get("severity") or "error")
        self.msg = QLineEdit(rule.get("message") or "")
        self.desc = QLineEdit(rule.get("description") or "")
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(bool(rule.get("enabled", 1)))
        lay.addRow("Name", self.name)
        lay.addRow("Type", self.kind)
        lay.addRow("Class", self.cls)
        lay.addRow("Field", self.field)
        lay.addRow("Expression", self.expr)
        lay.addRow("Runs on", self.trig)
        lay.addRow("Severity", self.sev)
        lay.addRow("Message", self.msg)
        lay.addRow("Description", self.desc)
        lay.addRow(self.enabled)
        self.help = QLabel()
        self.help.setWordWrap(True)
        self.help.setStyleSheet("color: #666;")
        lay.addRow(self.help)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)
        self.cls.currentIndexChanged.connect(self._class_changed)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self._class_changed()
        if rule.get("field_name"):
            self.field.setField(rule["field_name"])
        self.expr.setExpression(rule.get("expression") or "")
        self._kind_changed()
        self.resize(560, 420)

    def layer(self):
        try:
            return Q.get_layer(self.cfg.path, self.cls.currentData())
        except S.StoreError:
            return None

    def _class_changed(self, _i=0):
        lyr = self.layer()
        self.field.setLayer(lyr)
        self.expr.setLayer(lyr)

    def _kind_changed(self, _i=0):
        k = self.kind.currentData()
        self.trig.setEnabled(k == "calculation")
        self.msg.setEnabled(k != "calculation")
        self.sev.setEnabled(k != "calculation")
        self.help.setText(
            {
                "calculation": (
                    "The expression gives the value of the"
                    " field. It runs when a feature is added (and updated, if"
                    " chosen), also in QField."
                ),
                "constraint": (
                    "The expression must be true. Error blocks saving the"
                    " form, warning only warns. Leave the field empty for a"
                    " rule on the whole feature."
                ),
                "validation": (
                    "The expression must be true; checked by"
                    " Validate and Evaluate rules, broken features go to the"
                    " error layers (code AR)."
                ),
            }[k]
        )

    def _ok(self):
        if not self.name.text().strip():
            QMessageBox.warning(
                self, "Attribute rule", "Give the rule a name."
            )
            return
        if self.kind.currentData() == "calculation" and not (
            self.field.currentField()
        ):
            QMessageBox.warning(
                self, "Attribute rule", "A calculation rule needs a field."
            )
            return
        err = AR.check(self.expr.expression(), self.layer())
        if err:
            QMessageBox.warning(
                self, "Attribute rule", "Expression error:\n%s" % err
            )
            return
        self.accept()

    def rule(self):
        return {
            "name": self.name.text().strip(),
            "rule_type": self.kind.currentData(),
            "class_name": self.cls.currentData(),
            "field_name": self.field.currentField() or "",
            "expression": self.expr.expression(),
            "triggers": (
                self.trig.currentData()
                if self.kind.currentData() == "calculation"
                else ""
            ),
            "message": self.msg.text().strip(),
            "severity": self.sev.currentText(),
            "enabled": 1 if self.enabled.isChecked() else 0,
            "description": self.desc.text().strip(),
        }


class VersionDialog(QDialog):
    def __init__(self, parent, row=None, new=True):
        super().__init__(parent)
        self.setWindowTitle("New version" if new else "Version details")
        row = row or {}
        lay = QFormLayout(self)
        self.name = QLineEdit(row.get("name") or "")
        self.name.setEnabled(new)
        self.desc = QLineEdit(row.get("description") or "")
        self.wo = QLineEdit(row.get("work_order") or "")
        self.access = QComboBox()
        self.access.addItems(V.ACCESS)
        self.access.setCurrentText(row.get("access") or "Public")
        self.owner = QLineEdit(row.get("owner") or V.current_user())
        lay.addRow("Name", self.name)
        lay.addRow("Description", self.desc)
        lay.addRow("Work order / project", self.wo)
        lay.addRow("Access", self.access)
        lay.addRow("Owner", self.owner)
        note = QLabel(
            "Public: everyone can open and edit. Protected: others can open"
            " it read-only. Private: only the owner sees it."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        lay.addRow(note)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)
        self.resize(460, 220)


class ExtraPages:
    """Mixed into NetworkDock."""

    # ============================================================ Versions
    def _tab_versions(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Work on a design or a long job in its own version: a copy of"
                " the network where every tool works, while Default (the"
                " network everybody uses) stays untouched. When the work is"
                " approved, reconcile the version (take the changes made in"
                " Default meanwhile) and post it (put its changes into"
                " Default)."
            )
        )
        self.ver_banner = QLabel("Default")
        self.ver_banner.setStyleSheet(
            "font-weight: bold; padding: 6px; background: #e1f5ee;"
            " color: #085041;"
        )
        lay.addWidget(self.ver_banner)
        self.ver_table = _table(
            [
                "Name",
                "Parent",
                "Status",
                "Owner",
                "Access",
                "Work order",
                "Created",
                "Reconciled",
                "Posted",
                "Description",
            ],
            170,
        )
        self.ver_table.cellDoubleClicked.connect(lambda r, c: self._ver_open())
        lay.addWidget(self.ver_table)
        _buttons(
            lay,
            [
                (
                    "New version...",
                    self._ver_new,
                    "Copy Default into a new version (design project).",
                ),
                (
                    "Open selected version",
                    self._ver_open,
                    "Switch the map and every tool to the selected version.",
                ),
                ("Back to Default", self._ver_default, ""),
                (
                    "Open parent",
                    self._ver_up,
                    (
                        "Open the version (or Default) this version was made"
                        " from."
                    ),
                ),
                ("Delete version", self._ver_delete, ""),
            ],
        )
        row = QHBoxLayout()
        dt = QPushButton("Details...")
        dt.clicked.connect(self._ver_details)
        self.ver_status = QComboBox()
        self.ver_status.addItems(V.STATUSES)
        st = QPushButton("Set status")
        st.clicked.connect(self._ver_set_status)
        row.addWidget(dt)
        row.addStretch(1)
        row.addWidget(QLabel("Design status"))
        row.addWidget(self.ver_status)
        row.addWidget(st)
        lay.addLayout(row)
        rbox = QGroupBox("Reconcile and post (inside a version)")
        rl = QVBoxLayout(rbox)
        crow = QHBoxLayout()
        crow.addWidget(QLabel("Conflicts (edited in both):"))
        self.ver_favor_v = QRadioButton("keep the version")
        self.ver_favor_v.setChecked(True)
        self.ver_favor_d = QRadioButton("take the parent")
        crow.addWidget(self.ver_favor_v)
        crow.addWidget(self.ver_favor_d)
        crow.addStretch(1)
        rl.addLayout(crow)
        self.ver_buttons = _buttons(
            rl,
            [
                (
                    "Compare with parent",
                    self._ver_compare,
                    (
                        "List and show on the map what changed in the version"
                        " and in Default since the version was made or"
                        " reconciled."
                    ),
                ),
                (
                    "Reconcile",
                    self._ver_reconcile,
                    (
                        "Take the changes made in the parent (Default or the"
                        " version this one was made from) into this version;"
                        " features edited on both sides are merged field by"
                        " field."
                    ),
                ),
                (
                    "Post",
                    self._ver_post,
                    "Put the changes of this version into its parent.",
                ),
                ("Reconcile and post", self._ver_rec_post, ""),
            ],
        )
        self.ver_changes = _table(
            [
                "Class",
                "Global ID",
                "Version",
                "Parent",
                "Conflicting fields",
                "Keep",
            ],
            150,
        )
        self.ver_changes.cellDoubleClicked.connect(self._ver_zoom)
        rl.addWidget(self.ver_changes)
        lay.addWidget(rbox)
        pbox = QGroupBox("Protect Default")
        pl = QVBoxLayout(pbox)
        self.ver_protect = QCheckBox(
            "Default can only be changed by posting versions (its layers open"
            " read-only)"
        )
        self.ver_admin = QCheckBox(
            "I am the administrator: allow editing Default in this session"
        )
        prow = QHBoxLayout()
        pb = QPushButton("Save protection")
        pb.clicked.connect(self._ver_save_protect)
        prow.addWidget(self.ver_admin)
        prow.addStretch(1)
        prow.addWidget(pb)
        pl.addWidget(self.ver_protect)
        pl.addLayout(prow)
        lay.addWidget(pbox)
        self.ver_out = QPlainTextEdit()
        self.ver_out.setReadOnly(True)
        self.ver_out.setMaximumHeight(110)
        lay.addWidget(self.ver_out)
        lay.addStretch(1)
        self._ver_diff = None
        return w

    def _default(self):
        """Default at the top of the version tree (holds the list)."""
        return V.root_path(self.cfg.path)

    def _parent(self):
        return V.default_path(self.cfg.path)

    def _ver_fill(self):
        if self.cfg is None:
            return
        try:
            inf = V.info(self.cfg.path)
            dflt = self._default()
            rows = V.list_versions(dflt, user=V.current_user())
            protected = V.is_protected(dflt)
        except Exception as e:
            Q.log("Versions: %s" % e, True)
            return
        if inf.get("version"):
            row = next((r for r in rows if r["name"] == inf["version"]), {})
            self.ver_banner.setText(
                "Version: %s   |   parent %s   |   status %s   |   base of %s"
                % (
                    inf["version"],
                    row.get("parent") or "Default",
                    row.get("status", "?"),
                    (inf.get("base_time") or "").replace("T", " "),
                )
            )
            self.ver_banner.setStyleSheet(
                "font-weight: bold; padding: 6px; background: #faeeda;"
                " color: #633806;"
            )
        else:
            self.ver_banner.setText(
                "Default%s" % ("   (protected)" if protected else "")
            )
            self.ver_banner.setStyleSheet(
                "font-weight: bold; padding: 6px; background: #e1f5ee;"
                " color: #085041;"
            )
        for b in self.ver_buttons:
            b.setEnabled(bool(inf.get("version")))
        self.ver_protect.setChecked(protected)
        self.ver_table.setRowCount(0)
        for r in rows:
            i = self.ver_table.rowCount()
            self.ver_table.insertRow(i)
            _set_row(
                self.ver_table,
                i,
                [
                    r.get("name"),
                    r.get("parent") or "Default",
                    r.get("status"),
                    r.get("owner"),
                    r.get("access"),
                    r.get("work_order"),
                    (r.get("created") or "").replace("T", " "),
                    (r.get("reconciled") or "").replace("T", " "),
                    (r.get("posted") or "").replace("T", " "),
                    r.get("description"),
                ],
                r.get("name"),
            )
        self.ver_table.resizeColumnsToContents()

    def _ver_selected(self):
        r = self.ver_table.currentRow()
        if r < 0:
            QMessageBox.information(self, "Versions", "Select a version.")
            return None
        return self.ver_table.item(r, 0).data(Qt.ItemDataRole.UserRole)

    def _unsaved(self):
        out = []
        for cls in self.cfg.classes.values():
            lyr = Q.project_layer(self.cfg.path, cls)
            if lyr is not None and lyr.isEditable() and lyr.isModified():
                out.append(lyr.name())
        return out

    def _need_saved(self, title):
        names = self._unsaved()
        if names:
            QMessageBox.warning(
                self,
                title,
                "Save or discard the edits first: %s" % ", ".join(names),
            )
            return False
        return True

    def _switch(self, path):
        """Close the layers of the current network and open `path`."""
        if self.cfg is not None:
            if not self._need_saved("Versions"):
                return
            old = S.split_domain(self.cfg.path)[0]
            prj = QgsProject.instance()
            ids = []
            for lyr in prj.mapLayers().values():
                if isinstance(lyr, QgsVectorLayer):
                    p, _n = Q.layer_source(lyr)
                    if p and Q._norm(p) == Q._norm(old):
                        ids.append(lyr.id())
            root = prj.layerTreeRoot()
            groups = {
                root.findLayer(i).parent()
                for i in ids
                if root.findLayer(i) is not None
            }
            prj.removeMapLayers(ids)
            for g in groups:
                if g is not None and g is not root and not g.children():
                    parent = g.parent()
                    if parent is not None:
                        parent.removeChildNode(g)
            Q.clear_cache()
        self.open_network(path, add_layers=True)

    def _ver_new(self):
        if self.cfg is None:
            return
        dlg = VersionDialog(self)
        if not dlg.exec():
            return

        def go():
            if not self._need_saved("New version"):
                return None
            vpath = V.create_version(
                self.cfg.path,
                dlg.name.text(),
                list(self.cfg.classes.values()),
                dlg.desc.text().strip(),
                dlg.access.currentText(),
                dlg.wo.text().strip(),
                dlg.owner.text().strip(),
            )
            self._ver_fill()
            return vpath

        vpath = self._run("New version", go)
        if vpath and (
            QMessageBox.question(
                self,
                "New version",
                "Version created. Open it now?",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self._switch(vpath)

    def _ver_open(self):
        name = self._ver_selected()
        if not name:
            return
        dflt = self._default()
        row = V.get_version(dflt, name)
        if row.get("status") in ("Posted", "Abandoned"):
            if (
                QMessageBox.question(
                    self,
                    "Open version",
                    "This version is %s. Open it anyway?"
                    % row["status"].lower(),
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
        self._switch(V.version_path(dflt, row))
        if (
            row.get("access") == "Protected"
            and row.get("owner") != V.current_user()
        ):
            self._set_read_only(True)
            self._msg(
                "Protected version of %s: opened read-only." % row["owner"],
                "Warning",
            )

    def _ver_default(self):
        if self.cfg is None or not V.is_version(self.cfg.path):
            return
        self._switch(self._default())

    def _ver_up(self):
        if self.cfg is None or not V.is_version(self.cfg.path):
            return
        self._switch(self._parent())

    def _ver_delete(self):
        name = self._ver_selected()
        if not name:
            return
        if (
            QMessageBox.question(
                self,
                "Delete version",
                "Delete the version %s and all its edits? This cannot be"
                " undone." % name,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        if V.info(self.cfg.path).get("version") == name:
            QMessageBox.information(
                self, "Delete version", "Go back to Default first."
            )
            return

        def go():
            V.delete_version(self._default(), name)
            self._ver_fill()
            self._msg("Version %s deleted." % name, "Success")

        self._run("Delete version", go)

    def _ver_details(self):
        name = self._ver_selected()
        if not name:
            return
        dflt = self._default()
        dlg = VersionDialog(self, V.get_version(dflt, name), new=False)
        if dlg.exec():
            self._run(
                "Version details",
                lambda: (
                    V.update_details(
                        dflt,
                        name,
                        description=dlg.desc.text().strip(),
                        access=dlg.access.currentText(),
                        work_order=dlg.wo.text().strip(),
                        owner=dlg.owner.text().strip(),
                    ),
                    self._ver_fill(),
                ),
            )

    def _ver_set_status(self):
        name = self._ver_selected()
        if name:
            self._run(
                "Version status",
                lambda: (
                    V.set_status(
                        self._default(), name, self.ver_status.currentText()
                    ),
                    self._ver_fill(),
                ),
            )

    def _ver_save_protect(self):
        def go():
            V.set_protected(self._default(), self.ver_protect.isChecked())
            self._apply_protection()
            self._ver_fill()

        self._run("Protect Default", go)

    def _set_read_only(self, on):
        for cls in self.cfg.classes.values():
            lyr = Q.project_layer(self.cfg.path, cls)
            if lyr is not None:
                if on and lyr.isEditable():
                    continue
                lyr.setReadOnly(on)

    def _apply_protection(self):
        """Read-only Default layers when Default is protected."""
        if self.cfg is None:
            return
        try:
            version = V.is_version(self.cfg.path)
            protected = (not version) and V.is_protected(self.cfg.path)
        except Exception as e:
            Q.log("Protection: %s" % e, True)
            return
        lock = protected and not self.ver_admin.isChecked()
        self._set_read_only(lock)
        if lock:
            self._msg(
                "Default is protected: edit in a version (Versions page).",
                "Info",
                8,
            )

    def _ver_compare(self):
        def go():
            d = V.diff(self.cfg.path, list(self.cfg.classes.values()))
            self._ver_diff = d
            self._ver_fill_changes(d)
            n = self._ver_changes_layer(d)
            self.ver_out.setPlainText(
                "Changes since the version was made / last reconciled"
                " (features edited on both sides merge field by field)\n%s\n%d"
                " feature(s) drawn in the layer 'Version changes'."
                % (_fmt(d.counts()), n)
            )

        self._run("Compare", go)

    def _ver_fill_changes(self, d):
        t = self.ver_changes
        t.setRowCount(0)
        for cls, gid, vc, dc, conflict, fields in d.rows()[:2000]:
            i = t.rowCount()
            t.insertRow(i)
            _set_row(
                t,
                i,
                [
                    cls,
                    gid,
                    vc,
                    dc,
                    (fields or "whole feature") if conflict else "",
                    "",
                ],
                (cls, gid),
            )
            if conflict:
                cb = QComboBox()
                cb.addItems(["version", "parent"])
                cb.setCurrentText(
                    "version" if self.ver_favor_v.isChecked() else "parent"
                )
                t.setCellWidget(i, 5, cb)
        t.resizeColumnsToContents()

    def _choices(self):
        out = {}
        t = self.ver_changes
        for i in range(t.rowCount()):
            cb = t.cellWidget(i, 5)
            if cb is not None:
                key = t.item(i, 0).data(Qt.ItemDataRole.UserRole)
                out[tuple(key)] = cb.currentText()
        return out

    def _ver_geometry(self, path, cls, fid):
        ds = S.open_ds(S.split_domain(path)[0])
        try:
            lyr = ds.GetLayerByName(cls)
            f = lyr.GetFeature(fid) if lyr is not None else None
            g = f.GetGeometryRef() if f is not None else None
            return QgsGeometry.fromWkt(g.ExportToWkt()) if g else None
        finally:
            ds = None

    def _ver_changes_layer(self, d):
        from .diagrams import cfg_crs
        from .hydraulics import _categorized

        name = "Version changes"
        for lyr in QgsProject.instance().mapLayersByName(name):
            QgsProject.instance().removeMapLayer(lyr.id())
        layers = {}
        dpath = self._parent()
        n = 0
        for (cls, gid), kind in list(d.version_changes.items()) + [
            (k, "Default " + v)
            for k, v in d.default_changes.items()
            if k not in d.version_changes
        ]:
            if kind in ("insert", "update"):
                g = self._ver_geometry(
                    self.cfg.path, cls, d.ver[(cls, gid)][0]
                )
            elif kind == "delete":
                g = (
                    self._ver_geometry(dpath, cls, d.dft[(cls, gid)][0])
                    if ((cls, gid) in d.dft)
                    else None
                )
            else:
                src = d.dft.get((cls, gid))
                g = self._ver_geometry(dpath, cls, src[0]) if src else None
            if g is None or g.isNull():
                continue
            gk = Q.gkind(g)
            if gk not in layers:
                kind_name = {
                    "point": "Point",
                    "line": "MultiLineString",
                    "polygon": "MultiPolygon",
                }[gk]
                layers[gk] = QgsVectorLayer(
                    "%s?crs=%s&field=class_name:string&field=globalid:string"
                    "&field=change:string&field=conflict:string"
                    % (kind_name, cfg_crs(self.cfg)),
                    name,
                    "memory",
                )
            lyr = layers[gk]
            f = QgsFeature(lyr.fields())
            label = {
                "insert": "Added in version",
                "update": "Changed in version",
                "delete": "Deleted in version",
            }.get(kind, kind.replace("Default ", "Changed in Default: "))
            f.setAttributes(
                [cls, gid, label, "yes" if (cls, gid) in d.conflicts else ""]
            )
            if gk != "point":
                g.convertToMultiType()
            f.setGeometry(g)
            lyr.dataProvider().addFeatures([f])
            n += 1
        colors = {
            "Added in version": "#1d9e75",
            "Changed in version": "#ef9f27",
            "Deleted in version": "#e24b4a",
        }
        from .hydraulics import STATUS_COLORS

        STATUS_COLORS.update(colors)
        for lyr in layers.values():
            _categorized(lyr, "change", 1.2)
            QgsProject.instance().addMapLayer(lyr)
        return n

    def _ver_zoom(self, row, _c):
        if self._ver_diff is None:
            return
        key = tuple(
            self.ver_changes.item(row, 0).data(Qt.ItemDataRole.UserRole)
        )
        st = self._ver_diff.ver.get(key)
        path = self.cfg.path
        if st is None:
            st = self._ver_diff.dft.get(key)
            path = self._parent()
        if st is None:
            return
        g = self._ver_geometry(path, key[0], st[0])
        if g is not None and not g.isNull():
            box = g.boundingBox()
            box.grow(max(box.width(), box.height(), 20) * 0.6)
            self.iface.mapCanvas().setExtent(box)
            self.iface.mapCanvas().refresh()

    def _reload_layers(self, path):
        Q.clear_cache()
        for cls in self.cfg.classes.values():
            lyr = Q.project_layer(path, cls)
            if lyr is not None:
                lyr.dataProvider().reloadData()
                lyr.triggerRepaint()
        self._net = None

    def _ver_reconcile(self):
        def go():
            if not self._need_saved("Reconcile"):
                return
            rep = V.reconcile(
                self.cfg.path,
                list(self.cfg.classes.values()),
                "version" if self.ver_favor_v.isChecked() else "default",
                self._choices(),
            )
            self._reload_layers(self.cfg.path)
            self._reload_cfg()
            self.ver_out.setPlainText("Reconcile\n" + _fmt(rep))
            self.ver_changes.setRowCount(0)
            self._ver_fill()
            self._msg("Version reconciled with Default.", "Success")

        self._run("Reconcile", go)

    def _ver_post(self, _c=False, after_reconcile=False):
        inf = V.info(self.cfg.path)
        row = V.get_version(self._default(), inf.get("version", ""))
        if row.get("status") != "Approved" and not after_reconcile:
            if (
                QMessageBox.question(
                    self,
                    "Post",
                    "The design status is '%s', not Approved. Post the"
                    " changes into its parent anyway?"
                    % row.get("status"),
                )
                != QMessageBox.StandardButton.Yes
            ):
                return

        def go():
            if not self._need_saved("Post"):
                return
            self._auto("post")
            counts = V.post(self.cfg.path, list(self.cfg.classes.values()))
            self.ver_out.setPlainText(
                "Posted into the parent\n" + _fmt(counts)
            )
            self.ver_changes.setRowCount(0)
            self._ver_fill()
            self._msg(
                "Posted: %(insert)d added, %(update)d changed, %(delete)d"
                " deleted." % counts,
                "Success",
            )

        self._run("Post", go)

    def _ver_rec_post(self):
        inf = V.info(self.cfg.path)
        row = V.get_version(self._default(), inf.get("version", ""))
        if row.get("status") != "Approved" and (
            QMessageBox.question(
                self,
                "Reconcile and post",
                "The design status is '%s', not Approved. Continue?"
                % row.get("status"),
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._ver_reconcile()
        self._ver_post(after_reconcile=True)

    # ============================================================ Rules
    def _tab_attr_rules(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Attribute rules keep the data right while people edit."
                " Calculation rules fill a field automatically (asset IDs,"
                " lengths, sizes taken from the main...). Constraint rules"
                " stop wrong values in the form. Validation rules are checked"
                " in batch and their errors go to the error layers. The rules"
                " are QGIS expressions stored inside the network."
            )
        )
        self.ar_table = _table(
            [
                "On",
                "Name",
                "Type",
                "Class",
                "Field",
                "Runs on",
                "Severity",
                "Expression",
            ],
            260,
        )
        self.ar_table.cellDoubleClicked.connect(lambda r, c: self._ar_edit())
        lay.addWidget(self.ar_table)
        _buttons(
            lay,
            [
                ("Add rule...", self._ar_add, ""),
                ("Edit...", self._ar_edit, ""),
                ("Remove", self._ar_remove, ""),
                ("Up", lambda: self._ar_move(-1), ""),
                ("Down", lambda: self._ar_move(1), ""),
            ],
        )
        _buttons(
            lay,
            [
                (
                    "Add ready-made rules",
                    self._ar_templates,
                    (
                        "Asset IDs, measured length, size from the main,"
                        " positive sizes, valid asset types, install dates..."
                    ),
                ),
                (
                    "Save rules",
                    self._ar_save,
                    (
                        "Store the rules in the network and apply them to the"
                        " layers."
                    ),
                ),
            ],
        )
        ebox = QGroupBox("Evaluate rules on existing features")
        el = QVBoxLayout(ebox)
        self.ar_selected = QCheckBox("Selected features only")
        self.ar_calc = QCheckBox("Run calculation rules (writes the values)")
        self.ar_calc.setChecked(True)
        el.addWidget(self.ar_selected)
        el.addWidget(self.ar_calc)
        eb = QPushButton("Evaluate rules now")
        eb.clicked.connect(self._ar_evaluate)
        el.addWidget(eb)
        lay.addWidget(ebox)
        self.ar_out = QPlainTextEdit()
        self.ar_out.setReadOnly(True)
        self.ar_out.setMaximumHeight(130)
        lay.addWidget(self.ar_out)
        lay.addWidget(
            QLabel(
                "Expression functions: ns_next_id('V-', 6),"
                " ns_asset_group_name(), ns_asset_type_name(),"
                " ns_valid_asset_type(); and the QGIS functions"
                " overlay_nearest(), overlay_intersects(), aggregate()."
            )
        )
        lay.addStretch(1)
        self._ar_rows = []
        return w

    def _ar_fill(self, rows=None):
        if rows is not None:
            self._ar_rows = rows
        t = self.ar_table
        t.setRowCount(0)
        for r in self._ar_rows:
            i = t.rowCount()
            t.insertRow(i)
            _set_row(
                t,
                i,
                [
                    "",
                    r.get("name"),
                    r.get("rule_type"),
                    r.get("class_name"),
                    r.get("field_name"),
                    (r.get("triggers") or "").replace(",", " + "),
                    (
                        r.get("severity")
                        if r["rule_type"] != "calculation"
                        else ""
                    ),
                    r.get("expression"),
                ],
            )
            it = QTableWidgetItem()
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(
                Qt.CheckState.Checked
                if r.get("enabled")
                else Qt.CheckState.Unchecked
            )
            t.setItem(i, 0, it)
        t.resizeColumnsToContents()

    def _ar_sync_enabled(self):
        for i, r in enumerate(self._ar_rows):
            it = self.ar_table.item(i, 0)
            if it is not None:
                r["enabled"] = (
                    1 if it.checkState() == Qt.CheckState.Checked else 0
                )

    def _ar_add(self):
        if self.cfg is None:
            return
        dlg = RuleDialog(self, self.cfg)
        if dlg.exec():
            self._ar_sync_enabled()
            self._ar_rows.append(dlg.rule())
            self._ar_fill()

    def _ar_edit(self):
        i = self.ar_table.currentRow()
        if i < 0 or i >= len(self._ar_rows):
            return
        self._ar_sync_enabled()
        dlg = RuleDialog(self, self.cfg, self._ar_rows[i])
        if dlg.exec():
            self._ar_rows[i] = dlg.rule()
            self._ar_fill()

    def _ar_remove(self):
        i = self.ar_table.currentRow()
        if 0 <= i < len(self._ar_rows):
            self._ar_sync_enabled()
            del self._ar_rows[i]
            self._ar_fill()

    def _ar_move(self, step):
        i = self.ar_table.currentRow()
        j = i + step
        if 0 <= i < len(self._ar_rows) and 0 <= j < len(self._ar_rows):
            self._ar_sync_enabled()
            rows = self._ar_rows
            rows[i], rows[j] = rows[j], rows[i]
            self._ar_fill()
            self.ar_table.selectRow(j)

    def _ar_templates(self):
        if self.cfg is None:
            return
        self._ar_sync_enabled()
        have = {r.get("name") for r in self._ar_rows}
        new = [r for r in AR.templates(self.cfg) if r["name"] not in have]
        self._ar_rows += new
        self._ar_fill()
        self._msg(
            "%d ready-made rule(s) added: review them, then Save rules."
            % len(new)
        )

    def _ar_save(self):
        def go():
            self._ar_sync_enabled()
            AR.save(self.cfg.path, self._ar_rows)
            self._reload_cfg()
            Q.refresh_forms(self.cfg)
            self._ar_fill(list(self.cfg.attribute_rules))
            self._msg(
                "%d attribute rule(s) saved and applied to the layers."
                % len(self._ar_rows),
                "Success",
            )

        self._run("Attribute rules", go)

    def _ar_evaluate(self):
        def go():
            layers = Q.class_layers(self.cfg)
            AR.register_layers(self.cfg, layers)
            issues, changed, errors = AR.evaluate(
                self.cfg,
                layers,
                self.cfg.attribute_rules,
                self.ar_selected.isChecked(),
                self.ar_calc.isChecked(),
            )
            Q.write_errors(self.cfg, issues, codes=("AR",))
            lines = ["Evaluate attribute rules", ""]
            by_name = {r["name"]: r for r in self.cfg.attribute_rules}
            for name, n in changed.items():
                kind = by_name.get(name, {}).get("rule_type", "")
                lines.append(
                    "  %-45s %6d %s"
                    % (
                        name,
                        n,
                        "changed" if kind == "calculation" else "broken",
                    )
                )
            if errors:
                lines += ["", "Errors:"] + ["  " + e for e in errors[:20]]
            lines += [
                "",
                "%d broken rule(s) written to the error layers (code AR)."
                % len(issues),
            ]
            self.ar_out.setPlainText("\n".join(lines))
            self._net = None
            self.iface.mapCanvas().refresh()

        self._run("Evaluate rules", go)

    # ============================================================ Hydraulics
    def _tab_hydro(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Steady-state hydraulic analysis inside QGIS for water and"
                " district cooling networks (the same method as EPANET, no"
                " other software needed): pressures, flows, velocities, fire"
                " flow at the hydrants and calibration against field"
                " measurements."
            )
        )
        mbox = QGroupBox("Model")
        mf = QFormLayout(mbox)
        self.hy_d = _spin(100, 1, 5000, 1, " mm")
        self.hy_c = _spin(130, 1, 200, 1)
        self.hy_q = _spin(0.01, 0, 100, 4, " L/s")
        self.hy_h = _spin(50, 0, 1000, 1, " m")
        mf.addRow("Diameter when empty", self.hy_d)
        mf.addRow("Hazen-Williams C when empty", self.hy_c)
        mf.addRow("Demand per customer (L/s water, m3/h gas)", self.hy_q)
        mf.addRow("Source head above ground", self.hy_h)
        note = QLabel(
            "Diameters come from the size field of the lines, C from"
            " 'roughness', elevations from the junctions, demands from the"
            " 'demand' field of the customer devices."
        )
        note.setStyleSheet("color: #666;")
        mf.addRow(note)
        lay.addWidget(mbox)
        pbox = QGroupBox("Pumps, valves and tanks (water, cooling)")
        pf = QFormLayout(pbox)
        self.hy_ph = _spin(50, 1, 1000, 1, " m")
        self.hy_pq = _spin(50, 0.1, 100000, 1, " L/s")
        self.hy_prv = _spin(30, 0, 500, 1, " m")
        self.hy_td = _spin(20, 1, 500, 1, " m")
        self.hy_tl = _spin(5, 0, 100, 2, " m")
        self.hy_tmax = _spin(10, 0.1, 100, 2, " m")
        pf.addRow("Pump design head (when no pump_head)", self.hy_ph)
        pf.addRow("Pump design flow (when no pump_flow)", self.hy_pq)
        pf.addRow("PRV outlet pressure (when no setting)", self.hy_prv)
        pf.addRow("Tank diameter", self.hy_td)
        pf.addRow("Tank start level", self.hy_tl)
        pf.addRow("Tank maximum level", self.hy_tmax)
        pnote = QLabel(
            "Sources named 'pump' get a pump curve (single point, as"
            " EPANET); devices named 'pressure reducing' become PRVs that"
            " hold their outlet pressure; sources named 'tank' change level"
            " in the 24 h simulation."
        )
        pnote.setStyleSheet("color: #666;")
        pf.addRow(pnote)
        lay.addWidget(pbox)
        gbox = QGroupBox("Gas networks")
        gf = QFormLayout(gbox)
        self.hy_gmode = QComboBox()
        self.hy_gmode.addItem(
            "Medium pressure (bar, P1\u00b2 - P2\u00b2)", "mp"
        )
        self.hy_gmode.addItem("Low pressure (mbar)", "lp")
        self.hy_gp = _spin(4.0, 0.001, 1000, 3)
        self.hy_geps = _spin(0.05, 0.0, 10, 3, " mm")
        self.hy_grho = _spin(0.73, 0.1, 5, 3, " kg/m3")
        gf.addRow("Pressure level", self.hy_gmode)
        gf.addRow("Source / regulator outlet pressure", self.hy_gp)
        gf.addRow("Pipe roughness (when no 'roughness')", self.hy_geps)
        gf.addRow("Gas density at standard conditions", self.hy_grho)
        gnote = QLabel(
            "Gas uses the Darcy-Weisbach equation with the Swamee-Jain"
            " friction factor (low pressure) and the isothermal general flow"
            " equation (medium pressure). Demands are in m3/h (standard),"
            " pressures and limits in bar or mbar."
        )
        gnote.setStyleSheet("color: #666;")
        gf.addRow(gnote)
        lay.addWidget(gbox)
        abox = QGroupBox("Pressure and velocity analysis")
        af = QFormLayout(abox)
        self.hy_factor = _spin(1.0, 0.01, 20, 2)
        self.hy_pmin = _spin(20, 0, 5000, 3)
        self.hy_pmax = _spin(70, 0, 5000, 3)
        self.hy_vmax = _spin(2.0, 0, 20, 2, " m/s")
        af.addRow("Demand factor (peak hour)", self.hy_factor)
        af.addRow("Minimum pressure (m water, bar / mbar gas)", self.hy_pmin)
        af.addRow("Maximum pressure", self.hy_pmax)
        af.addRow("Maximum velocity", self.hy_vmax)
        rb = QPushButton("Run hydraulic analysis")
        rb.clicked.connect(self._hy_run)
        af.addRow(rb)
        lay.addWidget(abox)
        ebox = QGroupBox("24 hour simulation")
        ef = QFormLayout(ebox)
        self.hy_hours = _spin(24, 1, 168, 0, " h")
        self.hy_pattern = QLineEdit(
            " ".join("%g" % v for v in hydraulics.DEFAULT_PATTERN)
        )
        self.hy_pattern.setToolTip(
            "Demand multiplier for every hour (repeats after the last value)."
        )
        ef.addRow("Duration", self.hy_hours)
        ef.addRow("Hourly demand pattern", self.hy_pattern)
        erow = QHBoxLayout()
        eb = QPushButton("Run 24 h simulation")
        eb.clicked.connect(self._hy_eps)
        ex = QPushButton("Export 24 h model to EPANET...")
        ex.clicked.connect(self._hy_eps_export)
        erow.addWidget(eb)
        erow.addWidget(ex)
        ef.addRow(erow)
        lay.addWidget(ebox)
        fbox = QGroupBox("Fire flow")
        ff = QFormLayout(fbox)
        self.hy_fq = _spin(16, 0.1, 1000, 1, " L/s")
        self.hy_fres = _spin(14, 0, 200, 1, " m")
        self.hy_fsel = QCheckBox(
            "Selected devices only (otherwise every 'Hydrant' device)"
        )
        ff.addRow("Fire flow per hydrant", self.hy_fq)
        ff.addRow("Minimum residual pressure", self.hy_fres)
        ff.addRow(self.hy_fsel)
        fb = QPushButton("Run fire flow test")
        fb.clicked.connect(self._hy_fire)
        ff.addRow(fb)
        lay.addWidget(fbox)
        cbox = QGroupBox("Calibration")
        cf = QFormLayout(cbox)
        self.hy_obs = QgsMapLayerComboBox()
        try:
            from .dock import _filter

            self.hy_obs.setFilters(_filter("PointLayer"))
        except Exception as e:
            Q.log("Layer filter: %s" % e, True)
        self.hy_obs_field = QgsFieldComboBox()
        self.hy_obs.layerChanged.connect(self.hy_obs_field.setLayer)
        self.hy_obs_field.setLayer(self.hy_obs.currentLayer())
        self.hy_kind = QComboBox()
        self.hy_kind.addItem("Pressure (m)", "pressure")
        self.hy_kind.addItem("Hydraulic head (m)", "head")
        self.hy_kind.addItem("Flow (L/s)", "flow")
        self.hy_snap = _spin(10, 0.1, 1000, 1, " m")
        self.hy_group = QComboBox()
        self.hy_cdem = QCheckBox("Calibrate the demand factor too")
        self.hy_cmin = _spin(40, 1, 200, 0)
        self.hy_cmax = _spin(150, 1, 200, 0)
        cf.addRow("Measurements layer", self.hy_obs)
        cf.addRow("Measured value", self.hy_obs_field)
        cf.addRow("The value is", self.hy_kind)
        cf.addRow("Snap distance", self.hy_snap)
        cf.addRow("Group pipes by", self.hy_group)
        cf.addRow("C between", self.hy_cmin)
        cf.addRow("and", self.hy_cmax)
        cf.addRow(self.hy_cdem)
        crow = QHBoxLayout()
        cb = QPushButton("Calibrate")
        cb.clicked.connect(self._hy_calibrate)
        self.hy_apply = QPushButton("Write the calibrated C to the lines")
        self.hy_apply.setEnabled(False)
        self.hy_apply.clicked.connect(self._hy_apply)
        crow.addWidget(cb)
        crow.addWidget(self.hy_apply)
        cf.addRow(crow)
        lay.addWidget(cbox)
        self.hy_out = QPlainTextEdit()
        self.hy_out.setReadOnly(True)
        self.hy_out.setMinimumHeight(170)
        lay.addWidget(self.hy_out)
        lay.addStretch(1)
        self._hy_cal = None
        return w

    def _hy_fill(self):
        self.hy_group.clear()
        self.hy_group.addItem("One group (all pipes)", "")
        self.hy_group.addItem("Asset group", "assetgroup")
        self.hy_group.addItem("Asset type (material)", "assettype")
        try:
            lyr = Q.class_layers(self.cfg).get("Line")
        except Exception:
            lyr = None
        if lyr is not None:
            for f in lyr.fields():
                n = f.name()
                if n.lower() in ("assetgroup", "assettype", "fid", "globalid"):
                    continue
                if n.lower() in ("installdate", "material", "notes") or (
                    f.typeName().lower().startswith(("str", "text", "int"))
                ):
                    self.hy_group.addItem("Field: %s" % n, n)

    def _hy_params(self):
        return {
            "default_diameter": self.hy_d.value(),
            "roughness": self.hy_c.value(),
            "demand": self.hy_q.value(),
            "source_head": self.hy_h.value(),
            "pump_head": self.hy_ph.value(),
            "pump_flow": self.hy_pq.value(),
            "prv_setting": self.hy_prv.value(),
            "tank_diameter": self.hy_td.value(),
            "tank_level": self.hy_tl.value(),
            "tank_max": self.hy_tmax.value(),
            "gas_mode": self.hy_gmode.currentData(),
            "source_pressure": self.hy_gp.value(),
            "gas_eps": self.hy_geps.value(),
            "gas_density": self.hy_grho.value(),
        }

    def _is_gas(self):
        return self.cfg.settings.get("network_type") == "gas"

    def _hy_check(self, water_only=False):
        if self.cfg.settings.get("network_type") not in S.HYDRAULIC:
            raise ValueError(
                "The hydraulic solver is for water, district cooling and gas"
                " networks. Export gravity networks to SWMM (Hydraulic model"
                " page)."
            )
        if water_only and self._is_gas():
            raise ValueError("This tool is for water networks.")

    def _hy_eps(self):
        def go():
            self._hy_check()
            summary, text, _l = hydraulics.simulate_day(
                self.cfg,
                self.network(),
                self._hy_params(),
                int(self.hy_hours.value()),
                hydraulics.parse_pattern(self.hy_pattern.text()),
                self.hy_pmin.value(),
                self.hy_factor.value(),
            )
            self.hy_out.setPlainText(
                "24 hour simulation\n%s\n\n%s\n\nThe layer gives the lowest"
                " and highest pressure of every node over the period."
                % (_fmt(summary), text)
            )

        self._run("24 hour simulation", go)

    def _hy_eps_export(self):
        from qgis.PyQt.QtWidgets import QFileDialog

        path, _f = QFileDialog.getSaveFileName(
            self, "EPANET file", "network_24h.inp", "EPANET (*.inp)"
        )
        if not path:
            return

        def go():
            self._hy_check(water_only=True)
            p = self._hy_params()
            out = hydraulics.export_epanet(
                self.cfg,
                self.network(),
                path,
                p.pop("default_diameter"),
                p.pop("roughness"),
                p.pop("demand"),
                p.pop("source_head"),
                hours=int(self.hy_hours.value()),
                pattern=hydraulics.parse_pattern(self.hy_pattern.text()),
                **p
            )
            self.hy_out.setPlainText("Saved %s\n%s" % (path, _fmt(out)))

        self._run("EPANET", go)

    def _hy_run(self):
        def go():
            self._hy_check()
            summary, _m, res, _l = hydraulics.analyse(
                self.cfg,
                self.network(),
                self._hy_params(),
                self.hy_pmin.value(),
                self.hy_pmax.value(),
                self.hy_vmax.value(),
                demand_factor=self.hy_factor.value(),
                title="Hydraulics x%.2f" % self.hy_factor.value(),
            )
            self.hy_out.setPlainText(
                "Hydraulic analysis\n%s\n\nLayers added: pressures (Low / OK"
                " / High) and pipes (velocity classes)."
                % _fmt(summary)
            )
            if not res.converged:
                self._msg(
                    "The solution did not fully converge: check closed valves"
                    " and sources.",
                    "Warning",
                )

        self._run("Hydraulic analysis", go)

    def _hy_fire(self):
        def go():
            self._hy_check(water_only=True)
            out, _l = hydraulics.fire_flow(
                self.cfg,
                self.network(),
                self._hy_params(),
                self.hy_fq.value(),
                self.hy_fres.value(),
                self.hy_fsel.isChecked(),
                self.hy_factor.value(),
            )
            self.hy_out.setPlainText(
                "Fire flow\n%s\n\nThe layer gives for every hydrant the"
                " static and residual pressure, the lowest pressure in the"
                " network during the test and the flow available at the"
                " minimum residual pressure."
                % _fmt(out)
            )

        self._run("Fire flow", go)

    def _hy_calibrate(self):
        def go():
            self._hy_check(water_only=True)
            lyr = self.hy_obs.currentLayer()
            field = self.hy_obs_field.currentField()
            if lyr is None or not field:
                raise ValueError(
                    "Choose the layer of the measurements and its value field."
                )
            net = self.network()
            params = self._hy_params()
            model = hydraulics.build_model(self.cfg, net, **params)
            obs, skipped = hydraulics.observations_from_layer(
                self.cfg,
                net,
                model,
                lyr,
                field,
                self.hy_kind.currentData(),
                self.hy_snap.value(),
            )
            if not obs:
                raise ValueError(
                    "No measurement is within %.1f m of the network."
                    % self.hy_snap.value()
                )
            cal = hydraulics.calibrate(
                self.cfg,
                net,
                params,
                obs,
                self.hy_group.currentData() or "",
                self.hy_cdem.isChecked(),
                self.hy_cmin.value(),
                self.hy_cmax.value(),
            )
            self._hy_cal = cal
            self.hy_apply.setEnabled(bool(cal["c"]))
            self.hy_out.setPlainText(
                hydraulics.calibration_text(cal)
                + (
                    "\n\n%d measurement(s) too far from the network were left"
                    " out." % skipped
                    if skipped
                    else ""
                )
            )

        self._run("Calibration", go)

    def _hy_apply(self):
        if self._hy_cal is None:
            return

        def go():
            self._auto("calibration")
            n = hydraulics.apply_roughness(self.cfg, self._hy_cal)
            self._msg(
                "Calibrated C written to %d line(s) (field roughness)." % n,
                "Success",
            )

        self._run("Calibration", go)

    # ============================================================ Web editing
    def _web_box(self, lay):
        ebox = QGroupBox("Web editing")
        el = QVBoxLayout(ebox)
        el.addWidget(QLabel("Layers editable on the web:"))
        self.web_layers = QListWidget()
        self.web_layers.setMaximumHeight(130)
        el.addWidget(self.web_layers)
        crow = QHBoxLayout()
        self.web_create = QCheckBox("Add")
        self.web_create.setChecked(True)
        self.web_attr = QCheckBox("Change attributes")
        self.web_attr.setChecked(True)
        self.web_geom = QCheckBox("Change geometry")
        self.web_geom.setChecked(True)
        self.web_delete = QCheckBox("Delete")
        for c in (
            self.web_create,
            self.web_attr,
            self.web_geom,
            self.web_delete,
        ):
            crow.addWidget(c)
        el.addLayout(crow)
        form = QFormLayout()
        self.web_lizmap = QCheckBox("Write the Lizmap configuration too")
        self.web_lizmap.setChecked(True)
        self.web_groups = QLineEdit()
        self.web_groups.setPlaceholderText(
            "Lizmap user groups allowed to edit (empty = everybody logged in)"
        )
        form.addRow(self.web_lizmap)
        form.addRow("Editor groups", self.web_groups)
        el.addLayout(form)
        lay.addWidget(ebox)
        tbox = QGroupBox("Keep the network consistent after web edits")
        tl = QVBoxLayout(tbox)
        tl.addWidget(
            QLabel(
                "PostGIS: database triggers fill the global ID and the editor"
                " tracking fields and mark dirty areas for every change,"
                " whoever makes it (web, QField, SQL). Any format: 'Check for"
                " edits made outside QGIS' finds what changed since the"
                " previous check and marks dirty areas, so Validate checks"
                " those places."
            )
        )
        _buttons(
            tl,
            [
                ("Install database triggers", self._web_triggers_on, ""),
                ("Remove triggers", self._web_triggers_off, ""),
            ],
        )
        _buttons(
            tl,
            [
                ("Check for edits made outside QGIS", self._web_check, ""),
                ("Save current state as reference", self._web_reset, ""),
            ],
        )
        self.web_rules = QCheckBox(
            "Run the attribute rules on those edits (calculation rules fill"
            " their fields, broken rules go to the error layers)"
        )
        self.web_rules.setChecked(True)
        tl.addWidget(self.web_rules)
        arow = QHBoxLayout()
        self.web_auto = QCheckBox("Check automatically every")
        self.web_every = _spin(10, 1, 1440, 0, " min")
        self.web_auto.toggled.connect(self._web_auto_toggled)
        self.web_every.valueChanged.connect(
            lambda _v: self._web_auto_toggled(self.web_auto.isChecked())
        )
        arow.addWidget(self.web_auto)
        arow.addWidget(self.web_every)
        arow.addStretch(1)
        tl.addLayout(arow)
        lay.addWidget(tbox)
        from qgis.PyQt.QtCore import QTimer

        self._web_timer = QTimer(self)
        self._web_timer.timeout.connect(lambda: self._web_check(quiet=True))

    def _web_fill(self):
        self.web_layers.clear()
        for role, cls in self.cfg.classes.items():
            it = QListWidgetItem("%s (%s)" % (cls, role))
            it.setData(Qt.ItemDataRole.UserRole, cls)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(
                Qt.CheckState.Checked
                if role in ("Device", "Line", "Junction")
                else Qt.CheckState.Unchecked
            )
            self.web_layers.addItem(it)
        it = QListWidgetItem("Work orders")
        it.setData(Qt.ItemDataRole.UserRole, "un_work_orders")
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Checked)
        self.web_layers.addItem(it)

    def _web_edit_ids(self):
        ids = set()
        for i in range(self.web_layers.count()):
            it = self.web_layers.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                lyr = Q.project_layer(
                    self.cfg.path, it.data(Qt.ItemDataRole.UserRole)
                )
                if lyr is not None:
                    ids.add(lyr.id())
        return ids

    def _web_caps(self):
        return {
            "create": self.web_create.isChecked(),
            "attributes": self.web_attr.isChecked(),
            "geometry": self.web_geom.isChecked(),
            "delete": self.web_delete.isChecked(),
            "groups": self.web_groups.text().strip(),
        }

    def _web_triggers_on(self):
        def go():
            n = webedit.install_triggers(self.cfg)
            info = getattr(webedit.install_triggers, "last", {})
            self.pb_out.setText(
                "Triggers installed on %d table(s). Calculation rules run by"
                " the database itself (also for web edits): %s. Other rules"
                " run in QGIS / QField and on 'Check for edits made outside"
                " QGIS': %s."
                % (
                    n,
                    ", ".join(info.get("rules in the database", [])) or "none",
                    ", ".join(info.get("rules not translated", [])) or "none",
                )
            )
            self._msg("Triggers installed on %d table(s)." % n, "Success")

        self._run("Database triggers", go)

    def _web_triggers_off(self):
        self._run(
            "Database triggers",
            lambda: self._msg(
                "Triggers removed from %d table(s)."
                % webedit.remove_triggers(self.cfg)
            ),
        )

    def _web_auto_toggled(self, on):
        self._web_timer.stop()
        if on:
            self._web_timer.start(int(self.web_every.value() * 60000))

    def _web_report(self, res):
        if res["first"]:
            return (
                "The current state is saved as the reference. The next check"
                " will find the edits made outside QGIS from now on."
            )
        return (
            "Edits made outside QGIS since the previous check: %d added, %d"
            " changed, %d deleted. %d dirty area(s) marked (in the edit"
            " history too). Attribute rules: %d value(s) filled, %d broken."
            % (
                res["insert"],
                res["update"],
                res["delete"],
                res["areas"],
                res.get("rules: values filled", 0),
                res.get("rules: broken", 0),
            )
        )

    def _web_check(self, _c=False, quiet=False):
        if self.cfg is None:
            return
        if quiet:
            if self._unsaved():
                return  # never while the user has unsaved edits
            try:
                res = webedit.process_external_edits(
                    self.cfg, self.web_rules.isChecked()
                )
            except Exception as e:
                Q.log("Automatic check: %s" % e, True)
                return
            if res["insert"] or res["update"] or res["delete"]:
                self.pb_out.setText(self._web_report(res))
                self._net = None
                self._update_dirty_label()
                self._msg(self._web_report(res), "Info", 10)
                self.iface.mapCanvas().refresh()
            return

        def go():
            res = webedit.process_external_edits(
                self.cfg, self.web_rules.isChecked()
            )
            self.pb_out.setText(self._web_report(res))
            self._net = None
            self._update_dirty_label()
            self.iface.mapCanvas().refresh()

        self._run("Edits outside QGIS", go)

    def _web_reset(self):
        def go():
            webedit.reset_external_state(self.cfg)
            self.pb_out.setText("Current state saved as the reference.")

        self._run("Edits outside QGIS", go)

    # ============================================================ Topology
    def _tab_topology(self):
        from . import topology_rules as TR

        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "General topology rules between any layers of the project,"
                " like geodatabase topology: polygons that must not overlap"
                " or leave gaps, lines that must not cross, points that must"
                " lie on lines or inside polygons. Broken rules go to the"
                " error layers (codes T01 - T14) and can be marked as"
                " exceptions."
            )
        )
        form = QFormLayout()
        self.tr_rule = QComboBox()
        for key in TR.RULES:
            self.tr_rule.addItem(TR.describe(key), key)
        self.tr_a = QgsMapLayerComboBox()
        self.tr_b = QgsMapLayerComboBox()
        try:
            from .dock import _filter

            self.tr_a.setFilters(_filter("VectorLayer"))
            self.tr_b.setFilters(_filter("VectorLayer"))
        except Exception as e:
            Q.log("Layer filter: %s" % e, True)
        self.tr_b.setAllowEmptyLayer(True)
        self.tr_tol = _spin(0.01, 0, 1000, 3)
        form.addRow("Rule", self.tr_rule)
        form.addRow("Layer", self.tr_a)
        form.addRow("Second layer", self.tr_b)
        form.addRow("Tolerance", self.tr_tol)
        lay.addLayout(form)
        _buttons(
            lay,
            [
                ("Add rule", self._tr_add, ""),
                ("Remove selected", self._tr_remove, ""),
                (
                    "Save rules",
                    self._tr_save,
                    "Store the rules in the network.",
                ),
            ],
        )
        self.tr_table = _table(
            ["On", "Rule", "Layer", "Second layer", "Tolerance"], 200
        )
        lay.addWidget(self.tr_table)
        self.tr_selected = QCheckBox("Selected features only")
        lay.addWidget(self.tr_selected)
        rb = QPushButton("Check the topology rules")
        rb.clicked.connect(self._tr_run)
        lay.addWidget(rb)
        self.tr_out = QPlainTextEdit()
        self.tr_out.setReadOnly(True)
        self.tr_out.setMaximumHeight(150)
        lay.addWidget(self.tr_out)
        lay.addStretch(1)
        self._tr_rows = []
        return w

    def _tr_load(self):
        from . import topology_rules as TR

        try:
            rows = TR.load(self.cfg.path)
        except Exception:
            rows = []
        self._tr_fill(rows)

    def _tr_fill(self, rows=None):
        from . import topology_rules as TR

        if rows is not None:
            self._tr_rows = rows
        t = self.tr_table
        t.setRowCount(0)
        for r in self._tr_rows:
            i = t.rowCount()
            t.insertRow(i)
            la = TR.find_layer(r.get("layer_a"))
            lb = TR.find_layer(r.get("layer_b")) if r.get("layer_b") else None
            _set_row(
                t,
                i,
                [
                    "",
                    (
                        TR.describe(r["rule"])
                        if r["rule"] in TR.RULES
                        else r["rule"]
                    ),
                    la.name() if la else "(missing) " + str(r.get("layer_a")),
                    lb.name() if lb else "",
                    r.get("tolerance"),
                ],
            )
            it = QTableWidgetItem()
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(
                Qt.CheckState.Checked
                if r.get("enabled")
                else Qt.CheckState.Unchecked
            )
            t.setItem(i, 0, it)
        t.resizeColumnsToContents()

    def _tr_sync(self):
        for i, r in enumerate(self._tr_rows):
            it = self.tr_table.item(i, 0)
            if it is not None:
                r["enabled"] = (
                    1 if it.checkState() == Qt.CheckState.Checked else 0
                )

    def _tr_add(self):
        from . import topology_rules as TR

        key = self.tr_rule.currentData()
        la, lb = self.tr_a.currentLayer(), self.tr_b.currentLayer()
        _c, _l, ga, gb = TR.RULES[key]
        if la is None:
            QMessageBox.information(self, "Topology rules", "Choose a layer.")
            return
        if ga != "any" and Q.gkind(la) != ga:
            QMessageBox.warning(
                self, "Topology rules", "This rule needs a %s layer." % ga
            )
            return
        if gb and (lb is None or Q.gkind(lb) != gb):
            QMessageBox.warning(
                self,
                "Topology rules",
                "This rule needs a second layer of %ss." % gb,
            )
            return
        self._tr_sync()
        self._tr_rows.append(
            {
                "rule": key,
                "layer_a": la.source(),
                "layer_b": lb.source() if (gb and lb is not None) else None,
                "tolerance": self.tr_tol.value(),
                "enabled": 1,
            }
        )
        self._tr_fill()

    def _tr_remove(self):
        i = self.tr_table.currentRow()
        if 0 <= i < len(self._tr_rows):
            self._tr_sync()
            del self._tr_rows[i]
            self._tr_fill()

    def _tr_save(self):
        from . import topology_rules as TR

        def go():
            self._tr_sync()
            TR.save(self.cfg.path, self._tr_rows)
            self._msg(
                "%d topology rule(s) saved." % len(self._tr_rows), "Success"
            )

        self._run("Topology rules", go)

    def _tr_run(self):
        from . import topology_rules as TR

        def go():
            self._tr_sync()
            issues, report, errors = TR.run_all(
                self._tr_rows, self.tr_selected.isChecked()
            )
            Q.write_errors(self.cfg, issues, codes=("T",))
            lines = ["Topology rules", ""]
            lines += ["  %5d  %s" % (n, text) for text, n in report]
            if errors:
                lines += ["", "Not checked:"] + ["  " + e for e in errors]
            lines += [
                "",
                "%d error(s) written to the error layers (codes T01 - T14)."
                % len(issues),
            ]
            self.tr_out.setPlainText("\n".join(lines))
            if Q.project_layer(self.cfg.path, "un_errors_point") is None:
                Q.load_network(self.cfg, self.iface)
            self.iface.mapCanvas().refresh()

        self._run("Topology rules", go)

    # ============================================================ hooks
    def _extra_refresh(self):
        for fn in (
            self._ver_fill,
            self._hy_fill,
            self._web_fill,
            self._tr_load,
        ):
            try:
                fn()
            except Exception as e:
                Q.log("Refresh %s: %s" % (fn.__name__, e), True)
        self._ar_fill(list(getattr(self.cfg, "attribute_rules", [])))

    def _check_rules_on_commit(self, lyr, cls):
        """Warn when uncommitted edits break constraint / validation
        rules."""
        if self.cfg is None or not getattr(self.cfg, "attribute_rules", None):
            return
        try:
            broken = AR.check_edits(
                self.cfg, lyr, cls, self.cfg.attribute_rules
            )
        except Exception as e:
            Q.log("Attribute rules: %s" % e, True)
            return
        if broken:
            self._msg(
                "Edits break attribute rules: %s"
                % "; ".join(
                    "%s (%d)" % (name, n) for name, _m, n in broken[:4]
                ),
                "Warning",
                10,
            )

    @staticmethod
    def version_label(path):
        try:
            v = V.info(path).get("version")
        except Exception:
            return ""
        return " [version %s]" % v if v else ""


def network_title(cfg):
    return (cfg.settings.get("name") or cfg.tpl["label"]) + (
        ExtraPages.version_label(cfg.path)
    )
