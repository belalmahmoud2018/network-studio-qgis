"""Web publishing: prepare the current QGIS project for QGIS Server (WMS / WFS
/ WFS-T) and Lizmap.

The project is saved as .qgs with relative paths, the network layers are
published as WMS and
WFS (optionally editable through WFS-T), with a title, abstract and extent.
With PostGIS the server
reads the database directly; with files, copy the data next to the project on
the server.
For Lizmap, open the saved project in QGIS and run the Lizmap plugin once to
create its .cfg.
"""

import os

from qgis.core import Qgis, QgsProject

from . import storage as S
from .qgis_io import project_layer


def publish(
    cfg,
    folder,
    title,
    abstract="",
    editable=False,
    include_work_orders=True,
    edit_ids=None,
    capabilities=None,
    lizmap=False,
):
    """editable: WFS-T on the layers `edit_ids` (all published layers when
    None) with `capabilities` (create / attributes / geometry / delete);
    lizmap: also write the Lizmap configuration."""
    prj = QgsProject.instance()
    names = list(cfg.classes.values()) + (
        ["un_work_orders"] if include_work_orders else []
    )
    layers = [
        x
        for x in (project_layer(cfg.path, n) for n in names)
        if x is not None
    ]
    if not layers:
        raise ValueError("Add the network layers to the map first.")
    ids = [l.id() for l in layers]  # noqa: E741
    prj.setTitle(title)
    prj.writeEntry("WMSServiceCapabilities", "/", True)
    prj.writeEntry("WMSServiceTitle", "/", title)
    prj.writeEntry("WMSServiceAbstract", "/", abstract)
    prj.writeEntry("WMSAddWktGeometry", "/", True)
    prj.writeEntry("WMSFeatureInfoUseAttributeFormSettings", "/", True)
    ext = None
    for l in layers:  # noqa: E741
        e = l.extent()
        if not e.isEmpty():
            ext = e if ext is None else (ext.combineExtentWith(e) or ext)
    if ext is not None:
        prj.writeEntry(
            "WMSExtent",
            "/",
            [
                str(ext.xMinimum()),
                str(ext.yMinimum()),
                str(ext.xMaximum()),
                str(ext.yMaximum()),
            ],
        )
    prj.writeEntry("WFSLayers", "/", ids)
    for i in ids:
        prj.writeEntry("WFSLayersPrecision", "/" + i, 3)
    capabilities = capabilities or {}
    edit = [i for i in ids if edit_ids is None or i in edit_ids]
    if not editable:
        edit = []
    upd = (
        edit
        if (
            capabilities.get("attributes", True)
            or capabilities.get("geometry", True)
        )
        else []
    )
    prj.writeEntry("WFSTLayers", "/Update", upd)
    prj.writeEntry(
        "WFSTLayers",
        "/Insert",
        edit if capabilities.get("create", True) else [],
    )
    prj.writeEntry(
        "WFSTLayers",
        "/Delete",
        edit if capabilities.get("delete", True) else [],
    )
    try:
        prj.setFilePathStorage(Qgis.FilePathType.Relative)
    except AttributeError:
        prj.writeEntryBool("Paths", "/Absolute", False)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(
        folder,
        "".join(c if c.isalnum() else "_" for c in title).strip("_") + ".qgs",
    )
    if not prj.write(path):
        raise ValueError("Could not save the project: %s" % prj.error())
    extra = []
    if S.is_pg(cfg.path):
        from .webedit import use_postgres_provider

        n = use_postgres_provider(path)
        if n:
            extra.append(
                "%d PostGIS layer(s) use the PostgreSQL provider in the web"
                " project." % n
            )
    if lizmap:
        from .webedit import lizmap_config

        cfg_path = lizmap_config(path, layers, set(edit), capabilities, title)
        extra.append(
            "Lizmap configuration: %s (%d editable layer(s)). Open the"
            " project once with the Lizmap plugin to review it."
            % (os.path.basename(cfg_path), len(edit))
        )
    note = (
        "The data is in PostGIS: the server reads it directly (check the"
        " server can reach the database)."
        if S.is_pg(cfg.path)
        else (
            "Copy the network file next to the project on the server (the path"
            " is relative)."
        )
    )
    if editable and not S.is_pg(cfg.path):
        extra.append(
            "Several people editing on the web at the same time need"
            " PostGIS; with a file keep web editing to one person."
        )
    return path, len(ids), " ".join([note] + extra)
