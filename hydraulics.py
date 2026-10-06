"""Export the network to hydraulic modelling software.

EPANET (.inp)  water / district cooling: nodes -> junctions, sources -> reservoirs or tanks,
               line sub-edges -> pipes; pipes next to a closed isolating device are CLOSED;
               every customer adds a base demand to its node.
SWMM (.inp)    sewer / storm: nodes -> junctions, sources (outfalls, plants) -> outfalls,
               line sub-edges -> conduits in the flow direction, circular cross sections.
The files open directly in EPANET / SWMM (or QGIS plugins such as QGISRed / Generic SWMM).
"""
from .qgis_io import _num, _val, class_layers


def _node_values(cfg, net, field):
    """{node: value} of `field` from the junctions / devices sitting on each node."""
    layers = class_layers(cfg)
    val = {}
    for role in ("Junction", "Device"):
        lyr = layers.get(role)
        if lyr is None or lyr.fields().indexFromName(field) < 0:
            continue
        cls = cfg.classes[role]
        values = {f.id(): _num(_val(f, field)) for f in lyr.getFeatures()}
        for pi, p in enumerate(net.points):
            if p["key"][0] == cls and values.get(p["key"][1]) is not None:
                val.setdefault(net.point_node[pi], values[p["key"][1]])
    return val


def _line_values(cfg, field):
    lyr = class_layers(cfg).get("Line")
    if lyr is None or lyr.fields().indexFromName(field) < 0:
        return {}
    cls = cfg.classes["Line"]
    return {(cls, f.id()): _num(_val(f, field)) for f in lyr.getFeatures()}


def _used_nodes(net):
    used = set()
    for e in net.edges:
        used.add(e["a"])
        used.add(e["b"])
    return used


