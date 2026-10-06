"""Web publishing: prepare the current QGIS project for QGIS Server (WMS / WFS / WFS-T) and Lizmap.

The project is saved as .qgs with relative paths, the network layers are published as WMS and
WFS (optionally editable through WFS-T), with a title, abstract and extent. With PostGIS the server
reads the database directly; with files, copy the data next to the project on the server.
For Lizmap, open the saved project in QGIS and run the Lizmap plugin once to create its .cfg."""
import os

from qgis.core import Qgis, QgsProject

from . import storage as S
from .qgis_io import project_layer


def publish(cfg, folder, title, abstract="", editable=False, include_work_orders=True):
    prj = QgsProject.instance()
    names = list(cfg.classes.values()) + (["un_work_orders"] if include_work_orders else [])
    layers = [l for l in (project_layer(cfg.path, n) for n in names) if l is not None]
    if not layers:
        raise ValueError("Add the network layers to the map first.")
    ids = [l.id() for l in layers]
    prj.setTitle(title)
    prj.writeEntry("WMSServiceCapabilities", "/", True)
    prj.writeEntry("WMSServiceTitle", "/", title)
    prj.writeEntry("WMSServiceAbstract", "/", abstract)
    prj.writeEntry("WMSAddWktGeometry", "/", True)
    prj.writeEntry("WMSFeatureInfoUseAttributeFormSettings", "/", True)
    ext = None
    for l in layers:
        e = l.extent()
        if not e.isEmpty():
            ext = e if ext is None else (ext.combineExtentWith(e) or ext)
    if ext is not None:
        prj.writeEntry("WMSExtent", "/", [str(ext.xMinimum()), str(ext.yMinimum()), str(ext.xMaximum()),
                                          str(ext.yMaximum())])
    prj.writeEntry("WFSLayers", "/", ids)
    for i in ids:
        prj.writeEntry("WFSLayersPrecision", "/" + i, 3)
    if editable:
        for key in ("WFSTLayers/Update", "WFSTLayers/Insert", "WFSTLayers/Delete"):
            prj.writeEntry(key.split("/")[0], "/" + key.split("/")[1], ids)
    try:
        prj.setFilePathStorage(Qgis.FilePathType.Relative)
    except AttributeError:
        prj.writeEntryBool("Paths", "/Absolute", False)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "".join(c if c.isalnum() else "_" for c in title).strip("_") + ".qgs")
    if not prj.write(path):
        raise ValueError("Could not save the project: %s" % prj.error())
    note = ("The data is in PostGIS: the server reads it directly (check the server can reach the database)."
            if S.is_pg(cfg.path) else
            "Copy the network file next to the project on the server (the path is relative).")
    return path, len(ids), note
