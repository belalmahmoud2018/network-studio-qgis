"""Main panel of Network Studio."""

import os
import traceback
from datetime import datetime

from qgis.core import (
    Qgis,
    QgsFeatureRequest,
    QgsMapLayerProxyModel,
    QgsProject,
    QgsSettings,
    QgsVectorLayer,
)
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtCore import QDate, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QCursor, QDesktopServices, QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QInputDialog,
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
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import json

from . import (
    asset_package,
    backup,
    cost,
    demo,
    diagrams,
    esri_import,
    history,
    hydraulics,
    importer,
    mapbook,
    prepare,
    profile,
    publish,
    reports,
    risk,
    services,
    tools,
    workorders,
)
from . import templates as T
from .editing import SmartEditor
from . import qgis_io as Q
from . import storage as S
from .maptool import PickTool
from .new_network import NewNetworkDialog, PgDialog

TRACES = [
    ("connected", "Connected"),
    ("upstream", "Upstream (main feed path)"),
    ("upstream_all", "Upstream (all paths, loops)"),
    ("downstream", "Downstream"),
    ("isolation", "Isolation (which devices to close)"),
    ("shortest_path", "Shortest path"),
    ("loops", "Loops"),
    ("subnetwork", "Subnetwork"),
    ("controllers", "Subnetwork controllers"),
]


def _filter(name):
    v = getattr(Qgis, "LayerFilter", None)
    if v is not None and hasattr(v, name):
        return getattr(v, name)
    v = getattr(QgsMapLayerProxyModel, "Filter", None)
    if v is not None and hasattr(v, name):
        return getattr(v, name)
    return getattr(QgsMapLayerProxyModel, name)


def _scroll(widget):
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setWidget(widget)
    return sa


def _spin(value, lo=0.0, hi=100000.0, dec=3, suffix=""):
    s = QDoubleSpinBox()
    s.setDecimals(dec)
    s.setRange(lo, hi)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    return s


class Busy:
    def __enter__(self):
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))

    def __exit__(self, *a):
        QApplication.restoreOverrideCursor()