def export_epanet(cfg, net, path, default_diameter=100.0, roughness=130.0, demand=0.01, source_head=50.0,
                  only_fed=True):
    """only_fed: leave out the parts no source can reach (EPANET cannot solve them: error 110)."""
    size_field = cfg.settings.get("size_field") or cfg.tpl["size_field"][0]
    diam = _line_values(cfg, size_field)
    elev = _node_values(cfg, net, "elevation")
    used = _used_nodes(net)
    keep_edges = None
    if only_fed:
        reach_n, reach_e = net._bfs(net.source_nodes(), ())
        keep_edges = reach_e
        used = used & reach_n
    sources, tanks, closed_nodes = {}, set(), net.closed_nodes()
    demand_at = {}
    for pi, p in enumerate(net.points):
        n = net.point_node[pi]
        cats = net.point_cats(pi)
        if "source" in cats:
            sources[n] = p
            if "tank" in cfg.label(p["key"][0], p["ag"]).lower() or "tank" in \
                    cfg.type_label(p["key"][0], p["ag"], p["at"]).lower():
                tanks.add(n)
        if "customer" in cats:
            demand_at[n] = demand_at.get(n, 0.0) + demand
    out = ["[TITLE]", "%s - exported by Network Studio" % cfg.settings.get("name", ""), "", "[JUNCTIONS]",
           ";ID\tElev\tDemand"]
    for n in sorted(used):
        if n in sources:
            continue
        out.append("N%d\t%.3f\t%.5f" % (n, elev.get(n, 0.0), demand_at.get(n, 0.0)))
    out += ["", "[RESERVOIRS]", ";ID\tHead"]
    for n in sorted(sources):
        if n not in tanks:
            out.append("N%d\t%.3f" % (n, elev.get(n, 0.0) + source_head))
    out += ["", "[TANKS]", ";ID\tElev\tInitLvl\tMinLvl\tMaxLvl\tDiam\tMinVol"]
    for n in sorted(tanks):
        out.append("N%d\t%.3f\t5\t0\t10\t20\t0" % (n, elev.get(n, 0.0)))
    out += ["", "[PIPES]", ";ID\tNode1\tNode2\tLength\tDiameter\tRoughness\tMinorLoss\tStatus"]
    for eid, e in enumerate(net.edges):
        if e["a"] == e["b"] or (keep_edges is not None and eid not in keep_edges):
            continue
        key = net.edge_key(eid)
        d = diam.get(key) if key else None
        status = "Closed" if (e["a"] in closed_nodes or e["b"] in closed_nodes) else "Open"
        out.append("P%d\tN%d\tN%d\t%.3f\t%.1f\t%.1f\t0\t%s" % (
            eid, e["a"], e["b"], max(e["length"], 0.1), d or default_diameter, roughness, status))
    out += ["", "[OPTIONS]", "Units\tLPS", "Headloss\tH-W", "", "[REPORT]", "Status\tNo", "Summary\tNo",
            "Nodes\tAll", "Links\tAll", "", "[COORDINATES]", ";Node\tX\tY"]
    for n in sorted(used):
        x, y = net.node_xy[n]
        out.append("N%d\t%.3f\t%.3f" % (n, x, y))
    out += ["", "[END]", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    left_out = 0 if keep_edges is None else sum(1 for e in net.edges if e["a"] != e["b"]) - len(
        [x for x in keep_edges if net.edges[x]["a"] != net.edges[x]["b"]])
    return {"junctions": len(used - set(sources)), "sources": len(set(sources) & used),
            "pipes": len(net.edges) - left_out, "pipes left out (no source)": left_out}


def export_swmm(cfg, net, path, default_diameter=300.0, roughness=0.013, max_depth=3.0):
    size_field = cfg.settings.get("size_field") or cfg.tpl["size_field"][0]
    diam = _line_values(cfg, size_field)
    us_inv, ds_inv = _line_values(cfg, "us_invert"), _line_values(cfg, "ds_invert")
    elev = _node_values(cfg, net, "elevation")
    used = _used_nodes(net)
    invert = {}
    for eid, e in enumerate(net.edges):
        key = net.edge_key(eid)
        if key is None:
            continue
        d = net._directed(eid) or (e["a"], e["b"])
        for n, v in ((d[0], us_inv.get(key)), (d[1], ds_inv.get(key))):
            if v is not None:
                invert[n] = min(invert.get(n, v), v)
    outfalls = {net.point_node[pi] for pi in range(len(net.points)) if "source" in net.point_cats(pi)}
    out = ["[TITLE]", "%s - exported by Network Studio" % cfg.settings.get("name", ""), "",
           "[OPTIONS]", "FLOW_UNITS\tLPS", "INFILTRATION\tHORTON", "FLOW_ROUTING\tDYNWAVE", "",
           "[JUNCTIONS]", ";Name\tElevation\tMaxDepth\tInitDepth\tSurDepth\tAponded"]
    for n in sorted(used - outfalls):
        out.append("N%d\t%.3f\t%.2f\t0\t0\t0" % (n, invert.get(n, elev.get(n, 0.0)), max_depth))
    out += ["", "[OUTFALLS]", ";Name\tElevation\tType\tStage\tGated"]
    for n in sorted(outfalls & used):
        out.append("N%d\t%.3f\tFREE\t\tNO" % (n, invert.get(n, elev.get(n, 0.0))))
    out += ["", "[CONDUITS]", ";Name\tFrom\tTo\tLength\tRoughness\tInOffset\tOutOffset"]
    xs = ["", "[XSECTIONS]", ";Link\tShape\tGeom1\tGeom2\tGeom3\tGeom4\tBarrels"]
    for eid, e in enumerate(net.edges):
        if e["a"] == e["b"]:
            continue
        key = net.edge_key(eid)
        f, t = net._directed(eid) or (e["a"], e["b"])
        out.append("C%d\tN%d\tN%d\t%.3f\t%.4f\t0\t0" % (eid, f, t, max(e["length"], 0.1), roughness))
        d = (diam.get(key) if key else None) or default_diameter
        xs.append("C%d\tCIRCULAR\t%.3f\t0\t0\t0\t1" % (eid, d / 1000.0))
    out += xs
    out += ["", "[COORDINATES]", ";Node\tX\tY"]
    for n in sorted(used):
        x, y = net.node_xy[n]
        out.append("N%d\t%.3f\t%.3f" % (n, x, y))
    out += ["", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    return {"junctions": len(used - outfalls), "outfalls": len(outfalls & used), "conduits": len(net.edges)}


# ---------------------------------------------------------------------- results back on the map
def parse_report(path):
    """Node and link results from an EPANET report (.rpt) written with Nodes All / Links All.

    Returns ({node id: {Demand, Head, Pressure}}, {link id: {Flow, Velocity, Headloss}}) of the
    last time period in the report."""
    nodes, links, mode, cols = {}, {}, None, []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            low = line.lower()
            if low.startswith("node results"):
                mode, cols = "node", []
                continue
            if low.startswith("link results"):
                mode, cols = "link", []
                continue
            if not line or line.startswith("-"):
                continue
            if mode and not cols:
                words = line.split()
                if mode == "node" and "demand" in low or mode == "link" and "flow" in low:
                    cols = [w.capitalize() for w in words]
                continue
            if low.startswith(("node ", "link ")) and len(line.split()) <= len(cols) + 1 and not \
                    any(ch.isdigit() for ch in line.split()[1] if line.split()[1:]):
                continue          # units line
            parts = line.split()
            if len(parts) < len(cols) + 1:
                continue
            try:
                vals = {c: float(v) for c, v in zip(cols, parts[1:len(cols) + 1])}
            except ValueError:
                continue
            (nodes if mode == "node" else links)[parts[0]] = vals
    return nodes, links


def results_layers(cfg, net, rpt_path):
    """Pressures (points) and velocities / flows (lines) as styled memory layers."""
    from qgis.core import (QgsFeature, QgsGeometry, QgsGraduatedSymbolRenderer, QgsPointXY, QgsProject,
                           QgsVectorLayer, QgsClassificationQuantile)
    nodes, links = parse_report(rpt_path)
    if not nodes and not links:
        raise ValueError("No results in the report: in EPANET use Report Options with Nodes All and Links All "
                         "(the exported .inp already asks for them), then run and save the report (.rpt).")
    from .diagrams import cfg_crs
    crs = cfg_crs(cfg)
    pl = QgsVectorLayer("Point?crs=%s&field=node:string&field=pressure:double&field=head:double&field=demand:double"
                        % crs, "EPANET pressures", "memory")
    ll = QgsVectorLayer("LineString?crs=%s&field=link:string&field=class_name:string&field=fid:long"
                        "&field=flow:double&field=velocity:double&field=headloss:double" % crs, "EPANET velocities", "memory")
    pf, lf = [], []
    for nid, v in nodes.items():
        if not nid.startswith("N") or not nid[1:].isdigit() or int(nid[1:]) >= len(net.node_xy):
            continue
        x, y = net.node_xy[int(nid[1:])]
        f = QgsFeature(pl.fields())
        f.setAttributes([nid, v.get("Pressure"), v.get("Head"), v.get("Demand")])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        pf.append(f)
    for lid, v in links.items():
        if not lid.startswith("P") or not lid[1:].isdigit() or int(lid[1:]) >= len(net.edges):
            continue
        e = net.edges[int(lid[1:])]
        key = net.edge_key(int(lid[1:]))
        if key is None:
            continue
        coords = net.lines[e["line"]]["parts"][e["part"]][e["v0"]:e["v1"] + 1]
        f = QgsFeature(ll.fields())
        f.setAttributes([lid, key[0], key[1], v.get("Flow"), v.get("Velocity"), v.get("Headloss")])
        f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in coords]))
        lf.append(f)
    pl.dataProvider().addFeatures(pf)
    ll.dataProvider().addFeatures(lf)
    for lyr, field in ((pl, "pressure"), (ll, "velocity")):
        if lyr.featureCount():
            r = QgsGraduatedSymbolRenderer(field)
            r.setClassificationMethod(QgsClassificationQuantile())
            r.updateClasses(lyr, 5)
            from qgis.core import QgsStyle
            ramp = QgsStyle.defaultStyle().colorRamp("Spectral")
            if ramp:
                ramp.invert() if field == "velocity" else None
                r.updateColorRamp(ramp)
            lyr.setRenderer(r)
            QgsProject.instance().addMapLayer(lyr)
    return {"nodes": len(pf), "links": len(lf)}
