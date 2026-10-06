"""Demo network: a realistic sample of any network type, built in a few seconds for presentations.

A street grid with mains along the streets, fittings / manholes at the intersections, isolating
devices near the intersections (pressure networks), a source (or outfall), buildings along the
streets and their service connections. Gravity networks drain to the outfall with falling inverts."""
import random

from osgeo import ogr
from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

from . import services
from . import storage as S
from . import templates as T
from .qgis_io import class_layers
from .tools import generate_ids


def _pick(cfg, role, cat=None, name=None, tier=None, exclude=()):
    cls = cfg.classes.get(role)
    for ag, gname in cfg.groups(cls) if cls else []:
        cats = cfg.cats.get((cls, ag), set())
        if exclude and cats & set(exclude):
            continue
        if name and gname.lower() != name.lower():
            continue
        if cat and cat not in cats:
            continue
        if tier and cfg.tiers.get((cls, ag)) not in (None, tier):
            continue
        return ag
    return None


def create(path, key, authid="EPSG:32638", x0=500000.0, y0=2700000.0, blocks=4, block=100.0, seed=7):
    rnd = random.Random(seed)
    tpl = T.BY_KEY[key]
    S.create_network(path, key, authid=authid, tolerance=0.001, gap=0.5, name="Demo %s" % tpl["label"])
    cfg = S.load_config(path)
    L = class_layers(cfg)
    flow = cfg.flow
    size_field = cfg.settings.get("size_field") or tpl["size_field"][0]
    # groups
    if key == "electric":
        src = _pick(cfg, "Device", name="Transformer")
        main = _pick(cfg, "Line", name="LV underground")
        iso = _pick(cfg, "Device", name="Fuse")
    elif flow == "gravity":
        src = _pick(cfg, "Device", name="Outfall") or _pick(cfg, "Device", "source")
        main = _pick(cfg, "Line", "main")
        iso = None
    else:
        src = _pick(cfg, "Device", "source")
        main = _pick(cfg, "Line", "main")
        iso = _pick(cfg, "Device", "isolating", exclude=("source",)) if flow == "source" else None
    junc = _pick(cfg, "Junction", exclude=("tap",))
    signal = _pick(cfg, "Device", name="Traffic signal") if key == "roads" else None
    n = blocks

    def node(i, j):
        return QgsPointXY(x0 + i * block, y0 + j * block)

    def dist(i, j):
        return (i + j) * block

    def add(role, geom, **attrs):
        lyr = L[role]
        f = QgsFeature(lyr.fields())
        f.setAttributes([None] * len(lyr.fields()))
        attrs.setdefault("lifecyclestatus", 3)
        attrs.setdefault("installdate", "%d-01-01" % rnd.randint(1975, 2022))
        for k, v in attrs.items():
            i = lyr.fields().indexFromName(k)
            if i >= 0:
                f.setAttribute(i, v)
        f.setGeometry(geom)
        lyr.dataProvider().addFeatures([f])

    segs = []
    for j in range(n + 1):
        for i in range(n):
            segs.append(((i, j), (i + 1, j)))
    for i in range(n + 1):
        for j in range(n):
            segs.append(((i, j), (i, j + 1)))
    for a, b in segs:
        if flow == "gravity" and dist(*a) < dist(*b):
            a, b = b, a                       # digitize downhill, toward the outfall at (0, 0)
        pa, pb = node(*a), node(*b)
        coords = [pa]
        if iso:
            t = 10.0 / block
            coords.append(QgsPointXY(pa.x() + (pb.x() - pa.x()) * t, pa.y() + (pb.y() - pa.y()) * t))
        coords.append(pb)
        far = max(dist(*a), dist(*b))
        size = {"diameter": 300 if far <= block * 2 else 200 if far <= block * 5 else 150,
                "voltage_kv": 0.4, "fiber_count": 96 if far <= block * 3 else 48, "pairs": 100,
                "lanes": 2, "size": 1}.get(size_field, 1)
        attrs = {"assetgroup": main, "assettype": 1, size_field: size, "condition": rnd.randint(1, 5),
                 "measuredlength": round(block, 3)}
        if flow == "gravity":
            attrs.update({"us_invert": round(95.0 + dist(*a) * 0.004, 3), "ds_invert": round(95.0 + dist(*b) * 0.004, 3),
                          "flowdirection": 1})
        if flow == "undirected":
            attrs.update({"oneway": 0, "roadname": "Street %d" % (len(segs) % 7 + 1), "speed": 50})
        add("Line", QgsGeometry.fromPolylineXY(coords), **attrs)
        if iso:
            add("Device", QgsGeometry.fromPointXY(coords[1]), assetgroup=iso, assettype=1, operatingstatus=1)
    for i in range(n + 1):
        for j in range(n + 1):
            if (i, j) == (0, 0) and src:
                add("Device", QgsGeometry.fromPointXY(node(0, 0)), assetgroup=src, assettype=1, operatingstatus=1,
                    assetid="SRC-001")
            elif signal and (i + j) % 2 == 0:
                add("Device", QgsGeometry.fromPointXY(node(i, j)), assetgroup=signal, assettype=1, operatingstatus=1)
            elif junc:
                attrs = {"assetgroup": junc, "assettype": 1}
                if flow == "gravity":
                    attrs["elevation"] = round(95.0 + dist(i, j) * 0.004 + 2.5, 3)
                else:
                    attrs["elevation"] = round(100.0 + rnd.uniform(-2, 2), 2)
                add("Junction", QgsGeometry.fromPointXY(node(i, j)), **attrs)
    # buildings along the horizontal streets
    ds = S.open_ds(path, update=True)
    try:
        ref = ds.GetLayerByName(cfg.classes["Line"])
        S._make_table(ds, "demo_buildings", [("parcel_id", ogr.OFTString, 20)], ogr.wkbPolygon,
                      ref.GetSpatialRef(), S.detect_format(path))
    finally:
        ds = None
    bl = QgsVectorLayer(S.uri(path, "demo_buildings"), "Demo buildings", "ogr")
    feats, k = [], 0
    for j in range(n):
        for i in range(n):
            for b in range(3):
                k += 1
                x = x0 + i * block + 18 + b * 28
                y = y0 + j * block + 14
                f = QgsFeature(bl.fields())
                f.setAttributes([None] * len(bl.fields()))
                f.setAttribute("parcel_id", "P-%04d" % k)
                f.setGeometry(QgsGeometry.fromWkt("POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))" % (
                    x, y, x + 16, y, x + 16, y + 12, x, y + 12, x, y)))
                feats.append(f)
    bl.dataProvider().addFeatures(feats)
    svc = tpl.get("service", {})
    line_ag = _pick(cfg, "Line", name=svc.get("line")) or _pick(cfg, "Line", "service")
    tap_ag = _pick(cfg, "Junction", name=svc.get("tap")) or _pick(cfg, "Junction", "tap")
    end_ag = _pick(cfg, "Device", name=svc.get("end")) or _pick(cfg, "Device", "customer")
    report = {}
    if line_ag:
        report = services.run(cfg, services.ServiceOptions(
            customer_layer=bl, id_field="parcel_id", main_groups=[main], max_distance=40, min_length=0.5,
            clearance=2.0, reuse_tap=0.5, service_group=(line_ag, 1), tap_group=(tap_ag, 1) if tap_ag else None,
            end_group=(end_ag, 1) if end_ag else None))
    cfg = S.load_config(path)
    from .qgis_io import clear_cache
    clear_cache(path)
    pattern = "%s-{GROUP}-{N}" % "".join(c for c in tpl["prefix"].upper() if c.isalpha())[:3]
    for role in ("Device", "Line", "Junction"):
        if role in cfg.classes:
            generate_ids(cfg, role, pattern, only_empty=True, digits=5)
    return {"segments": len(segs), "buildings": len(feats), "services": report.get("created", 0)}