class NetworkDock(QDialog):
    """Floating, non-modal window: the map stays usable (needed to pick trace
    points)."""

    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.setWindowTitle("Network Studio")
        self.setObjectName("NetworkStudioWindow")
        self.setModal(False)
        self.iface = iface
        QgsProject.instance().layersAdded.connect(self._watch_layers)
        self.iface = iface
        self.cfg = None
        self.picks = []
        self.tool = None
        self.last_result = None
        self._net = None
        self._watched = []
        self._dirty = []
        self._dirty_fids = {}
        self.editor = SmartEditor(iface)
        self.recorder = history.Recorder()

        pages = [
            ("OVERVIEW", None),
            ("Dashboard", self._tab_dashboard),
            ("1  SET UP", None),
            ("Network", self._tab_network),
            ("Model", self._tab_model),
            ("Company and branding", self._tab_branding),
            ("2  DATA", None),
            ("Import", self._tab_data),
            ("3  EDIT", None),
            ("Editing", self._tab_editing),
            ("Asset IDs and fields", self._tab_fields),
            ("4  QUALITY", None),
            ("Prepare and validate", self._tab_validate),
            ("Associations", self._tab_assoc),
            ("5  ANALYSIS", None),
            ("Trace", self._tab_trace),
            ("Subnetworks", self._tab_subnetworks),
            ("Diagrams", self._tab_diagrams),
            ("Profiles", self._tab_profiles),
            ("Criticality and risk", self._tab_risk),
            ("Hydraulic model", self._tab_export),
            ("6  DESIGN", None),
            ("Service connections", self._tab_services),
            ("Cost estimate", self._tab_cost),
            ("7  OPERATIONS", None),
            ("Work orders", self._tab_workorders),
            ("Edit history", self._tab_history),
            ("8  DELIVER", None),
            ("Reports", self._tab_reports),
            ("Map book", self._tab_mapbook),
            ("Web publishing", self._tab_publish),
            ("ArcGIS asset package", self._tab_assetpkg),
        ]
        self.nav = QListWidget()
        self.nav.setFixedWidth(170)
        self.stack = QStackedWidget()
        self.pages = []
        bold = QFont()
        bold.setBold(True)
        for title, builder in pages:
            it = QListWidgetItem(title)
            if builder is None:
                it.setFlags(Qt.ItemFlag.NoItemFlags)
                it.setFont(bold)
                it.setForeground(QColor("#0f6e56"))
                it.setData(Qt.ItemDataRole.UserRole, -1)
            else:
                it.setText("    " + title)
                it.setData(Qt.ItemDataRole.UserRole, self.stack.count())
                page = _scroll(builder())
                self.stack.addWidget(page)
                self.pages.append((title, page))
            self.nav.addItem(it)
        for (
            _t,
            page,
        ) in self.pages:  # long explanations wrap instead of widening the page
            for lab in page.widget().findChildren(QLabel):
                if len(lab.text()) > 45:
                    lab.setWordWrap(True)
        self.nav.currentItemChanged.connect(self._nav_changed)
        body = QHBoxLayout()
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        outer = QVBoxLayout(self)
        outer.addLayout(body)
        self.status = QLabel("No network open.")
        self.status.setStyleSheet("color: #666;")
        outer.addWidget(self.status)
        self.goto("Network")
        self.resize(940, 780)
        self._enable(False)
        self._fill_recent()

    def _nav_changed(self, cur, _prev):
        if cur is None:
            return
        i = cur.data(Qt.ItemDataRole.UserRole)
        if i is not None and i >= 0:
            self.stack.setCurrentIndex(i)

    def goto(self, title):
        for r in range(self.nav.count()):
            if self.nav.item(r).text().strip() == title:
                self.nav.setCurrentRow(r)

    # ================================================================ network
    # cache
    def network(self):
        """The connectivity graph, rebuilt only after the network data
        changed."""
        if self._net is None:
            self._net = Q.read_network(self.cfg)
        return self._net

    def _invalidate(self, *_a):
        self._net = None

    NETWORK_FIELDS = {
        "assetgroup",
        "assettype",
        "operatingstatus",
        "lifecyclestatus",
        "flowdirection",
        "oneway",
        "f_elev",
        "t_elev",
    }

    def _dirty_attribute(self, lyr, fid, idx):
        """Only fields that change connectivity or tracing make a dirty area
        (not computed fields
        such as risk, subnetwork or isconnected, nor notes / tracking fields).
        """
        try:
            name = lyr.fields().at(idx).name().lower()
        except Exception:
            return
        if name in self.NETWORK_FIELDS:
            self._dirty_feature(lyr, fid)

    def _dirty_feature(self, lyr, fid, geom=None):
        self._net = None
        if self.cfg is None or not self.cfg.topology_enabled:
            return
        if geom is not None and not geom.isNull():
            box = geom.boundingBox()
            box.grow(max(self.cfg.gap, 0.001))
            self._dirty.append(box)
        else:
            self._dirty_fids.setdefault(lyr.id(), (lyr, set()))[1].add(
                fid
            )  # located once, at commit

    def _resolve_dirty(self):
        for lyr, fids in self._dirty_fids.values():
            try:
                for f in lyr.getFeatures(
                    QgsFeatureRequest().setFilterFids(list(fids))
                ):
                    if f.hasGeometry():
                        box = f.geometry().boundingBox()
                        box.grow(max(self.cfg.gap, 0.001))
                        self._dirty.append(box)
            except RuntimeError:
                pass
        self._dirty_fids = {}

    def _commit_dirty(self):
        self._net = None
        self._resolve_dirty()
        if len(self._dirty) > 200:  # bulk edits: one area around everything
            box = self._dirty[0]
            for b in self._dirty[1:]:
                box.combineExtentWith(b)
            self._dirty = [box]
        if self._dirty and self.cfg is not None:
            try:
                Q.write_dirty_areas(self.cfg, self._dirty)
            except Exception as e:
                Q.log("Dirty areas: %s" % e, True)
            self._dirty = []
            self._update_dirty_label()

    def _watch_layers(self, *_a):
        for lyr, slots in self._watched:
            for sig, slot in slots:
                try:
                    getattr(lyr, sig).disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
        self._watched = []
        if self.cfg is None:
            return
        for cls in self.cfg.classes.values():
            lyr = Q.project_layer(self.cfg.path, cls)
            if lyr is None:
                continue
            slots = [
                (sig, self._invalidate)
                for sig in ("featureDeleted", "afterRollBack", "dataChanged")
            ]
            slots += [
                (
                    "featureAdded",
                    lambda fid, l=lyr: self._dirty_feature(l, fid),  # noqa
                ),
                (
                    "geometryChanged",
                    lambda fid, g, l=lyr: self._dirty_feature(l, fid, g),  # noqa
                ),
                (
                    "attributeValueChanged",
                    lambda fid, i, v, l=lyr: self._dirty_attribute(l, fid, i),  # noqa
                ),
                (
                    "afterCommitChanges",
                    lambda *_a: QTimer.singleShot(0, self._commit_dirty),
                ),
            ]
            for sig, slot in slots:
                getattr(lyr, sig).connect(slot)
            self._watched.append((lyr, slots))
        self._net = None
        if self.editor.enabled:
            self.editor.set_network(self.cfg)
        self.recorder.attach(self.cfg)

    # ================================================================ helpers
    def _msg(self, text, level="Info", duration=6):
        lvl = getattr(Qgis, level, None)
        if lvl is None:
            lvl = getattr(Qgis.MessageLevel, level)
        self.iface.messageBar().pushMessage(
            "Network Studio", text, lvl, duration
        )

    def _run(self, title, fn, need_cfg=True):
        if need_cfg and self.cfg is None:
            QMessageBox.information(
                self, title, "Open or create a network first (Network tab)."
            )
            return None
        try:
            with Busy():
                return fn()
        except (S.StoreError, ValueError) as e:
            QMessageBox.warning(self, title, str(e))
        except Exception:
            Q.log("%s\n%s" % (title, traceback.format_exc()), True)
            QMessageBox.critical(
                self,
                title,
                "Unexpected error. Details are in the Log Messages panel "
                "(Network Studio).\n\n%s"
                % traceback.format_exc(limit=1),
            )
        return None

    def _enable(self, on):
        for title, page in self.pages:
            if title not in ("Network", "Company and branding"):
                page.setEnabled(on)

    def _group_combo(
        self, combo, role, type_combo=None, category=None, name=None
    ):
        combo.clear()
        if self.cfg is None or role not in self.cfg.classes:
            return
        cls = self.cfg.classes[role]
        for ag, gname in self.cfg.groups(cls):
            combo.addItem("%d  %s" % (ag, gname), ag)
        found = (
            self.cfg.find_group(role, category=category, name=name)
            if (category or name)
            else None
        )
        if found:
            combo.setCurrentIndex(max(0, combo.findData(found[1])))
        if type_combo is not None:

            def refill(_i=0):
                type_combo.clear()
                ag = combo.currentData()
                for at, tname in (
                    self.cfg.types(cls, ag) if ag is not None else []
                ):
                    type_combo.addItem("%d  %s" % (at, tname), at)

            try:
                combo.currentIndexChanged.disconnect()
            except TypeError:
                pass
            combo.currentIndexChanged.connect(refill)
            refill()

    # ================================================================ Network
    def _tab_network(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        new = QPushButton("New network...")
        new.setToolTip(
            "Choose the network type (water, sewer, electricity, fiber,"
            " roads...) and create it."
        )
        new.clicked.connect(self._new_network)
        lay.addWidget(new)
        dm = QPushButton("Create a demo network...")
        dm.setToolTip(
            "A realistic sample network of any type (streets, mains, devices,"
            " buildings, services) to learn the tools or to present them to a"
            " client."
        )
        dm.clicked.connect(self._demo)
        lay.addWidget(dm)
        box = QGroupBox("Open a network")
        bl = QVBoxLayout(box)
        rrow = QHBoxLayout()
        self.recent = QComboBox()
        self.recent.setToolTip("Networks opened recently")
        ro = QPushButton("Open recent")
        ro.clicked.connect(
            lambda: (
                self.open_network(self.recent.currentData(), add_layers=True)
                if self.recent.currentData()
                else None
            )
        )
        rrow.addWidget(self.recent, 1)
        rrow.addWidget(ro)
        bl.addLayout(rrow)
        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText(".gpkg, .sqlite or .gdb")
        b1 = QPushButton("File...")
        b1.clicked.connect(self._browse_file)
        b2 = QPushButton(".gdb...")
        b2.clicked.connect(self._browse_gdb)
        b3 = QPushButton("PostGIS...")
        b3.setToolTip(
            "Open a network stored in PostgreSQL / PostGIS (several users at"
            " the same time)."
        )
        b3.clicked.connect(self._browse_pg)
        row.addWidget(self.path_edit, 1)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addWidget(b3)
        bl.addLayout(row)
        row2 = QHBoxLayout()
        op = QPushButton("Open")
        op.clicked.connect(
            lambda: self.open_network(self.path_edit.text().strip())
        )
        fl = QPushButton("From selected layer")
        fl.setToolTip(
            "Use the network file of the layer selected in the Layers panel."
        )
        fl.clicked.connect(self._from_layer)
        row2.addWidget(op)
        row2.addWidget(fl)
        bl.addLayout(row2)
        lay.addWidget(box)
        self.info = QLabel("No network open.")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        lay.addWidget(self.info)
        add = QPushButton("Add network layers to the map")
        add.clicked.connect(
            lambda: (
                self._run(
                    "Add layers", lambda: Q.load_network(self.cfg, self.iface)
                ),
                self._watch_layers(),
            )
        )
        lay.addWidget(add)
        kbox = QGroupBox("Backups")
        kl = QVBoxLayout(kbox)
        self.auto_backup = QCheckBox(
            "Back up automatically before automatic edits (prepare, import,"
            " services, model)"
        )
        self.auto_backup.setChecked(True)
        kl.addWidget(self.auto_backup)
        krow = QHBoxLayout()
        bk = QPushButton("Back up now")
        bk.clicked.connect(
            lambda: self._run(
                "Backup",
                lambda: self._msg(
                    "Backup: %s" % backup.make_backup(self.cfg.path, "manual"),
                    "Success",
                ),
            )
        )
        of = QPushButton("Open backups folder")
        of.clicked.connect(self._open_backups)
        krow.addWidget(bk)
        krow.addWidget(of)
        kl.addLayout(krow)
        fp = QPushButton(
            "Save a QGIS project next to the data (QField / field work)"
        )
        fp.setToolTip(
            "Saves the current project with relative paths in the network"
            " folder, ready for QField."
        )
        fp.clicked.connect(self._save_field_project)
        kl.addWidget(fp)
        lay.addWidget(kbox)
        tbox = QGroupBox("Network topology")
        tl = QVBoxLayout(tbox)
        self.topo_label = QLabel()
        self.topo_label.setWordWrap(True)
        tl.addWidget(self.topo_label)
        trow = QHBoxLayout()
        en = QPushButton("Enable network topology")
        en.setToolTip(
            "Builds the topology: full validation, then traces and subnetworks"
            " are available."
        )
        en.clicked.connect(lambda: self._set_topology(True))
        dis = QPushButton("Disable network topology")
        dis.setToolTip(
            "For large bulk edits / loads: no dirty areas, traces off, smart"
            " editing off."
        )
        dis.clicked.connect(lambda: self._set_topology(False))
        trow.addWidget(en)
        trow.addWidget(dis)
        tl.addLayout(trow)
        lay.addWidget(tbox)
        sbox = QGroupBox("Settings")
        sf = QFormLayout(sbox)
        self.tol_spin = _spin(0.001, 0.0001, 10, 4)
        self.gap_spin = _spin(0.5, 0.001, 1000, 3)
        sf.addRow("Connectivity tolerance", self.tol_spin)
        sf.addRow("Gap distance", self.gap_spin)
        save = QPushButton("Save settings")
        save.clicked.connect(self._save_settings)
        sf.addRow("", save)
        lay.addWidget(sbox)
        lay.addStretch(1)
        return w

    def _new_network(self):
        dlg = NewNetworkDialog(self)
        if dlg.exec() and dlg.created_path:
            self.open_network(dlg.created_path, add_layers=True)

    def _browse_file(self):
        path, _f = QFileDialog.getOpenFileName(
            self,
            "Network file",
            "",
            "Network (*.gpkg *.sqlite *.db *.sqlite3)",
        )
        if path:
            self.path_edit.setText(path)
            self.open_network(path)

    def _browse_gdb(self):
        path = QFileDialog.getExistingDirectory(
            self, "File Geodatabase (.gdb folder)"
        )
        if path:
            self.path_edit.setText(path)
            self.open_network(path)

    def _browse_pg(self):
        dlg = PgDialog(self, self.path_edit.text())
        if dlg.exec():
            self.path_edit.setText(dlg.connection())
            self.open_network(dlg.connection(), add_layers=True)

    def _from_layer(self):
        lyr = self.iface.activeLayer()
        if not isinstance(lyr, QgsVectorLayer):
            QMessageBox.information(
                self,
                "Network",
                "Select a layer of the network in the Layers panel.",
            )
            return
        path, _n = Q.layer_source(lyr)
        self.path_edit.setText(path)
        self.open_network(path)

    def open_network(self, path, add_layers=False):
        if not path:
            return
        if "#" not in path:
            try:
                domains = S.list_domains(path)
            except S.StoreError:
                domains = []
            if len(domains) > 1:
                labels = []
                for dname in domains:
                    p = path if dname is None else "%s#%s" % (path, dname)
                    try:
                        labels.append(
                            "%s  (%s)"
                            % (
                                S.load_config(p).settings.get("name", ""),
                                dname or "main",
                            )
                        )
                    except S.StoreError:
                        labels.append(dname or "main")
                choice, ok = QInputDialog.getItem(
                    self,
                    "Domain networks",
                    "This file holds several networks. Open:",
                    labels,
                    0,
                    False,
                )
                if not ok:
                    return
                dname = domains[labels.index(choice)]
                path = path if dname is None else "%s#%s" % (path, dname)
        try:
            cfg = S.load_config(path)
        except S.StoreError as e:
            QMessageBox.warning(self, "Network", str(e))
            return
        self.cfg = cfg
        self.path_edit.setText(path)
        self.tol_spin.setValue(cfg.tolerance)
        self.gap_spin.setValue(cfg.gap)
        tpl = cfg.tpl
        self.info.setText(
            "<b>%s</b><br>Type: %s<br>Flow: %s<br>Classes: %s<br>Rules: %d,"
            " associations: %d"
            % (
                cfg.settings.get("name", ""),
                tpl["label"],
                cfg.flow,
                ", ".join(cfg.classes.values()),
                len(cfg.rules),
                len(cfg.associations),
            )
        )
        self._enable(True)
        self._refresh_forms()
        if add_layers:
            self._run(
                "Add layers", lambda: Q.load_network(self.cfg, self.iface)
            )
        self._watch_layers()
        self._remember(path)
        try:
            workorders.ensure_layer(
                cfg.path,
                cfg.classes.get("Line") or list(cfg.classes.values())[0],
            )
        except Exception as e:
            Q.log("Work orders: %s" % e, True)
        self._refresh_wo()
        self.status.setText(
            "Network: %s  |  %s  |  %s"
            % (cfg.settings.get("name", ""), tpl["label"], path)
        )
        if "Line" in cfg.classes:
            lyr = Q.get_layer(cfg.path, cfg.classes["Line"])
            if lyr.fields().indexFromName("created_user") < 0:
                self._msg(
                    "This network was made by an older version: use Editing >"
                    " Upgrade network.",
                    "Warning",
                    10,
                )
        self._msg(
            "Network opened: %s"
            % (
                S.pg_label(path)
                if S.is_pg(path)
                else os.path.basename(path.rstrip("/\\"))
            )
        )

    def _remember(self, path):
        st = QgsSettings()
        items = [
            p
            for p in st.value("NetworkStudio/recent", []) or []
            if p and p != path
        ]
        items = [path] + items[:9]
        st.setValue("NetworkStudio/recent", items)
        self._fill_recent()

    def _fill_recent(self):
        import os as _os

        self.recent.clear()
        for p in QgsSettings().value("NetworkStudio/recent", []) or []:
            if p and S.is_pg(p):
                self.recent.addItem("PostGIS  -  " + S.pg_label(p), p)
            elif p and _os.path.exists(S.split_domain(p)[0]):
                self.recent.addItem(
                    _os.path.basename(p.rstrip("/\\")) + "  -  " + p, p
                )

    def _open_backups(self):
        if self.cfg is None:
            return
        import os as _os

        folder = backup.backup_dir(self.cfg.path)
        _os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _auto(self, reason):
        if self.cfg is not None and self.auto_backup.isChecked():
            try:
                backup.make_backup(self.cfg.path, reason)
                backup.prune(self.cfg.path)
            except Exception as e:
                Q.log("Automatic backup failed: %s" % e, True)

    def _save_field_project(self):
        def go():
            import os as _os

            base = S.split_domain(self.cfg.path)[0]
            folder = (
                backup.backup_dir(base)
                if S.is_pg(base)
                else _os.path.dirname(base.rstrip("/\\"))
            )
            _os.makedirs(folder, exist_ok=True)
            name = (self.cfg.settings.get("name") or "network").replace(
                " ", "_"
            ) + "_field.qgz"
            prj = QgsProject.instance()
            try:
                prj.setFilePathStorage(Qgis.FilePathType.Relative)
            except AttributeError:
                prj.writeEntryBool("Paths", "/Absolute", False)
            path = _os.path.join(folder, name)
            if not prj.write(path):
                raise ValueError(
                    "Could not save the project: %s" % prj.error()
                )
            self._msg("Project saved for field work: %s" % path, "Success")

        self._run("Field project", go)

    def _set_topology(self, on):
        if self.cfg is None:
            return

        def go():
            S.set_setting(
                self.cfg.path, "topology_enabled", "1" if on else "0"
            )
            self._reload_cfg()
            if on:
                self._validate(full=True, quiet=True)
            else:
                self.e_on.setChecked(False)
            self._update_topology_label()
            self._msg(
                "Network topology %s." % ("enabled" if on else "disabled"),
                "Success",
            )

        self._run("Network topology", go)

    def _update_topology_label(self):
        if self.cfg is None:
            return
        on = self.cfg.topology_enabled
        self.topo_label.setText(
            "Topology is <b>%s</b>. %s"
            % (
                "enabled" if on else "disabled",
                (
                    "Edits create dirty areas; traces and subnetworks are"
                    " available."
                    if on
                    else (
                        "Enable it after bulk edits: it validates the whole"
                        " network."
                    )
                ),
            )
        )

    def _require_topology(self):
        if self.cfg is not None and not self.cfg.topology_enabled:
            raise ValueError(
                "The network topology is disabled. Enable it on the Network"
                " page first."
            )

    def _reload_cfg(self):
        if self.cfg is not None:
            self.cfg = S.load_config(self.cfg.path)
            Q.clear_cache(self.cfg.path)
            self._net = None
            if self.editor.enabled:
                self.editor.set_network(self.cfg)

    def _save_settings(self):
        def go():
            S.set_setting(
                self.cfg.path, "tolerance", repr(self.tol_spin.value())
            )
            S.set_setting(self.cfg.path, "gap", repr(self.gap_spin.value()))
            self._reload_cfg()
            self._msg("Settings saved.")

        self._run("Settings", go)

    def _refresh_forms(self):
        cfg = self.cfg
        # data tab
        self.imp_role.clear()
        for role, cls in cfg.classes.items():
            self.imp_role.addItem("%s (%s)" % (cls, role), role)
        # services tab
        svc = cfg.tpl.get("service", {})
        self._group_combo(
            self.svc_line,
            "Line",
            self.svc_line_t,
            category="service",
            name=svc.get("line"),
        )
        self._group_combo(
            self.svc_tap,
            "Junction",
            self.svc_tap_t,
            category="tap",
            name=svc.get("tap"),
        )
        self._group_combo(
            self.svc_end,
            "Device",
            self.svc_end_t,
            category="customer",
            name=svc.get("end"),
        )
        self.svc_mains.clear()
        cls = cfg.classes.get("Line")
        for ag, gname in cfg.groups(cls) if cls else []:
            it = QListWidgetItem("%d  %s" % (ag, gname))
            it.setData(Qt.ItemDataRole.UserRole, ag)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(
                Qt.CheckState.Checked
                if "main" in cfg.cats.get((cls, ag), set())
                else Qt.CheckState.Unchecked
            )
            self.svc_mains.addItem(it)
        self._imp_role_changed()
        self._model_load()
        self._fill_trace_configs()
        self._fill_conditions_attrs()
        self._fill_controllers()
        self._update_topology_label()
        self._update_dirty_label()
        self._update_assoc_label()
        self._fill_diagram_subs()
        self._fill_field_tools()
        self._fill_prices()
        self.svc_hint.setText(
            "Service: %(line)s, tap: %(tap)s, end device: %(end)s"
            % cfg.tpl["service"]
        )
        self._fill_subnetworks()

    # ================================================================ Data
    def _tab_data(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Import existing layers into the network classes. Fields with"
                " the same name are copied; the asset group / type come from a"
                " fixed choice or from a field."
            )
        )
        form = QFormLayout()
        self.imp_src = QgsMapLayerComboBox()
        self.imp_src.setFilters(_filter("VectorLayer"))
        form.addRow("Source layer", self.imp_src)
        self.imp_sel = QCheckBox("Selected features only")
        form.addRow("", self.imp_sel)
        self.imp_role = QComboBox()
        form.addRow("Target class", self.imp_role)
        lay.addLayout(form)
        self.imp_fixed = QRadioButton("Same asset group for all features")
        self.imp_byfield = QRadioButton("Asset group from a field")
        self.imp_byfield.setChecked(True)
        lay.addWidget(self.imp_fixed)
        fixed = QHBoxLayout()
        self.imp_ag, self.imp_at = QComboBox(), QComboBox()
        fixed.addWidget(self.imp_ag, 1)
        fixed.addWidget(self.imp_at, 1)
        lay.addLayout(fixed)
        lay.addWidget(self.imp_byfield)
        frow = QHBoxLayout()
        self.imp_field = QgsFieldComboBox()
        sug = QPushButton("Read values and suggest")
        sug.clicked.connect(self._suggest)
        frow.addWidget(self.imp_field, 1)
        frow.addWidget(sug)
        lay.addLayout(frow)
        self.imp_table = QTableWidget(0, 3)
        self.imp_table.setHorizontalHeaderLabels(
            ["Value in source", "Asset group", "Asset type"]
        )
        self.imp_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.imp_table.setMinimumHeight(180)
        lay.addWidget(self.imp_table)
        go = QPushButton("Import")
        go.clicked.connect(self._import)
        lay.addWidget(go)
        abox = QGroupBox("Import from ArcGIS (File Geodatabase)")
        al = QVBoxLayout(abox)
        al.addWidget(
            QLabel(
                "Utility network classes (ASSETGROUP / ASSETTYPE / GLOBALID)"
                " keep their codes; street data of Network Analyst (ONEWAY,"
                " F_ELEV / T_ELEV, FRC) goes into a Road network."
            )
        )
        arow = QHBoxLayout()
        ab = QPushButton("Read a .gdb...")
        ab.clicked.connect(self._esri_read)
        arow.addWidget(ab)
        ai = QPushButton("Import checked layers")
        ai.clicked.connect(self._esri_import)
        arow.addWidget(ai)
        al.addLayout(arow)
        self.es_table = QTableWidget(0, 4)
        self.es_table.setHorizontalHeaderLabels(
            ["Layer", "Kind", "Features", "Import into"]
        )
        self.es_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.es_table.setMinimumHeight(160)
        al.addWidget(self.es_table)
        asc = QPushButton(
            "Import associations (CSV from 'Export Associations')..."
        )
        asc.clicked.connect(self._esri_assoc)
        al.addWidget(asc)
        lay.addWidget(abox)
        self.imp_src.layerChanged.connect(self.imp_field.setLayer)
        self.imp_field.setLayer(self.imp_src.currentLayer())
        self.imp_role.currentIndexChanged.connect(self._imp_role_changed)
        lay.addStretch(1)
        return w

    def _imp_role_changed(self, _i=0):
        role = self.imp_role.currentData()
        if role:
            self._group_combo(self.imp_ag, role, self.imp_at)
        self.imp_table.setRowCount(0)

    def _suggest(self):
        lyr, field, role = (
            self.imp_src.currentLayer(),
            self.imp_field.currentField(),
            self.imp_role.currentData(),
        )
        if not (lyr and field and role and self.cfg):
            return
        vals = importer.distinct_values(lyr, field)
        sug = importer.suggest_mapping(self.cfg, role, vals)
        cls = self.cfg.classes[role]
        self.imp_table.setRowCount(0)
        for v in vals:
            r = self.imp_table.rowCount()
            self.imp_table.insertRow(r)
            item = QTableWidgetItem(str(v))
            item.setData(Qt.ItemDataRole.UserRole, v)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.imp_table.setItem(r, 0, item)
            gc, tc = QComboBox(), QComboBox()
            gc.addItem("(skip)", None)
            for ag, gname in self.cfg.groups(cls):
                gc.addItem("%d  %s" % (ag, gname), ag)

            def refill(_i=0, gc=gc, tc=tc):
                tc.clear()
                if gc.currentData() is not None:
                    for at, tname in self.cfg.types(cls, gc.currentData()):
                        tc.addItem("%d  %s" % (at, tname), at)

            gc.currentIndexChanged.connect(refill)
            ag, at, _s = sug.get(v, (None, None, 0))
            gc.setCurrentIndex(
                max(0, gc.findData(ag)) if ag is not None else 0
            )
            refill()
            if at is not None:
                tc.setCurrentIndex(max(0, tc.findData(at)))
            self.imp_table.setCellWidget(r, 1, gc)
            self.imp_table.setCellWidget(r, 2, tc)

    def _import(self):
        def go():
            src, role = (
                self.imp_src.currentLayer(),
                self.imp_role.currentData(),
            )
            if src is None or role is None:
                raise ValueError(
                    "Choose the source layer and the target class."
                )
            self._auto("import")
            if self.imp_byfield.isChecked():
                if self.imp_table.rowCount() == 0:
                    raise ValueError("Press 'Read values and suggest' first.")
                mapping = {}
                for r in range(self.imp_table.rowCount()):
                    v = self.imp_table.item(r, 0).data(
                        Qt.ItemDataRole.UserRole
                    )
                    ag = self.imp_table.cellWidget(r, 1).currentData()
                    at = self.imp_table.cellWidget(r, 2).currentData()
                    if ag is not None:
                        mapping[v] = (ag, at or 1)
                res = importer.run(
                    self.cfg,
                    role,
                    src,
                    self.imp_field.currentField(),
                    mapping,
                    selected_only=self.imp_sel.isChecked(),
                )
            else:
                res = importer.run(
                    self.cfg,
                    role,
                    src,
                    fixed=(
                        self.imp_ag.currentData(),
                        self.imp_at.currentData(),
                    ),
                    selected_only=self.imp_sel.isChecked(),
                )
            QMessageBox.information(
                self,
                "Import",
                "Imported: %(imported)d\nNo asset group (skipped):"
                " %(unmapped)d\nWithout usable geometry: %(skipped)d\n\nThe"
                " new features are in edit mode when the layer is on the map:"
                " review and save." % res,
            )

        self._run("Import", go)

    # ================================================================ Prepare
    # / validate
    def _tab_validate(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        pbox = QGroupBox("1. Prepare the network (automatic)")
        pl = QVBoxLayout(pbox)
        pl.addWidget(
            QLabel(
                "Closes gaps up to the gap distance and adds the vertices"
                " needed for connectivity."
            )
        )
        self.prep_points = QCheckBox("Move points onto the nearest line")
        self.prep_points.setChecked(True)
        self.prep_ends = QCheckBox(
            "Move line ends onto points / line ends / lines"
        )
        self.prep_ends.setChecked(True)
        pl.addWidget(self.prep_points)
        pl.addWidget(self.prep_ends)
        pb = QPushButton("Prepare network")
        pb.clicked.connect(self._prepare)
        pl.addWidget(pb)
        lay.addWidget(pbox)

        vbox = QGroupBox("2. Validate")
        vl = QVBoxLayout(vbox)
        self.val_dangles = QCheckBox("Report dangling line ends")
        self.val_dangles.setChecked(True)
        self.val_islands = QCheckBox("Report areas not fed by any source")
        self.val_islands.setChecked(True)
        self.val_rules = QCheckBox("Check connectivity rules")
        self.val_rules.setChecked(True)
        self.val_required = QCheckBox("Check required fields (Model page)")
        self.val_required.setChecked(True)
        self.val_extra = QCheckBox(
            "Extra checks: duplicate asset ids, overlapping / crossing / very"
            " short lines"
        )
        self.val_extra.setChecked(True)
        for c in (
            self.val_dangles,
            self.val_islands,
            self.val_rules,
            self.val_required,
            self.val_extra,
        ):
            vl.addWidget(c)
        vb = QPushButton("Validate network")
        vb.clicked.connect(self._validate)
        vl.addWidget(vb)
        lay.addWidget(vbox)

        self.val_dirty = QCheckBox(
            "Dirty areas only (faster after small edits)"
        )
        self.val_subs = QCheckBox(
            "Check subnetwork rules (one controller per feature, controller"
            " required)"
        )
        self.val_subs.setChecked(True)
        vl2 = vbox.layout()
        vl2.insertWidget(vl2.count() - 1, self.val_subs)
        vl2.insertWidget(vl2.count() - 1, self.val_dirty)
        self.dirty_label = QLabel()
        vl2.addWidget(self.dirty_label)
        dc = QPushButton("Clear dirty areas")
        dc.clicked.connect(self._clear_dirty)
        vl2.addWidget(dc)

        xbox = QGroupBox("Errors that are accepted (exceptions)")
        xl = QHBoxLayout(xbox)
        mk = QPushButton("Mark selected errors as exceptions")
        mk.setToolTip(
            "Select features in the Errors layers first. Exceptions are not"
            " reported again."
        )
        mk.clicked.connect(self._mark_exceptions)
        cl = QPushButton("Clear all exceptions")
        cl.clicked.connect(self._clear_exceptions)
        xl.addWidget(mk)
        xl.addWidget(cl)
        lay.addWidget(xbox)

        ybox = QGroupBox("Verify network topology")
        yl = QVBoxLayout(ybox)
        yl.addWidget(
            QLabel(
                "Checks that the data matches the network definition (asset"
                " groups / types, rules, associations, controllers, terminals,"
                " network attributes)."
            )
        )
        vy = QPushButton("Verify")
        vy.clicked.connect(self._verify)
        yl.addWidget(vy)
        lay.addWidget(ybox)
        self.val_out = QPlainTextEdit()
        self.val_out.setReadOnly(True)
        self.val_out.setMinimumHeight(140)
        lay.addWidget(self.val_out)
        return w

    def _prepare(self):
        def go():
            self._auto("prepare")
            res = prepare.prepare(
                self.cfg,
                self.cfg.gap,
                self.prep_points.isChecked(),
                self.prep_ends.isChecked(),
            )
            self._net = None
            self.val_out.setPlainText(
                "Prepare network\n  points moved: %(points_moved)d\n  line"
                " ends moved: %(line_ends_moved)d\n  vertices inserted:"
                " %(vertices_inserted)d\n\nChanges are in edit mode: review"
                " them and save the layers." % res
            )
            self.iface.mapCanvas().refresh()

        self._run("Prepare network", go)

    def _validate(self, _checked=False, full=False, quiet=False):
        def go():
            net = self.network()
            issues = net.validate(
                self.cfg.rules if self.val_rules.isChecked() else None,
                self.val_dangles.isChecked(),
                self.val_islands.isChecked(),
                names=self.cfg.group_names,
            )
            if self.val_required.isChecked() or full:
                issues += reports.required_issues(self.cfg)
            if self.val_extra.isChecked() or full:
                issues += tools.extra_checks(
                    self.cfg, check_crossings=self.cfg.flow != "undirected"
                )
            if (
                self.val_subs.isChecked() or full
            ) and self.cfg.flow != "undirected":
                issues += net.subnetwork_issues()
            areas = None
            self._resolve_dirty()
            if self.val_dirty.isChecked() and not full:
                areas = Q.read_dirty_areas(self.cfg) + self._dirty
                if not areas:
                    raise ValueError(
                        "There are no dirty areas: nothing was edited since"
                        " the last validation."
                    )
            n_exc = sum(
                1
                for it in issues
                if (it["code"], it["key"][0], it["key"][1])
                in self.cfg.exceptions
            )
            Q.write_errors(self.cfg, issues, areas)
            Q.write_dirty_areas(self.cfg, [], replace=True)
            self._dirty = []
            self._update_dirty_label()
            from datetime import datetime as _dt

            S.set_setting(
                self.cfg.path,
                "last_validation",
                _dt.now().strftime("%Y-%m-%d %H:%M"),
            )
            self.cfg.settings["last_validation"] = _dt.now().strftime(
                "%Y-%m-%d %H:%M"
            )
            summary = {}
            for it in issues:
                k = (
                    it["code"],
                    it["severity"],
                    it["message"].split(" (")[0].split(":")[0],
                )
                summary[k] = summary.get(k, 0) + 1
            lines = [
                "Validation - %s" % datetime.now().strftime("%H:%M:%S"),
                "Nodes: %d, edges: %d" % (len(net.node_xy), len(net.edges)),
                "",
            ]
            for (code, sev, msg), n in sorted(summary.items()):
                lines.append("%s  %-7s %5d  %s" % (code, sev, n, msg))
            if not issues:
                lines.append("No problems found.")
            if n_exc:
                lines.append(
                    "(%d error(s) are exceptions and were not written)" % n_exc
                )
            if areas:
                lines.append(
                    "Validated inside %d dirty area(s) only." % len(areas)
                )
            lines.append("")
            lines.append(
                "Errors were written to the layers 'Errors (points)' and"
                " 'Errors (lines)'."
            )
            self.val_out.setPlainText("\n".join(lines))
            if Q.project_layer(self.cfg.path, "un_errors_point") is None:
                Q.load_network(self.cfg, self.iface)
            self.iface.mapCanvas().refresh()
            if not quiet:
                self._msg(
                    "%d problem(s) found." % len(issues),
                    "Warning" if issues else "Success",
                )

        self._run("Validate", go)

    def _update_dirty_label(self):
        if self.cfg is None:
            return
        try:
            n = len(Q.read_dirty_areas(self.cfg)) + len(self._dirty)
        except Exception:
            n = len(self._dirty)
        self.dirty_label.setText("Dirty areas waiting for validation: %d" % n)

    def _clear_dirty(self):
        self._run(
            "Dirty areas",
            lambda: (
                Q.write_dirty_areas(self.cfg, [], replace=True),
                setattr(self, "_dirty", []),
                self._update_dirty_label(),
            ),
        )

    def _mark_exceptions(self):
        def go():
            rows = list(self.cfg.exceptions)
            n = 0
            for name in ("un_errors_point", "un_errors_line"):
                lyr = Q.project_layer(self.cfg.path, name)
                if lyr is None:
                    continue
                for f in lyr.getSelectedFeatures():
                    rows.append(
                        (f["code"], f["class_name"], int(f["feature_fid"]))
                    )
                    n += 1
            if not n:
                raise ValueError(
                    "Select error features in the Errors layers first."
                )
            S.save_table(
                self.cfg.path,
                "un_error_exceptions",
                [
                    {"code": c, "class_name": k, "feature_fid": i}
                    for c, k, i in sorted(set(rows), key=str)
                ],
            )
            self._reload_cfg()
            self._msg(
                "%d error(s) marked as exceptions. Validate again to refresh"
                " the error layers." % n
            )

        self._run("Exceptions", go)

    def _clear_exceptions(self):
        self._run(
            "Exceptions",
            lambda: (
                S.save_table(self.cfg.path, "un_error_exceptions", []),
                self._reload_cfg(),
                self._msg("Exceptions cleared."),
            ),
        )

    def _verify(self):
        self._run(
            "Verify",
            lambda: self.val_out.setPlainText(
                "Verify network topology\n\n" + "\n".join(Q.verify(self.cfg))
            ),
        )

    def _learn(self):
        def go():
            net = self.network()
            found = net.learn_rules()
            new = found - self.cfg.rules
            if not new:
                self.val_out.setPlainText(
                    "All connections in the data are already allowed."
                )
                return
            txt = "\n".join(
                "  %s: %s  <->  %s: %s"
                % (a[0], self.cfg.label(*a), b[0], self.cfg.label(*b))
                for a, b in sorted(new, key=str)[:40]
            )
            if (
                QMessageBox.question(
                    self,
                    "Learn rules",
                    "Add %d new rule(s)?\n\n%s" % (len(new), txt),
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            S.save_rules(self.cfg.path, self.cfg.rules | new)
            self._reload_cfg()
            self._update_rules_label()

        self._run("Learn rules", go)

    def _reset_rules(self):
        def go():
            names = T.class_names(
                self.cfg.tpl, "StructureJunction" in self.cfg.classes
            )
            rename = {c: self.cfg.classes.get(r, c) for r, c in names.items()}
            rules = {
                tuple(
                    sorted(
                        ((rename[a[0]], a[1]), (rename[b[0]], b[1])), key=str
                    )
                )
                for a, b in T.default_rules(
                    self.cfg.tpl, "StructureJunction" in self.cfg.classes
                )
            }
            S.save_rules(self.cfg.path, rules)
            self._reload_cfg()
            self._update_rules_label()

        if (
            QMessageBox.question(
                self, "Rules", "Replace the rules with the template defaults?"
            )
            == QMessageBox.StandardButton.Yes
        ):
            self._run("Rules", go)

    def _update_rules_label(self):
        self._fill_rules()

    # ================================================================ Trace
    def _tab_trace(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.trace_kind = QComboBox()
        for k, label in TRACES:
            self.trace_kind.addItem(label, k)
        form.addRow("Trace", self.trace_kind)
        lay.addLayout(form)
        row = QHBoxLayout()
        for kind, label in (
            ("start", "Add start"),
            ("barrier", "Add barrier"),
            ("end", "Add end (path)"),
        ):
            b = QPushButton(label)
            b.clicked.connect(lambda _c=False, k=kind: self._pick(k))
            row.addWidget(b)
        lay.addLayout(row)
        self.pick_list = QListWidget()
        self.pick_list.setMaximumHeight(110)
        lay.addWidget(self.pick_list)
        clear = QPushButton("Clear points")
        clear.clicked.connect(self._clear_picks)
        lay.addWidget(clear)
        fbox = QGroupBox("Trace settings")
        ff = QFormLayout(fbox)
        self.use_status = QCheckBox(
            "Closed devices stop the trace (operating status)"
        )
        self.use_status.setChecked(True)
        ff.addRow(self.use_status)
        self.only_service = QCheckBox(
            "Use only features that are In service (skip proposed,"
            " abandoned...)"
        )
        ff.addRow(self.only_service)
        self.min_size = _spin(0, 0, 100000, 1)
        self.min_size.setSpecialValueText("off")
        ff.addRow("Stop at lines smaller than", self.min_size)
        self.out_filter = QComboBox()
        for key, label in (
            ("all", "All traced features"),
            ("customer", "Customers only"),
            ("isolating", "Isolating devices only"),
            ("lines", "Lines only"),
            ("points", "Points only"),
        ):
            self.out_filter.addItem(label, key)
        ff.addRow("Show in the result", self.out_filter)
        crow = QHBoxLayout()
        self.trace_cfg = QComboBox()
        self.trace_cfg.setEditable(True)
        self.trace_cfg.setPlaceholderText("saved trace name")
        self.trace_cfg.activated.connect(self._apply_trace_config)
        sv = QPushButton("Save")
        sv.clicked.connect(self._save_trace_config)
        dl = QPushButton("Delete")
        dl.clicked.connect(self._delete_trace_config)
        crow.addWidget(self.trace_cfg, 1)
        crow.addWidget(sv)
        crow.addWidget(dl)
        ff.addRow("Saved traces", crow)
        lay.addWidget(fbox)
        self._conditions_box(lay)
        srow = QHBoxLayout()
        s1 = QPushButton("Selected features as starts")
        s1.clicked.connect(lambda: self._picks_from_selection("start"))
        s2 = QPushButton("Selected features as barriers")
        s2.clicked.connect(lambda: self._picks_from_selection("barrier"))
        srow.addWidget(s1)
        srow.addWidget(s2)
        lay.addLayout(srow)
        irow = QHBoxLayout()
        e1 = QPushButton("Export trace configurations...")
        e1.clicked.connect(lambda: self._trace_configs_io(True))
        i1 = QPushButton("Import trace configurations...")
        i1.clicked.connect(lambda: self._trace_configs_io(False))
        irow.addWidget(e1)
        irow.addWidget(i1)
        lay.addLayout(irow)
        self.res_layers = QCheckBox("Also add the result as new layers")
        lay.addWidget(self.res_layers)
        run = QPushButton("Run trace")
        run.clicked.connect(self._trace)
        lay.addWidget(run)
        exp = QPushButton("Export the result to Excel...")
        exp.setToolTip(
            "All attributes of the traced features (e.g. the customers of an"
            " outage), one sheet per class."
        )
        exp.clicked.connect(self._export_trace)
        lay.addWidget(exp)
        self.trace_out = QPlainTextEdit()
        self.trace_out.setReadOnly(True)
        self.trace_out.setMinimumHeight(150)
        lay.addWidget(self.trace_out)
        lay.addStretch(1)
        return w

    def _layers_by_class(self):
        if self.cfg is None:
            return {}
        out = {}
        for role, cls in self.cfg.classes.items():
            lyr = Q.project_layer(self.cfg.path, cls)
            if lyr is not None:
                out[cls] = lyr
        return out

    def _pick(self, kind):
        if self.cfg is None:
            return
        if not self._layers_by_class():
            QMessageBox.information(
                self, "Trace", "Add the network layers to the map first."
            )
            return
        canvas = self.iface.mapCanvas()
        if self.tool is None:
            self.tool = PickTool(canvas, self._layers_by_class)
            self.tool.picked.connect(self._picked)
        self.tool.kind = kind
        canvas.setMapTool(self.tool)
        self._msg(
            "Click on a network feature to add a %s point." % kind, duration=3
        )

    def _picked(self, p):
        self.picks.append(p)
        self.pick_list.addItem(
            "%s: %s #%s" % (p["kind"], p["key"][0], p["key"][1])
        )

    def _clear_picks(self):
        canvas = self.iface.mapCanvas()
        for p in self.picks:
            canvas.scene().removeItem(p["marker"])
        self.picks = []
        self.pick_list.clear()

    def _locate(self, net, kind):
        nodes, edges = set(), set()
        for p in self.picks:
            if p["kind"] != kind:
                continue
            n, e = net.nodes_of_key(p["key"])
            if e:
                one = net.edge_near(p["key"], p["x"], p["y"])
                e = {one} if one is not None else e
            nodes |= n
            edges |= e
        return nodes, edges

    def _trace(self):
        def go():
            self._require_topology()
            kind = self.trace_kind.currentData()
            net = self.network()
            sn, se = self._locate(net, "start")
            bn, be = self._locate(net, "barrier")
            tn, te = self._locate(net, "end")
            res = net.trace(
                kind,
                sn,
                se,
                bn,
                be,
                self.use_status.isChecked(),
                tn,
                te,
                exclude_lifecycle=(
                    {1, 2, 4, 5, 6} if self.only_service.isChecked() else ()
                ),
                min_size=self.min_size.value() or None,
                output_groups=self._output_groups(),
                conditions=self._conditions(),
            )
            Q.select_result(self.cfg, res, zoom_canvas=self.iface.mapCanvas())
            lines = [res.message, ""]
            if kind == "isolation" and res.extra.get("to_close"):
                lines.append("Close these isolating devices:")
                for cls, fid in res.extra["to_close"]:
                    lines.append("  %s #%s" % (cls, fid))
                lines.append("")
                lines.append(
                    "Customers without supply: %d"
                    % len(res.extra.get("customers", []))
                )
            if kind == "subnetwork" and res.extra.get("names"):
                lines.append("Subnetworks: " + ", ".join(res.extra["names"]))
            lines.append("Result features are selected on the map.")
            self.trace_out.setPlainText("\n".join(lines))
            if self.res_layers.isChecked() and res.count():
                Q.result_layers(
                    self.cfg, res, self.trace_kind.currentText().split(" (")[0]
                )
            self.last_result = res

        self._run("Trace", go)

    def _output_groups(self):
        key = self.out_filter.currentData()
        if key == "all":
            return None
        out = set()
        for (cls, ag), cats in self.cfg.cats.items():
            role = self.cfg.role_of.get(cls, "")
            kind = "lines" if role in ("Line", "StructureLine") else "points"
            if key in ("customer", "isolating") and key in cats:
                out.add((cls, ag))
            elif key == kind:
                out.add((cls, ag))
        return out or {("", -1)}

    def _trace_settings(self):
        return {
            "kind": self.trace_kind.currentData(),
            "use_status": self.use_status.isChecked(),
            "only_service": self.only_service.isChecked(),
            "min_size": self.min_size.value(),
            "output": self.out_filter.currentData(),
            "conditions": self._conditions(),
        }

    def _fill_trace_configs(self):
        self.trace_cfg.clear()
        for name in sorted(self.cfg.trace_configs()) if self.cfg else []:
            self.trace_cfg.addItem(name)
        self.trace_cfg.setCurrentIndex(-1)

    def _apply_trace_config(self, _i=0):
        conf = self.cfg.trace_configs().get(self.trace_cfg.currentText())
        if not conf:
            return
        self.trace_kind.setCurrentIndex(
            max(0, self.trace_kind.findData(conf.get("kind")))
        )
        self.use_status.setChecked(conf.get("use_status", True))
        self.only_service.setChecked(conf.get("only_service", False))
        self.min_size.setValue(conf.get("min_size", 0))
        self.out_filter.setCurrentIndex(
            max(0, self.out_filter.findData(conf.get("output", "all")))
        )
        self.cond_table.setRowCount(0)
        for cond in conf.get("conditions", []):
            self._table_add(self.cond_table, [str(c) for c in cond])

    def _save_trace_config(self):
        name = self.trace_cfg.currentText().strip()
        if not name:
            QMessageBox.information(
                self, "Saved traces", "Type a name for this trace first."
            )
            return

        def go():
            S.set_setting(
                self.cfg.path,
                "trace_config:" + name,
                json.dumps(self._trace_settings()),
            )
            self._reload_cfg()
            self._fill_trace_configs()
            self.trace_cfg.setCurrentText(name)

        self._run("Saved traces", go)

    def _delete_trace_config(self):
        name = self.trace_cfg.currentText().strip()
        if name:
            self._run(
                "Saved traces",
                lambda: (
                    S.set_setting(self.cfg.path, "trace_config:" + name, None),
                    self._reload_cfg(),
                    self._fill_trace_configs(),
                ),
            )

    def _export_trace(self):
        if self.last_result is None or not self.last_result.count():
            QMessageBox.information(self, "Export", "Run a trace first.")
            return
        path, _f = QFileDialog.getSaveFileName(
            self, "Export trace result", "trace_result.xlsx", "Excel (*.xlsx)"
        )
        if not path:
            return

        def go():
            res = self.last_result
            sheets = reports.feature_rows(self.cfg, res.lines | res.points)
            if res.extra.get("to_close"):
                sheets = dict(sheets)
                sheets["Devices to close"] = [
                    {"class": c, "fid": f} for c, f in res.extra["to_close"]
                ]
            out = reports.write_xlsx(path, sheets or {"Result": []})
            self._msg("Exported: %s" % out, "Success")

        self._run("Export", go)

    # ================================================================
    # Subnetworks
    def _tab_subnetworks(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Every source (pump, tank, substation breaker, OLT,"
                " outfall...) feeds a subnetwork. Update writes the subnetwork"
                " name into the 'subnetwork' field of every feature."
            )
        )
        up = QPushButton("Update subnetworks")
        up.clicked.connect(self._update_subnetworks)
        lay.addWidget(up)
        self.sub_list = QTableWidget(0, 4)
        self.sub_list.setHorizontalHeaderLabels(
            ["Subnetwork", "Tier", "Features", "Customers"]
        )
        self.sub_list.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.sub_list.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.sub_list.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.sub_list.cellDoubleClicked.connect(self._select_subnetwork)
        lay.addWidget(self.sub_list)
        lay.addWidget(
            QLabel("Double-click a subnetwork to select its features.")
        )
        brow = QHBoxLayout()
        ex = QPushButton("Export selected subnetwork...")
        ex.setToolTip("Its features to a new GeoPackage plus an Excel list.")
        ex.clicked.connect(self._export_subnetwork)
        ic = QPushButton("Update Is connected")
        ic.setToolTip(
            "Writes 1 / 0 into the isconnected field: connected to a"
            " controller or not."
        )
        ic.clicked.connect(self._update_isconnected)
        brow.addWidget(ex)
        brow.addWidget(ic)
        lay.addLayout(brow)
        self._controllers_box(lay)
        lay.addStretch(1)
        return w

    def _update_subnetworks(self):
        def go():
            self._require_topology()
            net = self.network()
            subs = net.subnetworks(name_of=lambda p: p.get("assetid"))
            changed = Q.write_subnetwork_field(self.cfg, subs)
            now = datetime.now().isoformat(timespec="seconds")
            rows = [
                {
                    "name": n,
                    "tier": i["tier"],
                    "controller_class": i["controller"][0],
                    "controller_fid": i["controller"][1],
                    "line_count": len(i["lines"]),
                    "point_count": len(i["points"]),
                    "customers": i["customers"],
                    "updated": now,
                }
                for n, i in sorted(subs["subnetworks"].items())
            ]
            S.save_subnetworks(self.cfg.path, rows)
            multi = sum(1 for v in subs["by_key"].values() if len(v) > 1)
            self._reload_cfg()
            self._fill_subnetworks()
            self._fill_diagram_subs()
            self._msg(
                "%d subnetwork(s), %d feature(s) updated%s."
                % (
                    len(rows),
                    changed,
                    (
                        ", %d fed by more than one source" % multi
                        if multi
                        else ""
                    ),
                )
            )

        self._run("Subnetworks", go)

    def _fill_subnetworks(self):
        self.sub_list.setRowCount(0)
        for r in (self.cfg.subnetworks if self.cfg else []):
            i = self.sub_list.rowCount()
            self.sub_list.insertRow(i)
            for c, v in enumerate(
                (
                    r.get("name"),
                    r.get("tier") or "",
                    (r.get("line_count") or 0) + (r.get("point_count") or 0),
                    r.get("customers") or 0,
                )
            ):
                self.sub_list.setItem(i, c, QTableWidgetItem(str(v)))

    def _select_subnetwork(self, row, _col):
        name = self.sub_list.item(row, 0).text()

        def go():
            net = self.network()
            subs = net.subnetworks(name_of=lambda p: p.get("assetid"))
            info = subs["subnetworks"].get(name)
            if info is None:
                raise ValueError(
                    "Subnetwork not found, update the subnetworks."
                )
            from .engine import TraceResult

            res = TraceResult()
            res.lines, res.points = info["lines"], info["points"]
            Q.select_result(self.cfg, res, zoom_canvas=self.iface.mapCanvas())

        self._run("Subnetworks", go)

    # ================================================================ Services
    def _tab_services(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Draw the service connection from the main to every customer"
                " automatically."
            )
        )
        self.svc_hint = QLabel()
        self.svc_hint.setWordWrap(True)
        lay.addWidget(self.svc_hint)
        form = QFormLayout()
        self.svc_cust = QgsMapLayerComboBox()
        self.svc_cust.setFilters(
            _filter("PointLayer") | _filter("PolygonLayer")
        )
        form.addRow("Customers (points or buildings)", self.svc_cust)
        self.svc_sel = QCheckBox("Selected customers only")
        form.addRow("", self.svc_sel)
        self.svc_id = QgsFieldComboBox()
        self.svc_id.setAllowEmptyFieldName(True)
        self.svc_cust.layerChanged.connect(self.svc_id.setLayer)
        self.svc_id.setLayer(self.svc_cust.currentLayer())
        form.addRow("Customer ID field", self.svc_id)
        self.svc_mains = QListWidget()
        self.svc_mains.setMaximumHeight(110)
        form.addRow("Connect to lines", self.svc_mains)
        self.svc_line, self.svc_line_t = QComboBox(), QComboBox()
        self.svc_tap, self.svc_tap_t = QComboBox(), QComboBox()
        self.svc_end, self.svc_end_t = QComboBox(), QComboBox()
        for label, a, b in (
            ("Service line", self.svc_line, self.svc_line_t),
            ("Tap junction", self.svc_tap, self.svc_tap_t),
            ("End device", self.svc_end, self.svc_end_t),
        ):
            row = QHBoxLayout()
            row.addWidget(a, 1)
            row.addWidget(b, 1)
            form.addRow(label, row)
        self.svc_method = QComboBox()
        self.svc_method.addItem(
            "Perpendicular to the nearest main", "perpendicular"
        )
        self.svc_method.addItem(
            "Nearest existing vertex of the main", "vertex"
        )
        form.addRow("Connection point", self.svc_method)
        self.svc_endat = QComboBox()
        self.svc_endat.addItem("Building edge facing the main", "edge")
        self.svc_endat.addItem("Building centre", "centroid")
        form.addRow("Buildings: end at", self.svc_endat)
        self.svc_max = _spin(50.0, 0.1, 10000, 2)
        self.svc_min = _spin(0.5, 0, 100, 2)
        self.svc_clear = _spin(1.0, 0, 100, 2)
        self.svc_reuse = _spin(0.5, 0, 100, 2)
        form.addRow("Maximum length", self.svc_max)
        form.addRow("Skip if closer than", self.svc_min)
        form.addRow("Keep taps away from devices", self.svc_clear)
        form.addRow("Reuse a tap closer than", self.svc_reuse)
        lay.addLayout(form)
        self.svc_tapbox = QCheckBox("Create the tap junction")
        self.svc_vertex = QCheckBox("Insert a vertex in the main at the tap")
        self.svc_endbox = QCheckBox("Create the end device at the customer")
        self.svc_cross = QCheckBox("Avoid crossing other mains")
        self.svc_skip = QCheckBox("Skip customers that already have a service")
        for c in (
            self.svc_tapbox,
            self.svc_vertex,
            self.svc_endbox,
            self.svc_cross,
            self.svc_skip,
        ):
            c.setChecked(True)
            lay.addWidget(c)
        go = QPushButton("Create service connections")
        go.clicked.connect(self._services)
        lay.addWidget(go)
        self.svc_out = QPlainTextEdit()
        self.svc_out.setReadOnly(True)
        self.svc_out.setMaximumHeight(140)
        lay.addWidget(self.svc_out)
        return w

    def _services(self):
        def go():
            mains = [
                self.svc_mains.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.svc_mains.count())
                if self.svc_mains.item(i).checkState() == Qt.CheckState.Checked
            ]
            opt = services.ServiceOptions(
                customer_layer=self.svc_cust.currentLayer(),
                selected_only=self.svc_sel.isChecked(),
                id_field=self.svc_id.currentField() or None,
                main_groups=mains,
                max_distance=self.svc_max.value(),
                min_length=self.svc_min.value(),
                clearance=self.svc_clear.value(),
                reuse_tap=self.svc_reuse.value(),
                method=self.svc_method.currentData(),
                end_at=self.svc_endat.currentData(),
                create_tap=self.svc_tapbox.isChecked(),
                insert_vertex=self.svc_vertex.isChecked(),
                create_end=self.svc_endbox.isChecked(),
                avoid_crossing=self.svc_cross.isChecked(),
                skip_existing=self.svc_skip.isChecked(),
                service_group=(
                    self.svc_line.currentData(),
                    self.svc_line_t.currentData() or 1,
                ),
                tap_group=(
                    (
                        self.svc_tap.currentData(),
                        self.svc_tap_t.currentData() or 1,
                    )
                    if self.svc_tap.currentData() is not None
                    else None
                ),
                end_group=(
                    (
                        self.svc_end.currentData(),
                        self.svc_end_t.currentData() or 1,
                    )
                    if self.svc_end.currentData() is not None
                    else None
                ),
            )
            self._auto("services")
            res = services.run(self.cfg, opt)
            self._net = None
            self.svc_out.setPlainText(
                "Service connections created: %(created)d\nTaps reused:"
                " %(reused_taps)d\nMains updated with a vertex:"
                " %(mains_updated)d\n\nSkipped:\n  already connected:"
                " %(existing)d\n  farther than the maximum: %(too_far)d\n "
                " would cross another main: %(crossing)d\n  touching the main:"
                " %(touching)d\n  no geometry: %(no_geometry)d\n\nNew features"
                " are in edit mode: review and save the layers." % res
            )
            self.iface.mapCanvas().refresh()

        self._run("Service connections", go)

    # ================================================================ Model
    def _tab_model(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "The data model of the network: asset groups and asset types"
                " of every class, what each group does (categories), its tier"
                " and, for devices, its terminals."
            )
        )
        row = QHBoxLayout()
        row.addWidget(QLabel("Class"))
        self.m_class = QComboBox()
        self.m_class.currentIndexChanged.connect(self._model_class_changed)
        row.addWidget(self.m_class, 1)
        lay.addLayout(row)
        gbox = QGroupBox("Asset groups")
        gl = QVBoxLayout(gbox)
        self.m_groups = QTableWidget(0, 4)
        self.m_groups.setHorizontalHeaderLabels(
            ["Code", "Name", "Categories", "Tier"]
        )
        self.m_groups.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.m_groups.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.m_groups.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.m_groups.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.m_groups.setMinimumHeight(170)
        self.m_groups.currentCellChanged.connect(self._model_group_changed)
        gl.addWidget(self.m_groups)
        cat_help = QLabel(
            "Categories (space separated): source, isolating, customer, tap,"
            " service, main, structure, container, endvertex (lines that"
            " connect only at their end points)"
        )
        cat_help.setWordWrap(True)
        gl.addWidget(cat_help)
        gr = QHBoxLayout()
        for label, fn in (
            ("Add group", self._model_add_group),
            ("Remove group", self._model_del_group),
        ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            gr.addWidget(b)
        gl.addLayout(gr)
        lay.addWidget(gbox)
        sub = QHBoxLayout()
        tbox = QGroupBox("Asset types of the selected group")
        tl = QVBoxLayout(tbox)
        self.m_types = QTableWidget(0, 2)
        self.m_types.setHorizontalHeaderLabels(["Code", "Name"])
        self.m_types.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.m_types.setMinimumHeight(140)
        tl.addWidget(self.m_types)
        tr = QHBoxLayout()
        for label, fn in (
            ("Add type", self._model_add_type),
            ("Remove type", self._model_del_type),
        ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            tr.addWidget(b)
        tl.addLayout(tr)
        sub.addWidget(tbox)
        kbox = QGroupBox("Terminals (devices between tiers)")
        kl = QVBoxLayout(kbox)
        self.m_terms = QTableWidget(0, 2)
        self.m_terms.setHorizontalHeaderLabels(["Terminal", "Tier"])
        self.m_terms.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.m_terms.setMinimumHeight(140)
        kl.addWidget(self.m_terms)
        kr = QHBoxLayout()
        for label, fn in (
            ("Add terminal", self._model_add_term),
            ("Remove terminal", self._model_del_term),
        ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            kr.addWidget(b)
        kl.addLayout(kr)
        sub.addWidget(kbox)
        lay.addLayout(sub)
        save = QPushButton("Save model")
        save.setStyleSheet("font-weight: bold;")
        save.clicked.connect(self._model_save)
        lay.addWidget(save)

        rbox = QGroupBox("Connectivity rules (which asset groups may connect)")
        rl = QVBoxLayout(rbox)
        self.m_rules = QTableWidget(0, 2)
        self.m_rules.setHorizontalHeaderLabels(["From", "To"])
        self.m_rules.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.m_rules.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.m_rules.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.m_rules.setMinimumHeight(170)
        rl.addWidget(self.m_rules)
        ar = QHBoxLayout()
        self.r_from, self.r_to = QComboBox(), QComboBox()
        add = QPushButton("Add rule")
        add.clicked.connect(self._rule_add)
        ar.addWidget(self.r_from, 1)
        ar.addWidget(self.r_to, 1)
        ar.addWidget(add)
        rl.addLayout(ar)
        rr = QHBoxLayout()
        for label, fn in (
            ("Remove selected", self._rule_del),
            ("Learn from data", self._learn),
            ("Reset to template", self._reset_rules),
        ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            rr.addWidget(b)
        rl.addLayout(rr)
        lay.addWidget(rbox)

        self._model_extra(lay)
        qbox = QGroupBox("Required fields (checked by Validate)")
        ql = QHBoxLayout(qbox)
        self.m_required = QLineEdit()
        self.m_required.setToolTip(
            "Comma separated field names, e.g."
            " assetgroup,assettype,assetid,installdate"
        )
        sreq = QPushButton("Save")
        sreq.clicked.connect(
            lambda: self._run(
                "Required fields",
                lambda: (
                    S.set_setting(
                        self.cfg.path,
                        "required_fields",
                        self.m_required.text().replace(" ", ""),
                    ),
                    self._reload_cfg(),
                    self._msg("Required fields saved."),
                ),
            )
        )
        ql.addWidget(self.m_required, 1)
        ql.addWidget(sreq)
        lay.addWidget(qbox)
        return w

    def _model_load(self):
        cfg = self.cfg
        self.m_model = {}
        for r in cfg.assets:
            c = self.m_model.setdefault(r["class_name"], {})
            g = c.setdefault(
                r["ag_code"],
                {
                    "name": r["ag_name"],
                    "cats": r.get("categories") or "",
                    "tier": r.get("tier") or "",
                    "types": {},
                    "terms": [],
                    "role": r["role"],
                },
            )
            g["types"][r["at_code"]] = r["at_name"]
        for (cls, ag), terms in cfg.terminals.items():
            if cls in self.m_model and ag in self.m_model[cls]:
                self.m_model[cls][ag]["terms"] = list(terms)
        self._m_cur = None
        self.m_class.blockSignals(True)
        self.m_class.clear()
        for role, cls in cfg.classes.items():
            self.m_class.addItem("%s (%s)" % (cls, role), cls)
        self.m_class.blockSignals(False)
        self._model_class_changed()
        self.m_required.setText(",".join(cfg.required))
        self._fill_rules()
        self._fill_model_extra()

    def _model_commit(self):
        """Copy the tables of the current class / group back into the in-memory
        model."""
        cls = getattr(self, "_m_cls", None)
        if not cls or cls not in self.m_model:
            return
        groups = {}
        for r in range(self.m_groups.rowCount()):
            try:
                code = int(self.m_groups.item(r, 0).text())
            except (ValueError, AttributeError):
                continue
            old = self.m_groups.item(r, 0).data(Qt.ItemDataRole.UserRole)
            g = dict(
                self.m_model[cls].get(
                    old,
                    {
                        "types": {1: "Standard"},
                        "terms": [],
                        "role": self._m_role,
                    },
                )
            )
            g["name"] = (
                self.m_groups.item(r, 1).text().strip() or "Group %d" % code
            )
            g["cats"] = " ".join(
                self.m_groups.item(r, 2).text().replace(",", " ").split()
            )
            g["tier"] = self.m_groups.item(r, 3).text().strip()
            groups[code] = g
            self.m_groups.item(r, 0).setData(Qt.ItemDataRole.UserRole, code)
        if self._m_cur is not None and self._m_cur in groups:
            types = {}
            for r in range(self.m_types.rowCount()):
                try:
                    types[int(self.m_types.item(r, 0).text())] = (
                        self.m_types.item(r, 1).text().strip() or "Type"
                    )
                except (ValueError, AttributeError):
                    pass
            groups[self._m_cur]["types"] = types or {1: "Standard"}
            groups[self._m_cur]["terms"] = [
                (
                    self.m_terms.item(r, 0).text().strip(),
                    self.m_terms.item(r, 1).text().strip(),
                )
                for r in range(self.m_terms.rowCount())
                if self.m_terms.item(r, 0)
                and self.m_terms.item(r, 0).text().strip()
            ]
        self.m_model[cls] = groups

    def _model_class_changed(self, _i=0):
        self._model_commit()
        cls = self.m_class.currentData()
        self._m_cls = cls
        self._m_role = self.cfg.role_of.get(cls) if self.cfg else None
        self._m_cur = None
        self.m_groups.blockSignals(True)
        self.m_groups.setRowCount(0)
        for code, g in sorted((self.m_model or {}).get(cls, {}).items()):
            r = self.m_groups.rowCount()
            self.m_groups.insertRow(r)
            for c, v in enumerate(
                (str(code), g["name"], g["cats"], g["tier"])
            ):
                self.m_groups.setItem(r, c, QTableWidgetItem(v))
            self.m_groups.item(r, 0).setData(Qt.ItemDataRole.UserRole, code)
        self.m_groups.blockSignals(False)
        self.m_types.setRowCount(0)
        self.m_terms.setRowCount(0)
        if self.m_groups.rowCount():
            self.m_groups.setCurrentCell(0, 1)

    def _model_group_changed(self, row, _c, prev_row, _pc):
        if row == prev_row and self._m_cur is not None:
            return
        self._model_commit()
        item = self.m_groups.item(row, 0) if row >= 0 else None
        self._m_cur = item.data(Qt.ItemDataRole.UserRole) if item else None
        g = self.m_model.get(self._m_cls, {}).get(self._m_cur)
        self.m_types.setRowCount(0)
        self.m_terms.setRowCount(0)
        if not g:
            return
        for code, name in sorted(g["types"].items()):
            r = self.m_types.rowCount()
            self.m_types.insertRow(r)
            self.m_types.setItem(r, 0, QTableWidgetItem(str(code)))
            self.m_types.setItem(r, 1, QTableWidgetItem(name))
        for name, tier in g["terms"]:
            r = self.m_terms.rowCount()
            self.m_terms.insertRow(r)
            self.m_terms.setItem(r, 0, QTableWidgetItem(name))
            self.m_terms.setItem(r, 1, QTableWidgetItem(tier or ""))

    def _model_add_group(self):
        codes = [
            int(self.m_groups.item(r, 0).text())
            for r in range(self.m_groups.rowCount())
            if self.m_groups.item(r, 0)
            and self.m_groups.item(r, 0).text().isdigit()
        ]
        code = max(codes or [0]) + 1
        r = self.m_groups.rowCount()
        self.m_groups.insertRow(r)
        for c, v in enumerate((str(code), "New group", "", "")):
            self.m_groups.setItem(r, c, QTableWidgetItem(v))
        self.m_groups.item(r, 0).setData(Qt.ItemDataRole.UserRole, code)
        self.m_model.setdefault(self._m_cls, {})[code] = {
            "name": "New group",
            "cats": "",
            "tier": "",
            "types": {1: "Standard"},
            "terms": [],
            "role": self._m_role,
        }
        self.m_groups.setCurrentCell(r, 1)

    def _model_del_group(self):
        r = self.m_groups.currentRow()
        if r < 0:
            return
        if (
            QMessageBox.question(
                self,
                "Model",
                "Remove the asset group '%s'? Features that use it will show "
                "an 'Asset group' error until you change them."
                % self.m_groups.item(r, 1).text(),
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        code = self.m_groups.item(r, 0).data(Qt.ItemDataRole.UserRole)
        self.m_model.get(self._m_cls, {}).pop(code, None)
        self._m_cur = None
        self.m_groups.removeRow(r)

    def _model_add_type(self):
        codes = [
            int(self.m_types.item(r, 0).text())
            for r in range(self.m_types.rowCount())
            if self.m_types.item(r, 0)
            and self.m_types.item(r, 0).text().isdigit()
        ]
        r = self.m_types.rowCount()
        self.m_types.insertRow(r)
        self.m_types.setItem(
            r, 0, QTableWidgetItem(str(max(codes or [0]) + 1))
        )
        self.m_types.setItem(r, 1, QTableWidgetItem("New type"))

    def _model_del_type(self):
        if self.m_types.currentRow() >= 0:
            self.m_types.removeRow(self.m_types.currentRow())

    def _model_add_term(self):
        r = self.m_terms.rowCount()
        self.m_terms.insertRow(r)
        self.m_terms.setItem(r, 0, QTableWidgetItem("Terminal %d" % (r + 1)))
        self.m_terms.setItem(
            r,
            1,
            QTableWidgetItem(
                self.cfg.tier_names()[0] if self.cfg.tier_names() else ""
            ),
        )

    def _model_del_term(self):
        if self.m_terms.currentRow() >= 0:
            self.m_terms.removeRow(self.m_terms.currentRow())

    def _model_save(self):
        def go():
            self._model_commit()
            role_of = self.cfg.role_of
            assets, terms = [], []
            for cls, groups in self.m_model.items():
                for ag, g in sorted(groups.items()):
                    for at, tname in sorted(g["types"].items()):
                        assets.append(
                            {
                                "class_name": cls,
                                "role": role_of.get(cls, g.get("role")),
                                "ag_code": ag,
                                "ag_name": g["name"],
                                "at_code": at,
                                "at_name": tname,
                                "categories": g["cats"],
                                "tier": g["tier"] or None,
                            }
                        )
                    for name, tier in g["terms"]:
                        terms.append(
                            {
                                "class_name": cls,
                                "ag_code": ag,
                                "terminal_name": name,
                                "tier": tier or None,
                            }
                        )
            self._auto("model")
            S.save_model(
                self.cfg.path, self.cfg.classes, assets, terms, self.cfg.flow
            )
            self._reload_cfg()
            cur = self.m_class.currentIndex()
            self._refresh_forms()
            self.m_class.setCurrentIndex(cur)
            Q.refresh_forms(self.cfg)
            self._msg(
                "Model saved: forms, styles and lists were updated.", "Success"
            )

        self._run("Model", go)

    def _group_items(self):
        out = []
        for role in ("Line", "Device", "Junction"):
            cls = self.cfg.classes.get(role)
            for ag, name in self.cfg.groups(cls) if cls else []:
                out.append(((cls, ag), "%s: %s" % (cls, name)))
        return out

    def _fill_rules(self):
        if self.cfg is None:
            return
        self.m_rules.setRowCount(0)
        for a, b in sorted(self.cfg.rules, key=str):
            r = self.m_rules.rowCount()
            self.m_rules.insertRow(r)
            ia = QTableWidgetItem("%s: %s" % (a[0], self.cfg.label(*a)))
            ia.setData(Qt.ItemDataRole.UserRole, (a, b))
            self.m_rules.setItem(r, 0, ia)
            self.m_rules.setItem(
                r, 1, QTableWidgetItem("%s: %s" % (b[0], self.cfg.label(*b)))
            )
        for combo in (self.r_from, self.r_to):
            combo.clear()
            for key, label in self._group_items():
                combo.addItem(label, key)

    def _rule_add(self):
        a, b = self.r_from.currentData(), self.r_to.currentData()
        if not a or not b:
            return
        pair = tuple(sorted((tuple(a), tuple(b)), key=str))
        self._run(
            "Rules",
            lambda: (
                S.save_rules(self.cfg.path, self.cfg.rules | {pair}),
                self._reload_cfg(),
                self._fill_rules(),
            ),
        )

    def _rule_del(self):
        rows = sorted({i.row() for i in self.m_rules.selectedIndexes()})
        drop = {
            tuple(
                map(
                    tuple,
                    self.m_rules.item(r, 0).data(Qt.ItemDataRole.UserRole),
                )
            )
            for r in rows
        }
        if drop:
            self._run(
                "Rules",
                lambda: (
                    S.save_rules(self.cfg.path, self.cfg.rules - drop),
                    self._reload_cfg(),
                    self._fill_rules(),
                ),
            )

    # ================================================================ Editing
    def _tab_editing(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        sbox = QGroupBox(
            "Smart editing (while you draw with the normal QGIS tools)"
        )
        sl = QFormLayout(sbox)
        self.e_on = QCheckBox("Turn smart editing on")
        self.e_on.toggled.connect(self._smart_toggled)
        sl.addRow(self.e_on)
        self.e_snap = QCheckBox("Snap new points and line ends to the network")
        self.e_snap.setChecked(True)
        self.e_split = QCheckBox(
            "Split the line under a new device (otherwise add a vertex)"
        )
        self.e_rules = QCheckBox(
            "Warn at once when a connectivity rule is broken"
        )
        self.e_rules.setChecked(True)
        for c in (self.e_snap, self.e_split, self.e_rules):
            c.toggled.connect(self._smart_options)
            sl.addRow(c)
        self.e_dist = _spin(0, 0, 1000, 3)
        self.e_dist.setSpecialValueText("gap distance")
        self.e_dist.valueChanged.connect(self._smart_options)
        sl.addRow("Snap distance", self.e_dist)
        self.e_state = QLabel("Smart editing is off.")
        sl.addRow(self.e_state)
        lay.addWidget(sbox)
        tbox = QGroupBox("Editor tracking")
        tl = QVBoxLayout(tbox)
        tl.addWidget(
            QLabel(
                "Every class records who created / last edited each feature"
                " and when (created_user, created_date, last_edited_user,"
                " last_edited_date)."
            )
        )
        up = QPushButton(
            "Upgrade network (add tracking fields and new tables)"
        )
        up.setToolTip("Needed once for networks created by version 0.1.")
        up.clicked.connect(self._upgrade)
        tl.addWidget(up)
        lay.addWidget(tbox)
        fbox = QGroupBox("Forms and styles")
        fl = QVBoxLayout(fbox)
        rf = QPushButton("Re-apply forms and styles to the network layers")
        rf.clicked.connect(
            lambda: self._run(
                "Forms",
                lambda: (
                    Q.refresh_forms(self.cfg),
                    self._msg("Forms and styles applied."),
                ),
            )
        )
        fl.addWidget(rf)
        lay.addWidget(fbox)
        lay.addStretch(1)
        return w

    def _smart_options(self, *_a):
        e = self.editor
        e.snap, e.split, e.check_rules = (
            self.e_snap.isChecked(),
            self.e_split.isChecked(),
            self.e_rules.isChecked(),
        )
        e.distance = self.e_dist.value() or None

    def _smart_toggled(self, on):
        self._smart_options()
        self.editor.cfg = self.cfg
        self.editor.set_enabled(on)
        n = len(self.editor._connected)
        self.e_state.setText(
            "Smart editing is on for %d layer(s)." % n
            if on
            else "Smart editing is off."
        )
        if on and n == 0:
            self.e_state.setText("Add the network layers to the map first.")

    def _upgrade(self):
        def go():
            self._auto("upgrade")
            n = S.upgrade_network(self.cfg.path, self.cfg.classes)
            for cls in self.cfg.classes.values():
                lyr = Q.project_layer(self.cfg.path, cls)
                if lyr is not None:
                    lyr.dataProvider().reloadData()
                    lyr.reload()
            self._reload_cfg()
            Q.refresh_forms(self.cfg)
            self._msg("Network upgraded (%d field(s) added)." % n, "Success")

        self._run("Upgrade", go)

    # ================================================================ Reports
    def _tab_reports(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.rp_life = QComboBox()
        for label, codes in (
            ("All features", None),
            ("In service", [3]),
            ("Proposed (design)", [1]),
            ("Under construction", [2]),
            ("Proposed + under construction", [1, 2]),
            ("Out of service / abandoned / removed", [4, 5, 6]),
        ):
            self.rp_life.addItem(label, codes)
        form.addRow("Lifecycle", self.rp_life)
        self.rp_sel = QCheckBox("Selected features only")
        form.addRow("", self.rp_sel)
        lay.addLayout(form)
        row = QHBoxLayout()
        sm = QPushButton("Network summary")
        sm.clicked.connect(self._report_summary)
        bq = QPushButton("Bill of quantities to Excel...")
        bq.clicked.connect(self._report_boq)
        row.addWidget(sm)
        row.addWidget(bq)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        er = QPushButton("Validation errors to Excel...")
        er.clicked.connect(self._report_errors)
        sb = QPushButton("Subnetworks to Excel...")
        sb.clicked.connect(self._report_subnetworks)
        row2.addWidget(er)
        row2.addWidget(sb)
        lay.addLayout(row2)
        row3 = QHBoxLayout()
        dd = QPushButton("Data dictionary to Excel...")
        dd.setToolTip(
            "Classes, fields, asset groups / types, rules, terminals, tiers,"
            " network attributes, domains."
        )
        dd.clicked.connect(
            lambda: (
                lambda p: p
                and self._run(
                    "Data dictionary",
                    lambda: self.rp_out.setPlainText(
                        "Saved: %s"
                        % reports.write_xlsx(
                            p, reports.data_dictionary(self.cfg)
                        )
                    ),
                )
            )(self._ask_xlsx("data_dictionary.xlsx"))
        )
        qa = QPushButton("QA report to Excel...")
        qa.clicked.connect(self._qa_report)
        row3.addWidget(dd)
        row3.addWidget(qa)
        lay.addLayout(row3)
        self.rp_out = QPlainTextEdit()
        self.rp_out.setReadOnly(True)
        self.rp_out.setMinimumHeight(380)
        f = QFont("Monospace")
        f.setStyleHint(QFont.StyleHint.TypeWriter)
        self.rp_out.setFont(f)
        lay.addWidget(self.rp_out)
        return w

    def _report_summary(self):
        def go():
            rows, totals = reports.summary(
                self.cfg, self.rp_life.currentData(), self.rp_sel.isChecked()
            )
            self.rp_out.setPlainText(
                reports.report_text(self.cfg, rows, totals)
            )

        self._run("Summary", go)

    def _ask_xlsx(self, name):
        path, _f = QFileDialog.getSaveFileName(
            self, "Save Excel file", name, "Excel (*.xlsx)"
        )
        return path

    def _report_boq(self):
        path = self._ask_xlsx("bill_of_quantities.xlsx")
        if not path:
            return

        def go():
            lines, points = reports.boq(
                self.cfg, self.rp_life.currentData(), self.rp_sel.isChecked()
            )
            rows, _t = reports.summary(
                self.cfg, self.rp_life.currentData(), self.rp_sel.isChecked()
            )
            out = reports.write_xlsx(
                path,
                {
                    "Lines": lines,
                    "Devices and junctions": points,
                    "Summary": rows,
                },
            )
            self.rp_out.setPlainText(
                "Bill of quantities saved:\n%s\n\nLines: %d rows\nPoints: %d"
                " rows" % (out, len(lines), len(points))
            )

        self._run("Bill of quantities", go)

    def _report_errors(self):
        path = self._ask_xlsx("validation_errors.xlsx")
        if not path:
            return

        def go():
            sheets = {}
            for name in ("un_errors_point", "un_errors_line"):
                lyr = Q.get_layer(self.cfg.path, name)
                sheets["Points" if name.endswith("point") else "Lines"] = [
                    {
                        f.name(): (
                            None
                            if v is None
                            or (hasattr(v, "isNull") and v.isNull())
                            else v
                        )
                        for f, v in zip(lyr.fields(), feat.attributes())
                    }
                    for feat in lyr.getFeatures()
                ]
            self.rp_out.setPlainText(
                "Saved: %s" % reports.write_xlsx(path, sheets)
            )

        self._run("Errors", go)

    def _report_subnetworks(self):
        path = self._ask_xlsx("subnetworks.xlsx")
        if path:
            self._run(
                "Subnetworks",
                lambda: self.rp_out.setPlainText(
                    "Saved: %s"
                    % reports.write_xlsx(
                        path, {"Subnetworks": self.cfg.subnetworks}
                    )
                ),
            )

    # ================================================================
    # Hydraulic export
    def _tab_export(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Export the network as a hydraulic model. Pressure networks"
                " (water, cooling) go to EPANET; gravity networks (sewer,"
                " storm) go to SWMM."
            )
        )
        ebox = QGroupBox("EPANET (.inp)")
        ef = QFormLayout(ebox)
        self.ep_d = _spin(100, 1, 5000, 1, " mm")
        self.ep_r = _spin(130, 1, 200, 1)
        self.ep_q = _spin(0.01, 0, 100, 4, " L/s")
        self.ep_h = _spin(50, 0, 1000, 1, " m")
        ef.addRow("Diameter when empty", self.ep_d)
        ef.addRow("Hazen-Williams C", self.ep_r)
        ef.addRow("Demand per customer", self.ep_q)
        ef.addRow("Source head above ground", self.ep_h)
        eb = QPushButton("Export to EPANET...")
        eb.clicked.connect(self._export_epanet)
        ef.addRow(eb)
        lay.addWidget(ebox)
        sbox = QGroupBox("SWMM (.inp)")
        sf = QFormLayout(sbox)
        self.sw_d = _spin(300, 1, 10000, 1, " mm")
        self.sw_n = _spin(0.013, 0.001, 1, 4)
        sf.addRow("Diameter when empty", self.sw_d)
        sf.addRow("Manning n", self.sw_n)
        sb = QPushButton("Export to SWMM...")
        sb.clicked.connect(self._export_swmm)
        sf.addRow(sb)
        lay.addWidget(sbox)
        rbox = QGroupBox("EPANET results on the map")
        rl = QVBoxLayout(rbox)
        rl.addWidget(
            QLabel(
                "Run the exported model in EPANET, save the report (.rpt),"
                " then load it here: pressures on the nodes and velocities on"
                " the pipes, coloured in 5 classes."
            )
        )
        lb = QPushButton("Load EPANET report (.rpt)...")
        lb.clicked.connect(self._epanet_results)
        rl.addWidget(lb)
        lay.addWidget(rbox)
        self.ex_out = QPlainTextEdit()
        self.ex_out.setReadOnly(True)
        self.ex_out.setMaximumHeight(120)
        lay.addWidget(self.ex_out)
        lay.addStretch(1)
        return w

    def _export_epanet(self):
        path, _f = QFileDialog.getSaveFileName(
            self, "EPANET file", "network.inp", "EPANET (*.inp)"
        )
        if path:
            self._run(
                "EPANET",
                lambda: self.ex_out.setPlainText(
                    "Saved %s\n%s"
                    % (
                        path,
                        hydraulics.export_epanet(
                            self.cfg,
                            self.network(),
                            path,
                            self.ep_d.value(),
                            self.ep_r.value(),
                            self.ep_q.value(),
                            self.ep_h.value(),
                        ),
                    )
                ),
            )

    def _export_swmm(self):
        path, _f = QFileDialog.getSaveFileName(
            self, "SWMM file", "network.inp", "SWMM (*.inp)"
        )
        if path:
            self._run(
                "SWMM",
                lambda: self.ex_out.setPlainText(
                    "Saved %s\n%s"
                    % (
                        path,
                        hydraulics.export_swmm(
                            self.cfg,
                            self.network(),
                            path,
                            self.sw_d.value(),
                            self.sw_n.value(),
                        ),
                    )
                ),
            )

    # ================================================================
    # Associations
    def _tab_assoc(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Associations relate features without touching: connectivity"
                " (two devices joined by a jumper), containment (equipment"
                " inside a station / cabinet) and structural attachment (a"
                " transformer on a pole)."
            )
        )
        self.as_label = QLabel()
        lay.addWidget(self.as_label)
        abox = QGroupBox("Automatic")
        al = QFormLayout(abox)
        self.att_dist = _spin(1.0, 0, 1000, 2)
        al.addRow("Attachment distance", self.att_dist)
        ab = QPushButton("Create containment and attachment")
        ab.clicked.connect(self._associations)
        al.addRow(ab)
        lay.addWidget(abox)
        mbox = QGroupBox("Manual")
        ml = QVBoxLayout(mbox)
        ml.addWidget(
            QLabel(
                "Select exactly two network features on the map (any classes),"
                " then:"
            )
        )
        row = QHBoxLayout()
        for kind in ("connectivity", "containment", "attachment"):
            b = QPushButton("Add %s" % kind)
            b.clicked.connect(
                lambda _c=False, k=kind: self._assoc_from_selection(k)
            )
            row.addWidget(b)
        ml.addLayout(row)
        rm = QPushButton("Remove associations of the selected features")
        rm.clicked.connect(self._assoc_remove_selection)
        ml.addWidget(rm)
        sh = QPushButton("Select the features associated with the selection")
        sh.clicked.connect(self._assoc_show)
        ml.addWidget(sh)
        lay.addWidget(mbox)
        ibox = QGroupBox("Import / export")
        il = QHBoxLayout(ibox)
        ex = QPushButton("Export associations (CSV)...")
        ex.clicked.connect(
            lambda: self._csv_export("un_associations", "associations.csv")
        )
        im = QPushButton("Import associations (CSV)...")
        im.clicked.connect(lambda: self._csv_import("un_associations"))
        il.addWidget(ex)
        il.addWidget(im)
        lay.addWidget(ibox)
        self.as_out = QPlainTextEdit()
        self.as_out.setReadOnly(True)
        self.as_out.setMaximumHeight(120)
        lay.addWidget(self.as_out)
        lay.addStretch(1)
        return w

    def _associations(self):
        def go():
            self._auto("associations")
            res = prepare.associations(self.cfg, self.att_dist.value())
            self._reload_cfg()
            self._update_assoc_label()
            self.as_out.setPlainText(
                "Associations\n  containment: %(containment)d\n  attachment:"
                " %(attachment)d\n  connectivity (kept): %(connectivity)d"
                % res
            )

        self._run("Associations", go)

    def _update_assoc_label(self):
        if self.cfg is None:
            return
        cnt = {}
        for t, _a, _b in self.cfg.associations:
            cnt[t] = cnt.get(t, 0) + 1
        self.as_label.setText(
            "Associations: "
            + (", ".join("%s %d" % kv for kv in sorted(cnt.items())) or "none")
        )

    def _selected_keys(self):
        keys = []
        for role, cls in self.cfg.classes.items():
            lyr = Q.project_layer(self.cfg.path, cls)
            if lyr is not None:
                keys += [(cls, fid) for fid in lyr.selectedFeatureIds()]
        return keys

    def _assoc_from_selection(self, kind):
        def go():
            keys = self._selected_keys()
            if len(keys) != 2:
                raise ValueError(
                    "Select exactly two network features (now %d)." % len(keys)
                )
            a, b = keys
            if kind in ("containment", "attachment"):
                poly_or_struct = [
                    k
                    for k in keys
                    if self.cfg.role_of.get(k[0], "").startswith(
                        ("Assembly", "Structure")
                    )
                ]
                if poly_or_struct:
                    a = poly_or_struct[0]
                    b = keys[1] if keys[0] == a else keys[0]
            assoc = [x for x in self.cfg.associations] + [(kind, a, b)]
            S.save_associations(self.cfg.path, assoc)
            self._reload_cfg()
            self._update_assoc_label()
            self.as_out.setPlainText(
                "Added %s: %s #%s -> %s #%s" % (kind, a[0], a[1], b[0], b[1])
            )

        self._run("Associations", go)

    def _assoc_remove_selection(self):
        def go():
            keys = set(self._selected_keys())
            keep = [
                x
                for x in self.cfg.associations
                if x[1] not in keys and x[2] not in keys
            ]
            n = len(self.cfg.associations) - len(keep)
            S.save_associations(self.cfg.path, keep)
            self._reload_cfg()
            self._update_assoc_label()
            self.as_out.setPlainText("Removed %d association(s)." % n)

        self._run("Associations", go)

    def _assoc_show(self):
        def go():
            keys = set(self._selected_keys())
            rel = set()
            for _t, a, b in self.cfg.associations:
                if a in keys:
                    rel.add(b)
                if b in keys:
                    rel.add(a)
            from .engine import TraceResult

            r = TraceResult()
            r.points = rel
            Q.select_result(self.cfg, r, zoom_canvas=self.iface.mapCanvas())
            self.as_out.setPlainText(
                "%d associated feature(s) selected." % len(rel)
            )

        self._run("Associations", go)

    # ================================================================ CSV
    # helpers
    def _csv_export(self, table, name):
        path, _f = QFileDialog.getSaveFileName(
            self, "Export CSV", name, "CSV (*.csv)"
        )
        if path:
            self._run(
                "Export",
                lambda: self._msg(
                    "%d row(s) exported to %s"
                    % (
                        S.export_csv(
                            S.read_table(self.cfg.path, table),
                            [n for n, _t in S.SYSTEM_TABLES[table]],
                            path,
                        ),
                        path,
                    ),
                    "Success",
                ),
            )

    def _csv_import(self, table):
        path, _f = QFileDialog.getOpenFileName(
            self, "Import CSV", "", "CSV (*.csv)"
        )
        if not path:
            return

        def go():
            rows = S.import_csv(path, table)
            replace = (
                QMessageBox.question(
                    self,
                    "Import",
                    "Replace the current rows (Yes) or add to them (No)?",
                )
                == QMessageBox.StandardButton.Yes
            )
            if not replace:
                rows = S.read_table(self.cfg.path, table) + rows
            S.save_table(self.cfg.path, table, rows)
            self._reload_cfg()
            self._refresh_forms()
            self._msg("%d row(s) imported." % len(rows), "Success")

        self._run("Import", go)

    # ================================================================ Model:
    # attributes and tiers
    def _model_extra(self, lay):
        nbox = QGroupBox("Network attributes (used by trace conditions)")
        nl = QVBoxLayout(nbox)
        self.m_attrs = QTableWidget(0, 3)
        self.m_attrs.setHorizontalHeaderLabels(
            ["Network attribute", "Class", "Field"]
        )
        self.m_attrs.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.m_attrs.setMinimumHeight(160)
        nl.addWidget(self.m_attrs)
        nr = QHBoxLayout()
        for label, fn in (
            (
                "Add",
                lambda: self._table_add(
                    self.m_attrs, ["New attribute", "", ""]
                ),
            ),
            ("Remove", lambda: self._table_del(self.m_attrs)),
            ("Save attributes", self._save_attrs),
        ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            nr.addWidget(b)
        nl.addLayout(nr)
        lay.addWidget(nbox)
        tbox = QGroupBox("Tiers and subnetwork definition")
        tl = QVBoxLayout(tbox)
        self.m_tiers = QTableWidget(0, 5)
        self.m_tiers.setHorizontalHeaderLabels(
            [
                "Tier",
                "Rank",
                "Tier group",
                "Many controllers (0/1)",
                "Controller required (0/1)",
            ]
        )
        self.m_tiers.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.m_tiers.setMinimumHeight(120)
        tl.addWidget(self.m_tiers)
        tr = QHBoxLayout()
        for label, fn in (
            (
                "Add tier",
                lambda: self._table_add(
                    self.m_tiers,
                    [
                        "New tier",
                        str(self.m_tiers.rowCount() + 1),
                        "",
                        "1",
                        "0",
                    ],
                ),
            ),
            ("Remove tier", lambda: self._table_del(self.m_tiers)),
            ("Save tiers", self._save_tiers),
        ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            tr.addWidget(b)
        tl.addLayout(tr)
        lay.addWidget(tbox)
        rbox = QGroupBox("Rules import / export")
        rl = QHBoxLayout(rbox)
        ex = QPushButton("Export rules (CSV)...")
        ex.clicked.connect(lambda: self._csv_export("un_rules", "rules.csv"))
        im = QPushButton("Import rules (CSV)...")
        im.clicked.connect(lambda: self._csv_import("un_rules"))
        rl.addWidget(ex)
        rl.addWidget(im)
        lay.addWidget(rbox)

    def _table_add(self, table, values):
        r = table.rowCount()
        table.insertRow(r)
        for c, v in enumerate(values):
            table.setItem(r, c, QTableWidgetItem(v))

    def _table_del(self, table):
        if table.currentRow() >= 0:
            table.removeRow(table.currentRow())

    def _table_rows(self, table):
        out = []
        for r in range(table.rowCount()):
            out.append(
                [
                    (
                        table.item(r, c).text().strip()
                        if table.item(r, c)
                        else ""
                    )
                    for c in range(table.columnCount())
                ]
            )
        return out

    def _fill_model_extra(self):
        self.m_attrs.setRowCount(0)
        for r in self.cfg.attribute_rows:
            self._table_add(
                self.m_attrs,
                [
                    r["name"] or "",
                    r["class_name"] or "",
                    r["field_name"] or "",
                ],
            )
        self.m_tiers.setRowCount(0)
        for r in self.cfg.tier_rows:
            self._table_add(
                self.m_tiers,
                [
                    r["name"] or "",
                    str(r.get("rank") or ""),
                    r.get("tier_group") or "",
                    str(r.get("multi_controllers") or 0),
                    str(r.get("require_controller") or 0),
                ],
            )

    def _save_attrs(self):
        rows = [
            {"name": a, "class_name": c, "field_name": f}
            for a, c, f in self._table_rows(self.m_attrs)
            if a and c and f
        ]
        self._run(
            "Network attributes",
            lambda: (
                S.save_table(self.cfg.path, "un_network_attributes", rows),
                self._reload_cfg(),
                self._fill_conditions_attrs(),
                self._msg("Network attributes saved."),
            ),
        )

    def _save_tiers(self):
        def num(v, d=0):
            try:
                return int(float(v))
            except ValueError:
                return d

        rows = [
            {
                "name": n,
                "rank": num(r, i + 1),
                "tier_group": g,
                "multi_controllers": num(m),
                "require_controller": num(q),
            }
            for i, (n, r, g, m, q) in enumerate(self._table_rows(self.m_tiers))
            if n
        ]
        self._run(
            "Tiers",
            lambda: (
                S.save_table(self.cfg.path, "un_tiers", rows),
                self._reload_cfg(),
                self._msg("Tiers saved."),
            ),
        )

    # ================================================================
    # Controllers
    def _controllers_box(self, lay):
        cbox = QGroupBox("Subnetwork controllers")
        cl = QVBoxLayout(cbox)
        cl.addWidget(
            QLabel(
                "Without controllers here, every device of a 'source' asset"
                " group is a controller. Set controllers to name subnetworks"
                " yourself (e.g. a feeder or pressure zone name)."
            )
        )
        self.ctrl_table = QTableWidget(0, 4)
        self.ctrl_table.setHorizontalHeaderLabels(
            ["Class", "FID", "Subnetwork name", "Tier"]
        )
        self.ctrl_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.ctrl_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.ctrl_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.ctrl_table.setMinimumHeight(130)
        cl.addWidget(self.ctrl_table)
        row = QHBoxLayout()
        st = QPushButton("Set selected devices as controllers")
        st.clicked.connect(self._set_controllers)
        rm = QPushButton("Remove selected rows")
        rm.clicked.connect(self._remove_controllers)
        row.addWidget(st)
        row.addWidget(rm)
        cl.addLayout(row)
        row2 = QHBoxLayout()
        ex = QPushButton("Export controllers (CSV)...")
        ex.clicked.connect(
            lambda: self._csv_export(
                "un_controllers", "subnetwork_controllers.csv"
            )
        )
        im = QPushButton("Import controllers (CSV)...")
        im.clicked.connect(lambda: self._csv_import("un_controllers"))
        row2.addWidget(ex)
        row2.addWidget(im)
        cl.addLayout(row2)
        lay.addWidget(cbox)

    def _fill_controllers(self):
        self.ctrl_table.setRowCount(0)
        for r in self.cfg.controller_rows if self.cfg else []:
            i = self.ctrl_table.rowCount()
            self.ctrl_table.insertRow(i)
            for c, v in enumerate(
                (
                    r["class_name"],
                    int(r["feature_fid"]),
                    r.get("subnetwork_name") or "",
                    r.get("tier") or "",
                )
            ):
                self.ctrl_table.setItem(i, c, QTableWidgetItem(str(v)))

    def _set_controllers(self):
        def go():
            dev = self.cfg.classes.get("Device")
            lyr = Q.project_layer(self.cfg.path, dev) if dev else None
            feats = list(lyr.getSelectedFeatures()) if lyr is not None else []
            if not feats:
                raise ValueError(
                    "Select one or more devices on the map first."
                )
            rows = [
                r
                for r in self.cfg.controller_rows
                if not (
                    r["class_name"] == dev
                    and int(r["feature_fid"]) in {f.id() for f in feats}
                )
            ]
            for f in feats:
                default = (
                    str(f["assetid"])
                    if f["assetid"] not in (None, "")
                    and not (
                        hasattr(f["assetid"], "isNull")
                        and f["assetid"].isNull()
                    )
                    else "%s-%d" % (dev, f.id())
                )
                name, ok = QInputDialog.getText(
                    self,
                    "Subnetwork controller",
                    "Subnetwork name for device #%d:" % f.id(),
                    text=default,
                )
                if not ok:
                    return
                ag = Q._int(f["assetgroup"])
                rows.append(
                    {
                        "class_name": dev,
                        "feature_fid": f.id(),
                        "subnetwork_name": name.strip() or default,
                        "tier": self.cfg.tiers.get((dev, ag)),
                    }
                )
            S.save_table(self.cfg.path, "un_controllers", rows)
            self._reload_cfg()
            self._fill_controllers()

        self._run("Controllers", go)

    def _remove_controllers(self):
        rows = sorted({i.row() for i in self.ctrl_table.selectedIndexes()})
        drop = {
            (
                self.ctrl_table.item(r, 0).text(),
                int(self.ctrl_table.item(r, 1).text()),
            )
            for r in rows
        }
        keep = [
            r
            for r in self.cfg.controller_rows
            if (r["class_name"], int(r["feature_fid"])) not in drop
        ]
        self._run(
            "Controllers",
            lambda: (
                S.save_table(self.cfg.path, "un_controllers", keep),
                self._reload_cfg(),
                self._fill_controllers(),
            ),
        )

    def _update_isconnected(self):
        def go():
            self._require_topology()
            keys = self.network().connected_keys()
            n = Q.write_flag_field(self.cfg, "isconnected", keys)
            self._msg(
                "Is connected updated: %d connected feature(s), %d value(s)"
                " changed." % (len(keys), n),
                "Success",
            )

        self._run("Is connected", go)

    def _export_subnetwork(self):
        row = self.sub_list.currentRow()
        if row < 0:
            QMessageBox.information(
                self,
                "Export subnetwork",
                "Select a subnetwork in the list first.",
            )
            return
        name = self.sub_list.item(row, 0).text()
        path, _f = QFileDialog.getSaveFileName(
            self, "Export subnetwork", "%s.gpkg" % name, "GeoPackage (*.gpkg)"
        )
        if not path:
            return

        def go():
            subs = self.network().subnetworks(
                name_of=lambda p: p.get("assetid")
            )
            info = subs["subnetworks"].get(name)
            if info is None:
                raise ValueError(
                    "Subnetwork not found: update the subnetworks."
                )
            n = Q.export_subnetwork(self.cfg, info, path)
            sheets = reports.feature_rows(
                self.cfg, info["lines"] | info["points"]
            )
            xl = reports.write_xlsx(path[:-5] + ".xlsx", sheets)
            self._msg(
                "Subnetwork %s: %d feature(s) exported to %s and %s"
                % (name, n, path, xl),
                "Success",
            )

        self._run("Export subnetwork", go)

    # ================================================================ Trace:
    # conditions and locations
    def _conditions_box(self, lay):
        cbox = QGroupBox(
            "Barrier conditions (the trace stops at features that match)"
        )
        cl = QVBoxLayout(cbox)
        self.cond_table = QTableWidget(0, 3)
        self.cond_table.setHorizontalHeaderLabels(
            ["Network attribute", "Operator", "Value"]
        )
        self.cond_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.cond_table.setMinimumHeight(90)
        cl.addWidget(self.cond_table)
        row = QHBoxLayout()
        self.cond_attr, self.cond_op, self.cond_val = (
            QComboBox(),
            QComboBox(),
            QLineEdit(),
        )
        from .engine import OPS

        self.cond_op.addItems(list(OPS))
        add = QPushButton("Add")
        add.clicked.connect(self._cond_add)
        rm = QPushButton("Remove")
        rm.clicked.connect(lambda: self._table_del(self.cond_table))
        for wdg in (self.cond_attr, self.cond_op, self.cond_val, add, rm):
            row.addWidget(wdg)
        cl.addLayout(row)
        lay.addWidget(cbox)

    def _fill_conditions_attrs(self):
        self.cond_attr.clear()
        self.cond_attr.addItems(
            sorted(self.cfg.network_attributes) if self.cfg else []
        )

    def _cond_add(self):
        self._table_add(
            self.cond_table,
            [
                self.cond_attr.currentText(),
                self.cond_op.currentText(),
                self.cond_val.text().strip(),
            ],
        )

    def _conditions(self):
        return [
            tuple(r)
            for r in self._table_rows(self.cond_table)
            if r[0] and r[1]
        ]

    def _picks_from_selection(self, kind):
        canvas = self.iface.mapCanvas()
        if self.tool is None:
            self.tool = PickTool(canvas, self._layers_by_class)
            self.tool.picked.connect(self._picked)
        self.tool.kind = kind
        n = 0
        for cls, lyr in self._layers_by_class().items():
            for f in lyr.getSelectedFeatures():
                if not f.hasGeometry():
                    continue
                g = f.geometry()
                p = (
                    g.pointOnSurface().asPoint()
                    if Q.gkind(lyr) != "line"
                    else g.interpolate(g.length() / 2).asPoint()
                )
                self.tool.emit_pick(cls, f.id(), p, lyr)
                n += 1
        if not n:
            QMessageBox.information(
                self,
                "Trace locations",
                "Select network features on the map first.",
            )

    def _trace_configs_io(self, export):
        if export:
            path, _f = QFileDialog.getSaveFileName(
                self,
                "Export trace configurations",
                "trace_configurations.json",
                "JSON (*.json)",
            )
            if path:
                self._run(
                    "Export",
                    lambda: open(path, "w", encoding="utf-8").write(
                        json.dumps(self.cfg.trace_configs(), indent=2)
                    ),
                )
            return
        path, _f = QFileDialog.getOpenFileName(
            self, "Import trace configurations", "", "JSON (*.json)"
        )
        if not path:
            return

        def go():
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            for name, conf in data.items():
                S.set_setting(
                    self.cfg.path, "trace_config:" + name, json.dumps(conf)
                )
            self._reload_cfg()
            self._fill_trace_configs()
            self._msg(
                "%d trace configuration(s) imported." % len(data), "Success"
            )

        self._run("Import", go)

    # ================================================================
    # Dashboard
    def _tab_dashboard(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        rf = QPushButton("Refresh dashboard")
        rf.clicked.connect(self._dashboard)
        qa = QPushButton("QA report to Excel...")
        qa.setToolTip(
            "Summary, error summary, all errors, data completeness and"
            " lifecycle - for the client."
        )
        qa.clicked.connect(self._qa_report)
        row.addWidget(rf)
        row.addWidget(qa)
        lay.addLayout(row)
        self.dash = QPlainTextEdit()
        self.dash.setReadOnly(True)
        f = QFont("Monospace")
        f.setStyleHint(QFont.StyleHint.TypeWriter)
        self.dash.setFont(f)
        self.dash.setMinimumHeight(600)
        self.dash.setPlainText("Open a network, then press Refresh dashboard.")
        lay.addWidget(self.dash)
        return w

    def _dashboard(self):
        def go():
            d = reports.dashboard(self.cfg)
            self.dash.setPlainText(
                reports.dashboard_text(self.cfg, d, workorders.stats(self.cfg))
            )

        self._run("Dashboard", go)

    def _qa_report(self):
        path = self._ask_xlsx("QA_report.xlsx")
        if path:
            self._run(
                "QA report",
                lambda: self._msg(
                    "QA report saved: %s"
                    % reports.write_xlsx(
                        path,
                        dict(
                            reports.qa_sheets(
                                self.cfg, reports.dashboard(self.cfg)
                            ),
                            Company=self._company_rows(),
                        ),
                    ),
                    "Success",
                ),
            )

    # ================================================================ Work
    # orders
    def _tab_workorders(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Work orders live in the 'Work orders' layer of the network"
                " file: they are on the map and keep the list of network"
                " features they concern."
            )
        )
        row = QHBoxLayout()
        n1 = QPushButton("New from selected features")
        n1.clicked.connect(lambda: self._wo_new("selection"))
        n2 = QPushButton("New from the last trace")
        n2.setToolTip(
            "E.g. after an isolation trace: valves to close and customers"
            " without supply."
        )
        n2.clicked.connect(lambda: self._wo_new("trace"))
        row.addWidget(n1)
        row.addWidget(n2)
        lay.addLayout(row)
        frow = QHBoxLayout()
        frow.addWidget(QLabel("Show"))
        self.wo_filter = QComboBox()
        self.wo_filter.addItem(
            "Open work (not completed / cancelled)",
            ["Open", "Assigned", "In progress", "On hold"],
        )
        self.wo_filter.addItem("All", None)
        for st in workorders.STATUSES:
            self.wo_filter.addItem(st, [st])
        self.wo_filter.currentIndexChanged.connect(self._refresh_wo)
        frow.addWidget(self.wo_filter, 1)
        lay.addLayout(frow)
        self.wo_table = QTableWidget(0, 7)
        self.wo_table.setHorizontalHeaderLabels(
            ["ID", "Title", "Type", "Status", "Priority", "Assigned to", "Due"]
        )
        self.wo_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.wo_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.wo_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.wo_table.setMinimumHeight(300)
        self.wo_table.cellDoubleClicked.connect(lambda r, c: self._wo_edit())
        lay.addWidget(self.wo_table)
        brow = QHBoxLayout()
        for label, fn in (
            ("Edit...", self._wo_edit),
            ("Select features", self._wo_select),
            ("Delete", self._wo_delete),
            ("Export to Excel...", self._wo_export),
        ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            brow.addWidget(b)
        lay.addLayout(brow)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Set status:"))
        for st in (
            "Assigned",
            "In progress",
            "On hold",
            "Completed",
            "Cancelled",
        ):
            b = QPushButton(st)
            b.clicked.connect(lambda _c=False, s=st: self._wo_status(s))
            srow.addWidget(b)
        lay.addLayout(srow)
        self.wo_stats = QLabel()
        lay.addWidget(self.wo_stats)
        return w

    def _refresh_wo(self, *_a):
        if self.cfg is None or not hasattr(self, "wo_table"):
            return
        try:
            rows = workorders.rows(self.cfg, self.wo_filter.currentData())
            st = workorders.stats(self.cfg)
        except Exception as e:
            Q.log("Work orders: %s" % e, True)
            return
        keep = None
        if self.wo_table.currentRow() >= 0 and self.wo_table.item(
            self.wo_table.currentRow(), 0
        ):
            keep = self.wo_table.item(self.wo_table.currentRow(), 0).data(
                Qt.ItemDataRole.UserRole
            )
        self.wo_table.setRowCount(0)
        for r in rows:
            i = self.wo_table.rowCount()
            self.wo_table.insertRow(i)
            dd = workorders.to_date(r.get("due_date"))
            due = dd.isoformat() if dd else ""
            for c, v in enumerate(
                (
                    r.get("wo_id"),
                    r.get("title"),
                    r.get("wo_type"),
                    r.get("status"),
                    r.get("priority"),
                    r.get("assigned_to"),
                    due,
                )
            ):
                it = QTableWidgetItem(str(v or ""))
                it.setData(Qt.ItemDataRole.UserRole, r["fid"])
                self.wo_table.setItem(i, c, it)
            if r["fid"] == keep:
                self.wo_table.setCurrentCell(i, 0)
        self.wo_stats.setText(
            "  ".join("%s: %d" % kv for kv in st.items() if kv[1])
            or "No work orders yet."
        )

    def _wo_current(self):
        r = self.wo_table.currentRow()
        if r < 0:
            raise ValueError("Select a work order in the list first.")
        fid = self.wo_table.item(r, 0).data(Qt.ItemDataRole.UserRole)
        return next(x for x in workorders.rows(self.cfg) if x["fid"] == fid)

    def _wo_dialog(self, row):
        dlg = QDialog(self)
        dlg.setWindowTitle("Work order %s" % (row.get("wo_id") or ""))
        form = QFormLayout(dlg)
        title = QLineEdit(row.get("title") or "")
        typ, status, prio = QComboBox(), QComboBox(), QComboBox()
        for combo, values, cur in (
            (typ, workorders.TYPES, row.get("wo_type")),
            (status, workorders.STATUSES, row.get("status")),
            (prio, workorders.PRIORITIES, row.get("priority")),
        ):
            combo.addItems(values)
            if cur:
                combo.setCurrentText(str(cur))
        who = QLineEdit(row.get("assigned_to") or "")
        due = QDateEdit()
        due.setCalendarPopup(True)
        d = workorders.to_date(row.get("due_date"))
        due.setDate(
            QDate(d.year, d.month, d.day)
            if d
            else QDate.currentDate().addDays(7)
        )
        desc = QPlainTextEdit(row.get("description") or "")
        desc.setMaximumHeight(90)
        cost = _spin(float(row.get("cost") or 0), 0, 1e12, 2)
        notes = QLineEdit(row.get("notes") or "")
        for label, wdg in (
            ("Title", title),
            ("Type", typ),
            ("Status", status),
            ("Priority", prio),
            ("Assigned to", who),
            ("Due date", due),
            ("Description", desc),
            ("Cost", cost),
            ("Notes", notes),
        ):
            form.addRow(label, wdg)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if not dlg.exec():
            return None
        return {
            "title": title.text().strip(),
            "wo_type": typ.currentText(),
            "status": status.currentText(),
            "priority": prio.currentText(),
            "assigned_to": who.text().strip(),
            "due_date": due.date().toString("yyyy-MM-dd"),
            "description": desc.toPlainText().strip(),
            "cost": cost.value(),
            "notes": notes.text().strip(),
        }

    def _wo_new(self, source):
        def go():
            extra = {}
            if source == "selection":
                keys = self._selected_keys()
                if not keys:
                    raise ValueError(
                        "Select the network features of the work first."
                    )
            else:
                res = self.last_result
                if res is None or not res.count():
                    raise ValueError("Run a trace first.")
                keys = sorted(res.lines | res.points)
                if res.extra.get("customers") is not None:
                    extra["customers_affected"] = len(
                        res.extra.get("customers", [])
                    )
                if res.extra.get("to_close"):
                    extra["description"] = "Close: " + ", ".join(
                        "%s #%s" % k for k in res.extra["to_close"]
                    )
            values = self._wo_dialog(
                {
                    "title": "",
                    "wo_type": "Outage" if source == "trace" else "Repair",
                    "description": extra.get("description", ""),
                }
            )
            if values is None:
                return
            values.update(
                {k: v for k, v in extra.items() if k != "description"}
            )
            wid = workorders.create(self.cfg, values, keys)
            self._refresh_wo()
            self._msg(
                "Work order %s created for %d feature(s)." % (wid, len(keys)),
                "Success",
            )

        self._run("Work orders", go)

    def _wo_edit(self):
        def go():
            row = self._wo_current()
            values = self._wo_dialog(row)
            if values:
                workorders.update(self.cfg, row["fid"], values)
                self._refresh_wo()

        self._run("Work orders", go)

    def _wo_status(self, status):
        def go():
            row = self._wo_current()
            workorders.update(self.cfg, row["fid"], {"status": status})
            self._refresh_wo()

        self._run("Work orders", go)

    def _wo_select(self):
        def go():
            from .engine import TraceResult

            r = TraceResult()
            r.points = set(workorders.feature_keys(self._wo_current()))
            Q.select_result(self.cfg, r, zoom_canvas=self.iface.mapCanvas())

        self._run("Work orders", go)

    def _wo_delete(self):
        def go():
            row = self._wo_current()
            if (
                QMessageBox.question(
                    self, "Work orders", "Delete %s?" % row.get("wo_id")
                )
                == QMessageBox.StandardButton.Yes
            ):
                workorders.delete(self.cfg, [row["fid"]])
                self._refresh_wo()

        self._run("Work orders", go)

    def _wo_export(self):
        path = self._ask_xlsx("work_orders.xlsx")
        if path:
            self._run(
                "Work orders",
                lambda: self._msg(
                    "Saved: %s"
                    % reports.write_xlsx(
                        path, {"Work orders": workorders.rows(self.cfg)}
                    ),
                    "Success",
                ),
            )

    # ================================================================ Diagrams
    def _tab_diagrams(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "A network diagram is a simplified drawing (schematic) of the"
                " network: every device and junction becomes a box in a tidy"
                " tree that starts at the controller. Diagrams are drawn"
                " beside the network as two new layers."
            )
        )
        form = QFormLayout()
        self.dg_sub = QComboBox()
        form.addRow("Subnetwork", self.dg_sub)
        self.dg_space = _spin(0, 0, 100000, 1)
        self.dg_space.setSpecialValueText("automatic")
        form.addRow("Spacing (map units)", self.dg_space)
        lay.addLayout(form)
        b1 = QPushButton("Draw diagram of the subnetwork")
        b1.clicked.connect(self._diagram_subnetwork)
        b2 = QPushButton("Draw diagram of the last trace result")
        b2.clicked.connect(self._diagram_trace)
        rf = QPushButton("Refresh subnetwork list")
        rf.clicked.connect(self._fill_diagram_subs)
        for b in (b1, b2, rf):
            lay.addWidget(b)
        self.dg_out = QLabel()
        lay.addWidget(self.dg_out)
        lay.addStretch(1)
        return w

    def _fill_diagram_subs(self):
        self.dg_sub.clear()
        for r in self.cfg.subnetworks if self.cfg else []:
            self.dg_sub.addItem(r["name"])

    def _diagram_subnetwork(self):
        def go():
            name = self.dg_sub.currentText()
            if not name:
                raise ValueError(
                    "Update the subnetworks first (Subnetworks page)."
                )
            net = self.network()
            info = net.subnetworks(name_of=lambda p: p.get("assetid"))[
                "subnetworks"
            ].get(name)
            if info is None:
                raise ValueError(
                    "Subnetwork not found: update the subnetworks."
                )
            keys = info["lines"]
            edges = {
                eid
                for eid in range(len(net.edges))
                if net.edge_key(eid) in keys
            }
            ctrl = [
                net.point_node[pi]
                for pi, p in enumerate(net.points)
                if p["key"] == info["controller"]
            ]
            r = diagrams.build(
                self.cfg,
                net,
                edges,
                ctrl,
                self.dg_space.value() or None,
                "Diagram %s" % name,
            )
            self.dg_out.setText(
                "Diagram: %(nodes)d nodes, %(links)d links." % r
            )

        self._run("Diagram", go)

    def _diagram_trace(self):
        def go():
            res = self.last_result
            if res is None or not res.lines:
                raise ValueError("Run a trace that returns lines first.")
            net = self.network()
            edges = {
                eid
                for eid in range(len(net.edges))
                if net.edge_key(eid) in res.lines
            }
            sn, se = self._locate(net, "start")
            roots = list(sn) + [
                net.edges[e][k] for e in se for k in ("a", "b")
            ]
            r = diagrams.build(
                self.cfg,
                net,
                edges,
                roots,
                self.dg_space.value() or None,
                "Diagram trace",
            )
            self.dg_out.setText(
                "Diagram: %(nodes)d nodes, %(links)d links." % r
            )

        self._run("Diagram", go)

    # ================================================================ Map book
    def _tab_mapbook(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Numbered A4 pages over the network (only pages with features)"
                " and a print layout with an atlas. Export it to one PDF or"
                " open it in the Layout Manager."
            )
        )
        form = QFormLayout()
        self.mb_scale = QComboBox()
        for sc in (500, 1000, 2000, 2500, 5000, 10000):
            self.mb_scale.addItem("1:%d" % sc, sc)
        self.mb_scale.setCurrentIndex(1)
        form.addRow("Scale", self.mb_scale)
        self.mb_title = QLineEdit()
        form.addRow("Title", self.mb_title)
        lay.addLayout(form)
        b1 = QPushButton("Create pages and layout")
        b1.clicked.connect(self._mapbook)
        b2 = QPushButton("Export the map book to PDF...")
        b2.clicked.connect(self._mapbook_pdf)
        lay.addWidget(b1)
        lay.addWidget(b2)
        self.mb_out = QLabel()
        lay.addWidget(self.mb_out)
        lay.addStretch(1)
        return w

    def _mapbook(self):
        def go():
            Q.load_network(self.cfg, self.iface)
            grid = mapbook.make_grid(self.cfg, self.mb_scale.currentData())
            title = self.mb_title.text().strip() or self.cfg.settings.get(
                "name", "Network"
            )
            st = QgsSettings()
            brand = {
                k: st.value("NetworkStudio/" + k, "")
                for k in ("company", "contact", "client", "project", "logo")
            }
            self._layout = mapbook.make_layout(
                self.cfg, grid, title, self.mb_scale.currentData(), brand
            )
            self.mb_out.setText(
                "%d page(s). Layout '%s' is in the Layout Manager."
                % (grid.featureCount(), mapbook.LAYOUT_NAME)
            )

        self._run("Map book", go)

    def _mapbook_pdf(self):
        layout = getattr(
            self, "_layout", None
        ) or QgsProject.instance().layoutManager().layoutByName(
            mapbook.LAYOUT_NAME
        )
        if layout is None:
            QMessageBox.information(
                self, "Map book", "Create the pages and layout first."
            )
            return
        path, _f = QFileDialog.getSaveFileName(
            self, "Map book PDF", "map_book.pdf", "PDF (*.pdf)"
        )
        if path:
            self._run(
                "Map book",
                lambda: self._msg(
                    "Map book saved: %s" % mapbook.export_pdf(layout, path),
                    "Success",
                ),
            )

    # ================================================================ ArcGIS
    # import
    def _esri_read(self):
        path = QFileDialog.getExistingDirectory(
            self, "File Geodatabase (.gdb folder)"
        )
        if not path:
            return

        def go():
            self._esri_path = path
            self.es_table.setRowCount(0)
            for name, kind, count, geom in esri_import.inspect(path):
                r = self.es_table.rowCount()
                self.es_table.insertRow(r)
                it = QTableWidgetItem(name)
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(
                    Qt.CheckState.Checked
                    if kind != "other"
                    else Qt.CheckState.Unchecked
                )
                self.es_table.setItem(r, 0, it)
                self.es_table.setItem(r, 1, QTableWidgetItem(kind))
                self.es_table.setItem(r, 2, QTableWidgetItem(str(count)))
                combo = QComboBox()
                combo.addItem("(skip)", None)
                for role, cls in self.cfg.classes.items():
                    combo.addItem("%s (%s)" % (cls, role), role)
                guess = (
                    "Line"
                    if kind == "streets"
                    else esri_import.guess_role(name, geom)
                )
                combo.setCurrentIndex(
                    max(0, combo.findData(guess)) if kind != "other" else 0
                )
                self.es_table.setCellWidget(r, 3, combo)

        self._run("ArcGIS import", go)

    def _esri_import(self):
        def go():
            if not getattr(self, "_esri_path", None):
                raise ValueError("Read a .gdb first.")
            self._auto("arcgis_import")
            out = []
            for r in range(self.es_table.rowCount()):
                if (
                    self.es_table.item(r, 0).checkState()
                    != Qt.CheckState.Checked
                ):
                    continue
                role = self.es_table.cellWidget(r, 3).currentData()
                if role is None:
                    continue
                name, kind = (
                    self.es_table.item(r, 0).text(),
                    self.es_table.item(r, 1).text(),
                )
                if kind == "streets":
                    res = esri_import.import_streets(
                        self.cfg, self._esri_path, name
                    )
                else:
                    res = esri_import.import_utility(
                        self.cfg, self._esri_path, name, role
                    )
                    self._reload_cfg()
                out.append("%s: %s" % (name, res))
            self._reload_cfg()
            if any(
                x[0] in ("C_Associations", "C_SubnetworkControllers")
                for x in [
                    (self.es_table.item(r, 0).text(),)
                    for r in range(self.es_table.rowCount())
                ]
            ) or self._has_package_tables(self._esri_path):
                out.append(
                    "Asset package tables: %s"
                    % asset_package.import_tables(self.cfg, self._esri_path)
                )
                self._reload_cfg()
            self._refresh_forms()
            QMessageBox.information(
                self, "ArcGIS import", "\n".join(out) or "Nothing checked."
            )

        self._run("ArcGIS import", go)

    @staticmethod
    def _has_package_tables(path):
        from osgeo import gdal

        ds = gdal.OpenEx(path, gdal.OF_VECTOR)
        try:
            return (
                ds is not None
                and ds.GetLayerByName("C_Associations") is not None
            )
        finally:
            ds = None

    def _tab_assetpkg(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Export the network as an ArcGIS Utility Network asset package"
                " (File Geodatabase): feature classes in the UtilityNetwork"
                " dataset with their data, asset group / type domains and the"
                " B_ / C_ tables (domain network, categories, network"
                " attributes, edge connectivity, association roles, terminals,"
                " tiers, rules, associations, subnetwork controllers)."
            )
        )
        note = QLabel(
            "ArcGIS needs ASSETGROUP to be a subtype field, which QGIS / GDAL"
            " cannot create. The export writes <name>_make_subtypes.py next to"
            " the package: in ArcGIS Pro open the Python window, run it once,"
            " then run the 'Apply Asset Package' tool."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #854f0b;")
        lay.addWidget(note)
        self.ap_data = QCheckBox(
            "Include the features (data), not only the schema"
        )
        self.ap_data.setChecked(True)
        lay.addWidget(self.ap_data)
        b = QPushButton("Export asset package...")
        b.clicked.connect(self._export_assetpkg)
        lay.addWidget(b)
        self.ap_out = QPlainTextEdit()
        self.ap_out.setReadOnly(True)
        self.ap_out.setMinimumHeight(220)
        lay.addWidget(self.ap_out)
        lay.addWidget(
            QLabel(
                "To bring an asset package into QGIS use Import > Import from"
                " ArcGIS: its feature classes, associations (C_Associations)"
                " and controllers (C_SubnetworkControllers) are read."
            )
        )
        lay.addStretch(1)
        return w

    def _export_assetpkg(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Folder for the asset package"
        )
        if not folder:
            return

        def go():
            name = "".join(
                c if c.isalnum() else "_"
                for c in self.cfg.settings.get("name") or "Network"
            )
            import os as _os

            path, script, counts = asset_package.export(
                self.cfg,
                _os.path.join(folder, name + "_AssetPackage.gdb"),
                self.ap_data.isChecked(),
            )
            self.ap_out.setPlainText(
                "Asset package: %s\nSubtype script: %s\n\nRows written:\n%s"
                % (
                    path,
                    script,
                    "\n".join("  %-36s %d" % kv for kv in counts.items()),
                )
            )

        self._run("Asset package", go)

    def _esri_assoc(self):
        path, _f = QFileDialog.getOpenFileName(
            self, "Associations CSV", "", "CSV (*.csv)"
        )
        if path:
            self._run(
                "Associations",
                lambda: (
                    QMessageBox.information(
                        self,
                        "Associations",
                        str(esri_import.import_associations(self.cfg, path)),
                    ),
                    self._reload_cfg(),
                ),
            )

    # ================================================================ EPANET
    # results / web
    def _epanet_results(self):
        path, _f = QFileDialog.getOpenFileName(
            self, "EPANET report", "", "EPANET report (*.rpt *.txt)"
        )
        if path:
            self._run(
                "EPANET results",
                lambda: self.ex_out.setPlainText(
                    "Loaded: %s"
                    % hydraulics.results_layers(self.cfg, self.network(), path)
                ),
            )

    def _tab_publish(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Prepare the current project for QGIS Server (WMS / WFS) and"
                " Lizmap: the network layers are published with a title and"
                " extent and the project is saved as .qgs."
            )
        )
        form = QFormLayout()
        self.pb_title = QLineEdit()
        self.pb_abstract = QLineEdit()
        self.pb_edit = QCheckBox("Allow editing through the web (WFS-T)")
        self.pb_wo = QCheckBox("Publish the work orders too")
        self.pb_wo.setChecked(True)
        form.addRow("Title", self.pb_title)
        form.addRow("Description", self.pb_abstract)
        form.addRow(self.pb_edit)
        form.addRow(self.pb_wo)
        lay.addLayout(form)
        b = QPushButton("Save the project for web publishing...")
        b.clicked.connect(self._publish)
        lay.addWidget(b)
        self.pb_out = QLabel()
        self.pb_out.setWordWrap(True)
        lay.addWidget(self.pb_out)
        lay.addWidget(
            QLabel(
                "Lizmap: open the saved project in QGIS and run the Lizmap"
                " plugin once to create its configuration, then upload the"
                " folder to the Lizmap server."
            )
        )
        lay.addStretch(1)
        return w

    def _publish(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Folder for the web project"
        )
        if not folder:
            return

        def go():
            title = self.pb_title.text().strip() or self.cfg.settings.get(
                "name", "Network"
            )
            path, n, note = publish.publish(
                self.cfg,
                folder,
                title,
                self.pb_abstract.text().strip(),
                self.pb_edit.isChecked(),
                self.pb_wo.isChecked(),
            )
            self.pb_out.setText(
                "Saved %s with %d published layer(s).\n%s" % (path, n, note)
            )

        self._run("Web publishing", go)

    # ================================================================ Demo
    def _demo(self):
        from . import templates as TT

        names = ["%s" % t["label"] for t in TT.NETWORK_TYPES]
        label, ok = QInputDialog.getItem(
            self, "Demo network", "Network type:", names, 0, False
        )
        if not ok:
            return
        key = TT.NETWORK_TYPES[names.index(label)]["key"]
        path, _f = QFileDialog.getSaveFileName(
            self,
            "Demo network file",
            "demo_%s.gpkg" % key,
            "GeoPackage (*.gpkg)",
        )
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"

        def go():
            import os as _os

            if _os.path.exists(path):
                raise ValueError("The file already exists: choose a new name.")
            crs = QgsProject.instance().crs()
            authid = (
                crs.authid()
                if crs.isValid() and not crs.isGeographic() and crs.authid()
                else "EPSG:32638"
            )
            res = demo.create(path, key, authid=authid)
            self.open_network(path, add_layers=True)
            bl = QgsVectorLayer(
                S.uri(path, "demo_buildings"), "Demo buildings", "ogr"
            )
            QgsProject.instance().addMapLayer(bl)
            self.iface.mapCanvas().setExtent(bl.extent().buffered(80))
            self.iface.mapCanvas().refresh()
            self._msg(
                "Demo network: %(segments)d street segments, %(buildings)d"
                " buildings, %(services)d services." % res,
                "Success",
            )

        self._run("Demo network", go, need_cfg=False)

    # ================================================================ Branding
    def _tab_branding(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Your company details appear on the map book and in the Excel"
                " reports (saved in your QGIS profile, used for every"
                " network)."
            )
        )
        form = QFormLayout()
        st = QgsSettings()
        self.br_name = QLineEdit(st.value("NetworkStudio/company", ""))
        self.br_contact = QLineEdit(st.value("NetworkStudio/contact", ""))
        self.br_client = QLineEdit(st.value("NetworkStudio/client", ""))
        self.br_project = QLineEdit(st.value("NetworkStudio/project", ""))
        lrow = QHBoxLayout()
        self.br_logo = QLineEdit(st.value("NetworkStudio/logo", ""))
        lb = QPushButton("Browse...")
        lb.clicked.connect(
            lambda: self.br_logo.setText(
                QFileDialog.getOpenFileName(
                    self, "Logo", "", "Images (*.png *.jpg *.jpeg *.svg)"
                )[0]
                or self.br_logo.text()
            )
        )
        lrow.addWidget(self.br_logo, 1)
        lrow.addWidget(lb)
        form.addRow("Company name", self.br_name)
        form.addRow("Contact (phone / e-mail / web)", self.br_contact)
        form.addRow("Logo", lrow)
        form.addRow("Client", self.br_client)
        form.addRow("Project name", self.br_project)
        lay.addLayout(form)
        sv = QPushButton("Save")
        sv.clicked.connect(self._save_branding)
        lay.addWidget(sv)
        lay.addStretch(1)
        return w

    def _save_branding(self):
        st = QgsSettings()
        for key, wdg in (
            ("company", self.br_name),
            ("contact", self.br_contact),
            ("logo", self.br_logo),
            ("client", self.br_client),
            ("project", self.br_project),
        ):
            st.setValue("NetworkStudio/" + key, wdg.text().strip())
        self._msg("Company details saved.", "Success")

    # ================================================================ Asset
    # IDs and fields
    def _tab_fields(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        ibox = QGroupBox("Asset ID generator")
        il = QFormLayout(ibox)
        self.id_class = QComboBox()
        il.addRow("Class", self.id_class)
        self.id_pattern = QLineEdit("{NET}-{GROUP}-{N}")
        self.id_pattern.setToolTip(
            "{NET} network, {CLASS} class, {GROUP} asset group, {TYPE} asset"
            " type (3-letter codes), {N} running number"
        )
        il.addRow("Pattern", self.id_pattern)
        self.id_digits = _spin(6, 1, 12, 0)
        il.addRow("Digits of {N}", self.id_digits)
        self.id_empty = QCheckBox("Only features without an asset id")
        self.id_empty.setChecked(True)
        self.id_sel = QCheckBox("Selected features only")
        il.addRow(self.id_empty)
        il.addRow(self.id_sel)
        gb = QPushButton("Generate asset IDs")
        gb.clicked.connect(self._gen_ids)
        il.addRow(gb)
        lay.addWidget(ibox)
        cbox = QGroupBox("Field calculator (bulk)")
        cl = QFormLayout(cbox)
        self.fc_class = QComboBox()
        self.fc_class.currentIndexChanged.connect(self._fc_fields)
        cl.addRow("Class", self.fc_class)
        self.fc_field = QComboBox()
        cl.addRow("Field", self.fc_field)
        self.fc_expr = QLineEdit("round($length, 2)")
        self.fc_expr.setToolTip(
            "Any QGIS expression, e.g. round($length, 2) or 'Zone ' ||"
            " subnetwork"
        )
        cl.addRow("Expression", self.fc_expr)
        self.fc_sel = QCheckBox("Selected features only")
        self.fc_empty = QCheckBox("Only where the field is empty")
        cl.addRow(self.fc_sel)
        cl.addRow(self.fc_empty)
        cb = QPushButton("Calculate")
        cb.clicked.connect(self._calc)
        cl.addRow(cb)
        lay.addWidget(cbox)
        lay.addWidget(
            QLabel(
                "Changes go into edit mode when the layer is on the map:"
                " review and save."
            )
        )
        lay.addStretch(1)
        return w

    def _fill_field_tools(self):
        for combo in (self.id_class, self.fc_class):
            combo.blockSignals(True)
            combo.clear()
            for role, cls in self.cfg.classes.items():
                combo.addItem("%s (%s)" % (cls, role), role)
            combo.blockSignals(False)
        self._fc_fields()

    def _fc_fields(self, *_a):
        self.fc_field.clear()
        role = self.fc_class.currentData()
        if self.cfg and role:
            lyr = Q.class_layers(self.cfg).get(role)
            if lyr is not None:
                self.fc_field.addItems([f.name() for f in lyr.fields()])

    def _gen_ids(self):
        self._run(
            "Asset IDs",
            lambda: self._msg(
                "%d asset id(s) written."
                % tools.generate_ids(
                    self.cfg,
                    self.id_class.currentData(),
                    self.id_pattern.text().strip(),
                    self.id_empty.isChecked(),
                    self.id_sel.isChecked(),
                    int(self.id_digits.value()),
                ),
                "Success",
            ),
        )

    def _calc(self):
        self._run(
            "Field calculator",
            lambda: self._msg(
                "%d feature(s) updated."
                % tools.calculate(
                    self.cfg,
                    self.fc_class.currentData(),
                    self.fc_field.currentText(),
                    self.fc_expr.text(),
                    self.fc_sel.isChecked(),
                    self.fc_empty.isChecked(),
                ),
                "Success",
            ),
        )

    # ================================================================ Profiles
    def _tab_profiles(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Longitudinal profile of a gravity path (sewer / storm):"
                " ground, pipe invert and crown, slopes, chainage. Use a"
                " Shortest path trace between two manholes, or select one"
                " continuous chain of pipes."
            )
        )
        b1 = QPushButton("Profile of the last trace result")
        b1.clicked.connect(lambda: self._profile("trace"))
        b2 = QPushButton("Profile of the selected pipes")
        b2.clicked.connect(lambda: self._profile("selection"))
        lay.addWidget(b1)
        lay.addWidget(b2)
        self.pf_img = QLabel()
        self.pf_img.setMinimumHeight(360)
        self.pf_img.setScaledContents(False)
        lay.addWidget(self.pf_img)
        rb = QHBoxLayout()
        sp = QPushButton("Save image...")
        sp.clicked.connect(self._profile_save)
        sx = QPushButton("Profile table to Excel...")
        sx.clicked.connect(self._profile_xlsx)
        rb.addWidget(sp)
        rb.addWidget(sx)
        lay.addLayout(rb)
        lay.addStretch(1)
        return w

    def _profile(self, source):
        def go():
            import os as _os
            import tempfile

            net = self.network()
            if source == "trace":
                if self.last_result is None:
                    raise ValueError("Run a Shortest path trace first.")
                keys = self.last_result.lines
            else:
                keys = set(
                    k
                    for k in self._selected_keys()
                    if self.cfg.role_of.get(k[0]) == "Line"
                )
            edges = [
                e
                for e in range(len(net.edges))
                if net.edge_key(e) in keys
                and net.edges[e]["line"] is not None
                and "service" not in net.line_cats(net.edges[e]["line"])
            ]
            sn, se = self._locate(net, "start")
            rows = profile.build(self.cfg, net, edges, list(sn))
            path = _os.path.join(
                tempfile.gettempdir(), "network_studio_profile.png"
            )
            profile.draw(
                rows,
                path,
                "%s - longitudinal profile"
                % self.cfg.settings.get("name", ""),
            )
            from qgis.PyQt.QtGui import QPixmap

            self.pf_img.setPixmap(
                QPixmap(path).scaledToWidth(
                    max(600, self.stack.width() - 60),
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self._pf = (rows, path)

        self._run("Profile", go)

    def _profile_save(self):
        if not getattr(self, "_pf", None):
            return
        path, _f = QFileDialog.getSaveFileName(
            self, "Profile image", "profile.png", "PNG (*.png)"
        )
        if path:
            import shutil

            shutil.copy(self._pf[1], path)
            self._msg("Saved: %s" % path, "Success")

    def _profile_xlsx(self):
        if not getattr(self, "_pf", None):
            return
        path = self._ask_xlsx("profile.xlsx")
        if path:
            self._run(
                "Profile",
                lambda: self._msg(
                    "Saved: %s"
                    % reports.write_xlsx(
                        path,
                        {
                            "Profile": [
                                {k: v for k, v in r.items() if k != "node"}
                                for r in self._pf[0]
                            ]
                        },
                    ),
                    "Success",
                ),
            )

    # ================================================================ Risk
    def _tab_risk(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Consequence = customers who lose supply when a line breaks"
                " (its valve segment is isolated).\nLikelihood = age (install"
                " date vs. expected life) or condition (1 good ... 5 very"
                " poor), the worse one.\nRisk = likelihood x consequence (1 -"
                " 25). The result is written into the line fields and ranked."
            )
        )
        form = QFormLayout()
        self.rk_life = _spin(50, 5, 150, 0, " years")
        form.addRow("Expected life", self.rk_life)
        lay.addLayout(form)
        rb = QPushButton("Run criticality and risk analysis")
        rb.clicked.connect(self._risk)
        lay.addWidget(rb)
        row = QHBoxLayout()
        ml = QPushButton("Show risk map")
        ml.clicked.connect(
            lambda: self._run("Risk map", lambda: risk.risk_layer(self.cfg))
        )
        ex = QPushButton("Renewal priority list to Excel...")
        ex.clicked.connect(self._risk_xlsx)
        row.addWidget(ml)
        row.addWidget(ex)
        lay.addLayout(row)
        self.rk_table = QTableWidget(0, 7)
        self.rk_table.setHorizontalHeaderLabels(
            [
                "Priority",
                "Asset id",
                "Asset group",
                "Customers",
                "Likelihood",
                "Consequence",
                "Risk",
            ]
        )
        self.rk_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.rk_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.rk_table.setMinimumHeight(320)
        self.rk_table.cellDoubleClicked.connect(self._risk_select)
        lay.addWidget(self.rk_table)
        return w

    def _risk(self):
        def go():
            self._require_topology()
            rows, nseg = risk.run(
                self.cfg, self.network(), self.rk_life.value()
            )
            self._risk_rows = rows
            self.rk_table.setRowCount(0)
            for r in rows[:500]:
                i = self.rk_table.rowCount()
                self.rk_table.insertRow(i)
                for c, v in enumerate(
                    (
                        r["Priority"],
                        r["assetid"] or "#%d" % r["fid"],
                        r["Asset group"],
                        r["Customers affected"],
                        r["Likelihood"],
                        r["Consequence"],
                        "%d %s" % (r["Risk score"], r["Risk class"]),
                    )
                ):
                    it = QTableWidgetItem(str(v))
                    it.setData(Qt.ItemDataRole.UserRole, r["fid"])
                    self.rk_table.setItem(i, c, it)
            self._msg(
                "Risk analysis: %d lines, %d valve segments."
                % (len(rows), nseg),
                "Success",
            )

        self._run("Risk", go)

    def _risk_select(self, row, _c):
        from .engine import TraceResult

        r = TraceResult()
        r.lines = {
            (
                self.cfg.classes["Line"],
                self.rk_table.item(row, 0).data(Qt.ItemDataRole.UserRole),
            )
        }
        Q.select_result(self.cfg, r, zoom_canvas=self.iface.mapCanvas())

    def _risk_xlsx(self):
        if not getattr(self, "_risk_rows", None):
            QMessageBox.information(self, "Risk", "Run the analysis first.")
            return
        path = self._ask_xlsx("renewal_priorities.xlsx")
        if path:
            self._run(
                "Risk",
                lambda: self._msg(
                    "Saved: %s"
                    % reports.write_xlsx(
                        path,
                        {
                            "Renewal priorities": self._risk_rows,
                            "Company": self._company_rows(),
                        },
                    ),
                    "Success",
                ),
            )

    # ================================================================ Cost
    def _tab_cost(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Unit prices per class / asset group / type / size (lines per"
                " metre, points per item). Empty type or size = any."
            )
        )
        self.pr_table = QTableWidget(0, 8)
        self.pr_table.setHorizontalHeaderLabels(
            [
                "Class",
                "Group code",
                "Type code",
                "Size",
                "Unit",
                "Price",
                "Currency",
                "Description",
            ]
        )
        self.pr_table.horizontalHeader().setSectionResizeMode(
            7, QHeaderView.ResizeMode.Stretch
        )
        self.pr_table.setMinimumHeight(260)
        lay.addWidget(self.pr_table)
        row = QHBoxLayout()
        for label, fn in (
            ("Add rows from the data", self._prices_fill),
            ("Remove row", lambda: self._table_del(self.pr_table)),
            ("Save prices", self._prices_save),
        ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        ex = QPushButton("Export prices (CSV)...")
        ex.clicked.connect(
            lambda: self._csv_export("un_unit_prices", "unit_prices.csv")
        )
        im = QPushButton("Import prices (CSV)...")
        im.clicked.connect(lambda: self._csv_import("un_unit_prices"))
        row2.addWidget(ex)
        row2.addWidget(im)
        lay.addLayout(row2)
        form = QFormLayout()
        self.ce_life = QComboBox()
        for label, codes in (
            ("Proposed (design)", [1]),
            ("Proposed + under construction", [1, 2]),
            ("All features", None),
            ("In service", [3]),
        ):
            self.ce_life.addItem(label, codes)
        form.addRow("Estimate for", self.ce_life)
        self.ce_sel = QCheckBox("Selected features only")
        form.addRow(self.ce_sel)
        lay.addLayout(form)
        eb = QPushButton("Cost estimate to Excel...")
        eb.clicked.connect(self._estimate)
        lay.addWidget(eb)
        self.ce_out = QLabel()
        lay.addWidget(self.ce_out)
        return w

    def _fill_prices(self):
        self.pr_table.setRowCount(0)
        for r in (
            S.read_table(self.cfg.path, "un_unit_prices") if self.cfg else []
        ):
            self._price_row(r)

    def _price_row(self, r):
        self._table_add(
            self.pr_table,
            [
                str(r.get(k) if r.get(k) is not None else "")
                for k in (
                    "class_name",
                    "ag_code",
                    "at_code",
                    "size",
                    "unit",
                    "price",
                    "currency",
                    "description",
                )
            ],
        )

    def _prices_fill(self):
        def go():
            for r in cost.template_rows(self.cfg):
                self._price_row(r)

        self._run("Prices", go)

    def _prices_save(self):
        def num(v, f=float):
            try:
                return f(float(v)) if v not in ("", None) else None
            except ValueError:
                return None

        rows = [
            {
                "class_name": c,
                "ag_code": num(g, int),
                "at_code": num(t, int),
                "size": num(sz),
                "unit": u or "each",
                "price": num(p) or 0.0,
                "currency": cur,
                "description": d,
            }
            for c, g, t, sz, u, p, cur, d in self._table_rows(self.pr_table)
            if c and g
        ]
        self._run(
            "Prices",
            lambda: (
                S.save_table(self.cfg.path, "un_unit_prices", rows),
                self._msg("%d price(s) saved." % len(rows), "Success"),
            ),
        )

    def _estimate(self):
        path = self._ask_xlsx("cost_estimate.xlsx")
        if not path:
            return

        def go():
            rows, total, missing = cost.estimate(
                self.cfg, self.ce_life.currentData(), self.ce_sel.isChecked()
            )
            rows.append({"Class": "TOTAL", "Amount": total})
            reports.write_xlsx(
                path, {"Cost estimate": rows, "Company": self._company_rows()}
            )
            self.ce_out.setText(
                "Total: %s  -  %d line(s) without a unit price.  Saved: %s"
                % ("{:,.2f}".format(total), missing, path)
            )

        self._run("Cost estimate", go)

    # ================================================================ History
    def _tab_history(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Every saved change is recorded: added and deleted features,"
                " geometry changes and every attribute change with the old and"
                " the new value, the user and the time."
            )
        )
        self.hs_on = QCheckBox("Record the edit history")
        self.hs_on.setChecked(True)
        self.hs_on.toggled.connect(
            lambda on: setattr(self.recorder, "enabled", on)
        )
        lay.addWidget(self.hs_on)
        row = QHBoxLayout()
        a1 = QPushButton("History of the selected features")
        a1.clicked.connect(lambda: self._history(True))
        a2 = QPushButton("Latest changes")
        a2.clicked.connect(lambda: self._history(False))
        ex = QPushButton("Export to Excel...")
        ex.clicked.connect(self._history_xlsx)
        for b in (a1, a2, ex):
            row.addWidget(b)
        lay.addLayout(row)
        self.hs_table = QTableWidget(0, 7)
        self.hs_table.setHorizontalHeaderLabels(
            ["When", "User", "Class", "FID", "Action", "Field", "Old -> new"]
        )
        self.hs_table.horizontalHeader().setSectionResizeMode(
            6, QHeaderView.ResizeMode.Stretch
        )
        self.hs_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.hs_table.setMinimumHeight(420)
        lay.addWidget(self.hs_table)
        return w

    def _history(self, selected):
        def go():
            fids = set(self._selected_keys()) if selected else None
            if selected and not fids:
                raise ValueError("Select network features on the map first.")
            rows = history.rows(self.cfg, fids=fids, limit=2000)
            self._hs_rows = rows
            self.hs_table.setRowCount(0)
            for r in rows:
                i = self.hs_table.rowCount()
                self.hs_table.insertRow(i)
                change = (
                    ""
                    if r["action"] not in ("update", "delete")
                    else "%s -> %s" % (r["old_value"], r["new_value"])
                )
                for c, v in enumerate(
                    (
                        r["changed_at"],
                        r["user_name"],
                        r["class_name"],
                        r["feature_fid"],
                        r["action"],
                        r["field_name"] or "",
                        change,
                    )
                ):
                    self.hs_table.setItem(
                        i, c, QTableWidgetItem(str(v if v is not None else ""))
                    )

        self._run("History", go)

    def _history_xlsx(self):
        path = self._ask_xlsx("edit_history.xlsx")
        if path:
            self._run(
                "History",
                lambda: self._msg(
                    "Saved: %s"
                    % reports.write_xlsx(
                        path,
                        {"History": history.rows(self.cfg, limit=1000000)},
                    ),
                    "Success",
                ),
            )

    def _company_rows(self):
        st = QgsSettings()
        return [
            {
                "Item": k.capitalize(),
                "Value": st.value("NetworkStudio/" + k, ""),
            }
            for k in ("company", "contact", "client", "project")
        ]

    def closeEvent(self, event):
        self._clear_picks()
        if (
            self.tool is not None
            and self.iface.mapCanvas().mapTool() is self.tool
        ):
            self.iface.mapCanvas().unsetMapTool(self.tool)
        super().closeEvent(event)
