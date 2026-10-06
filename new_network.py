"""'New network' dialog: the network type is chosen first and drives everything else."""
import os

from qgis.core import QgsCoordinateReferenceSystem, QgsProject
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import storage as S
from . import templates as T

ROLE_TITLES = {"Device": "Devices (points)", "Line": "Lines", "Junction": "Junctions (points)",
               "Assembly": "Assemblies (polygons)", "StructureJunction": "Structure junctions",
               "StructureLine": "Structure lines", "StructureBoundary": "Structure boundaries"}
CAT_TEXT = {"source": "source", "isolating": "isolating", "customer": "customer", "tap": "tap",
            "service": "service", "main": "main", "structure": "structure"}


class PgDialog(QDialog):
    """PostgreSQL / PostGIS connection: one schema per network."""

    def __init__(self, parent=None, current=""):
        super().__init__(parent)
        self.setWindowTitle("PostGIS connection")
        p = S.pg_parts(current) if S.is_pg(current) else {}
        form = QFormLayout(self)
        self.host = QLineEdit(p.get("host", "localhost"))
        self.port = QLineEdit(p.get("port", "5432"))
        self.db = QLineEdit(p.get("dbname", ""))
        self.user = QLineEdit(p.get("user", ""))
        self.pw = QLineEdit(p.get("password", ""))
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.schema = QLineEdit(p.get("active_schema", "network"))
        self.schema.setToolTip("Each network lives in its own schema: several networks can share one database.")
        for label, w in (("Host", self.host), ("Port", self.port), ("Database", self.db), ("User", self.user),
                         ("Password", self.pw), ("Schema (network name)", self.schema)):
            form.addRow(label, w)
        note = QLabel("The password is kept in the QGIS project / settings in plain text. For shared work use "
                      "a PostgreSQL service file or QGIS authentication instead.")
        note.setWordWrap(True)
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def connection(self):
        return S.pg_string(self.host.text().strip(), self.port.text().strip() or "5432", self.db.text().strip(),
                           self.user.text().strip(), self.pw.text(), self.schema.text().strip() or "network")


class NewNetworkDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New network")
        self.resize(900, 620)
        self.created_path = None
        lay = QVBoxLayout(self)

        intro = QLabel("1. Choose the network type. Its classes, asset groups and asset types are shown on "
                       "the right.  2. Choose where to save it.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        split = QSplitter()
        self.types = QListWidget()
        for t in T.NETWORK_TYPES:
            it = QListWidgetItem(t["label"])
            it.setData(Qt.ItemDataRole.UserRole, t["key"])
            self.types.addItem(it)
        split.addWidget(self.types)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.desc = QLabel()
        self.desc.setWordWrap(True)
        rl.addWidget(self.desc)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Class / asset group / asset type", "Role in the network", "Tier"])
        self.tree.setColumnWidth(0, 320)
        self.tree.setColumnWidth(1, 150)
        rl.addWidget(self.tree)
        split.addWidget(right)
        split.setSizes([260, 640])
        lay.addWidget(split, 1)

        box = QGroupBox("Save")
        form = QFormLayout(box)
        self.fmt = QComboBox()
        for f in S.FORMATS:
            self.fmt.addItem(f.label, f.key)
        form.addRow("Format", self.fmt)
        row = QHBoxLayout()
        self.path = QLineEdit()
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        form.addRow("File", row)
        self.name = QLineEdit()
        form.addRow("Network name", self.name)
        self.crs = QgsProjectionSelectionWidget()
        pcrs = QgsProject.instance().crs()
        self.crs.setCrs(pcrs if pcrs.isValid() and not pcrs.isGeographic() else QgsCoordinateReferenceSystem("EPSG:32638"))
        form.addRow("Coordinate system", self.crs)
        self.tol = QDoubleSpinBox()
        self.tol.setDecimals(4)
        self.tol.setRange(0.0001, 10)
        self.tol.setValue(0.001)
        self.tol.setToolTip("Features closer than this are connected (map units). Use a projected CRS in metres.")
        form.addRow("Connectivity tolerance", self.tol)
        self.gap = QDoubleSpinBox()
        self.gap.setDecimals(3)
        self.gap.setRange(0.001, 1000)
        self.gap.setValue(0.5)
        self.gap.setToolTip("Gaps up to this distance are reported, and closed by 'Prepare network'.")
        form.addRow("Gap distance", self.gap)
        self.add_domain = QCheckBox("Add to an existing file as another domain network (shares its structure network)")
        self.add_domain.setToolTip("Like 'Add Domain Network' in ArcGIS: e.g. electricity and telecom in one file.")
        form.addRow("", self.add_domain)
        self.structures = QCheckBox("Also create the structure network (poles, manholes, ducts, stations)")
        self.structures.setChecked(True)
        form.addRow("", self.structures)
        lay.addWidget(box)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.create_btn = bb.addButton("Create network", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.accepted.connect(self._create)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.types.currentRowChanged.connect(self._show)
        self.structures.toggled.connect(lambda _c: self._show(self.types.currentRow()))
        self.fmt.currentIndexChanged.connect(self._fix_ext)
        self.types.setCurrentRow(0)

    def tpl(self):
        it = self.types.currentItem()
        return T.BY_KEY[it.data(Qt.ItemDataRole.UserRole)] if it else None

    def _show(self, _row):
        tpl = self.tpl()
        if tpl is None:
            return
        flow = {"source": "Flow from sources (pressure network)", "gravity": "Gravity flow (follows line direction)",
                "undirected": "No flow direction (connectivity and shortest path)"}[tpl["flow"]]
        self.desc.setText("<b>%s</b><br>%s<br><i>%s</i>" % (tpl["label"], tpl["description"], flow))
        self.tree.clear()
        for role, cls in T.class_names(tpl, self.structures.isChecked()).items():
            top = QTreeWidgetItem([cls, ROLE_TITLES.get(role, role), ""])
            self.tree.addTopLevelItem(top)
            for gi, g in enumerate(T.groups_for(tpl, role), 1):
                gi_item = QTreeWidgetItem(["%d  %s" % (gi, g["name"]), ", ".join(sorted(g["cats"])), g["tier"] or ""])
                top.addChild(gi_item)
                for ti, t in enumerate(g["types"], 1):
                    gi_item.addChild(QTreeWidgetItem(["%d  %s" % (ti, t), "", ""]))
            top.setExpanded(role in ("Device", "Line"))
        if not self.name.text() or self.name.property("auto"):
            self.name.setText(tpl["label"] + " network")
            self.name.setProperty("auto", True)
        self._fix_ext()

    def _fmt(self):
        return next(f for f in S.FORMATS if f.key == self.fmt.currentData())

    def _fix_ext(self, *_a):
        p = self.path.text().strip()
        if self._fmt().key == "postgis":
            if p and not S.is_pg(p):
                self.path.clear()
            return
        if not p or S.is_pg(p):
            if S.is_pg(p):
                self.path.clear()
            return
        base = p.rstrip("/\\")
        for f in S.FORMATS:
            if f.ext and base.lower().endswith(f.ext):
                base = base[: -len(f.ext)]
        self.path.setText(base + self._fmt().ext)

    def _browse(self):
        fmt = self._fmt()
        if fmt.key == "postgis":
            dlg = PgDialog(self, self.path.text())
            if dlg.exec():
                self.path.setText(dlg.connection())
            return
        start = os.path.join(os.path.expanduser("~"), (self.tpl() or {}).get("key", "network") + fmt.ext)
        if fmt.directory:
            folder = QFileDialog.getExistingDirectory(self, "Folder for the new .gdb", os.path.dirname(start))
            if folder:
                self.path.setText(os.path.join(folder, os.path.basename(start)))
        else:
            path, _f = QFileDialog.getSaveFileName(self, "New network file", start, "*%s" % fmt.ext)
            if path:
                self.path.setText(path)
                self._fix_ext()

    def _create(self):
        tpl = self.tpl()
        path = self.path.text().strip()
        if not tpl or not path:
            QMessageBox.warning(self, "New network", "Choose a network type and a file.")
            return
        crs = self.crs.crs()
        if not crs.isValid():
            QMessageBox.warning(self, "New network", "Choose a coordinate system.")
            return
        if crs.isGeographic() and QMessageBox.question(
                self, "New network", "The coordinate system is in degrees, so the tolerance is in degrees too.\n"
                "A projected system in metres is recommended. Continue anyway?") != QMessageBox.StandardButton.Yes:
            return
        if self.add_domain.isChecked():
            base = S.split_domain(path)[0]
            if not (os.path.exists(base) or S.is_pg(base)):
                QMessageBox.warning(self, "New network", "Choose the existing file the network is added to.")
                return
            path = "%s#%s" % (base, tpl["prefix"].lower())
        try:
            S.create_network(path, tpl["key"], authid=crs.authid(), wkt=crs.toWkt(), tolerance=self.tol.value(),
                             gap=self.gap.value(), include_structures=self.structures.isChecked(),
                             name=self.name.text().strip())
        except S.StoreError as e:
            QMessageBox.critical(self, "New network", str(e))
            return
        self.created_path = path
        self.accept()
