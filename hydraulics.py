"""Export the network to hydraulic modelling software.

EPANET (.inp)  water / district cooling: nodes -> junctions, sources ->
reservoirs or tanks,
               line sub-edges -> pipes; pipes next to a closed isolating device
               are CLOSED;
               every customer adds a base demand to its node.
SWMM (.inp)    sewer / storm: nodes -> junctions, sources (outfalls, plants) ->
outfalls,
               line sub-edges -> conduits in the flow direction, circular cross
               sections.
The files open directly in EPANET / SWMM (or QGIS plugins such as QGISRed /
Generic SWMM).
"""

from .hydro_solver import from_pressure, pump_curve
from .qgis_io import _num, _val, class_layers


def _node_values(cfg, net, field):
    """{node: value} of `field` from the junctions / devices sitting on each
    node."""
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


def _point_values(cfg, role, field):
    """{(class, fid): value} of a numeric field of one class."""
    lyr = class_layers(cfg).get(role)
    if lyr is None or lyr.fields().indexFromName(field) < 0:
        return {}
    cls = cfg.classes[role]
    return {
        (cls, f.id()): _num(_val(f, field))
        for f in lyr.getFeatures()
        if _num(_val(f, field)) is not None
    }


def _labels(cfg, p):
    cls = p["key"][0]
    return (
        cfg.label(cls, p["ag"]).lower()
        + " / "
        + cfg.type_label(cls, p["ag"], p["at"]).lower()
    )


def model_law(cfg, gas_mode="mp"):
    if cfg.settings.get("network_type") == "gas":
        return "gas_lp" if gas_mode == "lp" else "gas_mp"
    return "hw"


def build_model(
    cfg,
    net,
    default_diameter=100.0,
    roughness=130.0,
    demand=0.01,
    source_head=50.0,
    only_fed=True,
    pump_head=None,
    pump_flow=50.0,
    pump_suction=0.0,
    prv_setting=30.0,
    tank_diameter=20.0,
    tank_level=5.0,
    tank_max=10.0,
    gas_mode="mp",
    source_pressure=4.0,
    gas_eps=0.05,
    gas_density=0.73,
):
    """Hydraulic model of a pressure network (see hydro_solver).

    Water / district cooling (Hazen-Williams): diameters from the size field
    of the lines, C from 'roughness', elevations from the junctions, demands
    from the 'demand' field of the customer devices (otherwise `demand` L/s
    per customer). Sources are fixed heads `source_head` m above ground;
    tanks keep a level (24 h simulation); pumps get a reservoir on their
    suction side and a curve through (pump_flow L/s, pump_head m) or the
    device fields pump_flow / pump_head; pressure reducing valves (devices
    named so) split their node and hold 'setting' m (or prv_setting)
    downstream. Gas (Darcy, Swamee-Jain): sources hold `source_pressure`
    (bar for medium pressure, mbar for low pressure), demands in m3/h,
    roughness in mm. Pipes next to a closed isolating device are closed.
    only_fed leaves out the parts no source can reach."""
    law = model_law(cfg, gas_mode)
    gas = law != "hw"
    size_field = cfg.settings.get("size_field") or cfg.tpl["size_field"][0]
    diam = _line_values(cfg, size_field)
    rough = _line_values(cfg, "roughness")
    elev = {} if gas else _node_values(cfg, net, "elevation")
    dev_demand = _point_values(cfg, "Device", "demand")
    dev_setting = _point_values(cfg, "Device", "setting")
    dev_phead = _point_values(cfg, "Device", "pump_head")
    dev_pflow = _point_values(cfg, "Device", "pump_flow")
    used = _used_nodes(net)
    keep_edges = None
    if only_fed:
        reach_n, reach_e = net._bfs(net.source_nodes(), ())
        keep_edges = reach_e
        used = used & reach_n
    model = {
        "law": law,
        "gas": {
            "rho_n": gas_density,
            "mu": 1.1e-5,
            "p_n": 101325.0,
            "t_n": 288.15,
            "t": 288.15,
            "z": 1.0,
            "p_atm": 101325.0,
        },
    }
    fixed, tanks, tank_info = {}, {}, {}
    closed_nodes = net.closed_nodes()
    demand_at, customers, pumps, prvs = {}, {}, [], []
    for pi, p in enumerate(net.points):
        n = net.point_node[pi]
        cats = net.point_cats(pi)
        lab = _labels(cfg, p)
        key = tuple(p["key"])
        if "source" in cats and n in used:
            ground = elev.get(n, 0.0)
            if gas:
                fixed[n] = None  # set below with the source pressure
            elif "pump" in lab:
                pumps.append((n, key))
            else:
                fixed[n] = ground + source_head
                if "tank" in lab:
                    lvl = min(tank_level, tank_max)
                    tanks[n] = (ground + source_head - lvl, lvl)
                    tank_info[n] = {
                        "diameter": tank_diameter,
                        "min": 0.0,
                        "max": tank_max,
                    }
        elif (
            not gas
            and n in used
            and ("pressure reducing" in lab or "prv" in lab.split())
        ):
            prvs.append((n, key))
        if "customer" in cats:
            q = dev_demand.get(key, demand)
            demand_at[n] = demand_at.get(n, 0.0) + (q or 0.0)
            customers.setdefault(n, []).append(key)
    model["fixed"] = fixed
    if gas:
        for n in fixed:
            fixed[n] = from_pressure(model, source_pressure)
    junctions = {
        n: (elev.get(n, 0.0), demand_at.get(n, 0.0))
        for n in used
        if n not in fixed
    }
    pipes = []
    for eid, e in enumerate(net.edges):
        if e["a"] == e["b"] or (
            keep_edges is not None and eid not in keep_edges
        ):
            continue
        key = net.edge_key(eid)
        d = diam.get(key) if key else None
        c = rough.get(key) if key else None
        pipes.append(
            {
                "id": eid,
                "a": e["a"],
                "b": e["b"],
                "length": max(e["length"], 0.1),
                "diameter": d if d and d > 0 else default_diameter,
                "c": c if (c and c > 0 and not gas) else roughness,
                "eps": c if (c and c > 0 and gas) else gas_eps,
                "open": not (e["a"] in closed_nodes or e["b"] in closed_nodes),
                "key": key,
            }
        )
    xy = {n: net.node_xy[n] for n in set(junctions) | set(fixed)}
    next_id = len(net.node_xy)
    next_link = len(net.edges)
    # pumps: suction reservoir -> pump -> network node
    for n, key in pumps:
        res_node = next_id
        next_id += 1
        fixed[res_node] = elev.get(n, 0.0) + pump_suction
        model.setdefault("fixed_elev", {})[res_node] = elev.get(n, 0.0)
        xy[res_node] = net.node_xy[n]
        hd = dev_phead.get(key) or pump_head or source_head
        qd = dev_pflow.get(key) or pump_flow
        h0, rp = pump_curve(hd, qd)
        pipes.append(
            {
                "id": next_link,
                "a": res_node,
                "b": n,
                "kind": "pump",
                "h0": h0,
                "rp": rp,
                "length": 1.0,
                "diameter": 300.0,
                "c": 130.0,
                "open": n not in closed_nodes,
                "key": key,
            }
        )
        next_link += 1
        junctions.setdefault(n, (elev.get(n, 0.0), demand_at.get(n, 0.0)))
    # pressure reducing valves: split the node, downstream pipes move to a
    # new node
    valves = []
    if prvs:
        dist = _hops(pipes, fixed)
        for n, key in prvs:
            if n not in junctions or n not in dist:
                continue
            down = [
                p
                for p in pipes
                if p.get("kind") != "pump"
                and n in (p["a"], p["b"])
                and dist.get(p["b"] if p["a"] == n else p["a"], -1) > dist[n]
            ]
            if not down or len(down) == sum(
                1 for p in pipes if n in (p["a"], p["b"])
            ):
                continue
            nb = next_id
            next_id += 1
            for p in down:
                if p["a"] == n:
                    p["a"] = nb
                else:
                    p["b"] = nb
            junctions[nb] = (elev.get(n, 0.0), 0.0)
            xy[nb] = net.node_xy[n]
            setting = dev_setting.get(key) or prv_setting
            valves.append(
                {
                    "a": n,
                    "b": nb,
                    "head": elev.get(n, 0.0) + setting,
                    "setting": setting,
                    "diameter": max(p["diameter"] for p in down),
                    "key": key,
                }
            )
    left_out = 0
    if keep_edges is not None:
        left_out = sum(1 for e in net.edges if e["a"] != e["b"]) - sum(
            1 for p in pipes if p.get("kind") != "pump"
        )
    model.update(
        {
            "junctions": junctions,
            "fixed": fixed,
            "tanks": tanks,
            "tank_info": tank_info,
            "fixed_elev": _fixed_elev(model, fixed, elev, len(net.node_xy)),
            "pipes": pipes,
            "valves": valves,
            "xy": xy,
            "customers": customers,
            "left_out": left_out,
            "pumps": len(pumps),
        }
    )
    return model


def _fixed_elev(model, fixed, elev, n_nodes):
    out = dict(model.get("fixed_elev", {}))
    for n in fixed:
        if n < n_nodes:
            out[n] = elev.get(n, 0.0)
    return out


def _hops(pipes, fixed):
    """Number of pipes between every node and the nearest source."""
    adj = {}
    for p in pipes:
        if p["open"]:
            adj.setdefault(p["a"], []).append(p["b"])
            adj.setdefault(p["b"], []).append(p["a"])
    dist = {n: 0 for n in fixed}
    frontier = list(fixed)
    while frontier:
        nxt = []
        for n in frontier:
            for m in adj.get(n, ()):
                if m not in dist:
                    dist[m] = dist[n] + 1
                    nxt.append(m)
        frontier = nxt
    return dist


def export_epanet(
    cfg,
    net,
    path,
    default_diameter=100.0,
    roughness=130.0,
    demand=0.01,
    source_head=50.0,
    only_fed=True,
    hours=0,
    pattern=None,
    **params
):
    """only_fed: leave out the parts no source can reach (EPANET cannot solve
    them: error 110). hours / pattern: a 24 h (extended period) model."""
    from .hydro_solver import write_inp

    model = build_model(
        cfg,
        net,
        default_diameter,
        roughness,
        demand,
        source_head,
        only_fed,
        **params
    )
    write_inp(
        model,
        path,
        "%s - exported by Network Studio" % cfg.settings.get("name", ""),
        hours,
        pattern,
    )
    return {
        "junctions": len(model["junctions"]),
        "sources": len(model["fixed"]),
        "pipes": sum(1 for p in model["pipes"] if p.get("kind") != "pump"),
        "pumps": model["pumps"],
        "pressure reducing valves": len(model["valves"]),
        "pipes left out (no source)": model["left_out"],
    }


def export_swmm(
    cfg, net, path, default_diameter=300.0, roughness=0.013, max_depth=3.0
):
    size_field = cfg.settings.get("size_field") or cfg.tpl["size_field"][0]
    diam = _line_values(cfg, size_field)
    us_inv, ds_inv = _line_values(cfg, "us_invert"), _line_values(
        cfg, "ds_invert"
    )
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
    outfalls = {
        net.point_node[pi]
        for pi in range(len(net.points))
        if "source" in net.point_cats(pi)
    }
    out = [
        "[TITLE]",
        "%s - exported by Network Studio" % cfg.settings.get("name", ""),
        "",
        "[OPTIONS]",
        "FLOW_UNITS\tLPS",
        "INFILTRATION\tHORTON",
        "FLOW_ROUTING\tDYNWAVE",
        "",
        "[JUNCTIONS]",
        ";Name\tElevation\tMaxDepth\tInitDepth\tSurDepth\tAponded",
    ]
    for n in sorted(used - outfalls):
        out.append(
            "N%d\t%.3f\t%.2f\t0\t0\t0"
            % (n, invert.get(n, elev.get(n, 0.0)), max_depth)
        )
    out += ["", "[OUTFALLS]", ";Name\tElevation\tType\tStage\tGated"]
    for n in sorted(outfalls & used):
        out.append(
            "N%d\t%.3f\tFREE\t\tNO" % (n, invert.get(n, elev.get(n, 0.0)))
        )
    out += [
        "",
        "[CONDUITS]",
        ";Name\tFrom\tTo\tLength\tRoughness\tInOffset\tOutOffset",
    ]
    xs = [
        "",
        "[XSECTIONS]",
        ";Link\tShape\tGeom1\tGeom2\tGeom3\tGeom4\tBarrels",
    ]
    for eid, e in enumerate(net.edges):
        if e["a"] == e["b"]:
            continue
        key = net.edge_key(eid)
        f, t = net._directed(eid) or (e["a"], e["b"])
        out.append(
            "C%d\tN%d\tN%d\t%.3f\t%.4f\t0\t0"
            % (eid, f, t, max(e["length"], 0.1), roughness)
        )
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
    return {
        "junctions": len(used - outfalls),
        "outfalls": len(outfalls & used),
        "conduits": len(net.edges),
    }


# ----------------------------------------------------------------------
# results back on the map
def parse_report(path):
    """Node and link results from an EPANET report (.rpt) written with Nodes
    All / Links All.

    Returns ({node id: {Demand, Head, Pressure}}, {link id: {Flow, Velocity,
    Headloss}}) of the
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
                if (
                    mode == "node"
                    and "demand" in low
                    or mode == "link"
                    and "flow" in low
                ):
                    cols = [w.capitalize() for w in words]
                continue
            if (
                low.startswith(("node ", "link "))
                and len(line.split()) <= len(cols) + 1
                and not any(
                    ch.isdigit() for ch in line.split()[1] if line.split()[1:]
                )
            ):
                continue  # units line
            parts = line.split()
            if len(parts) < len(cols) + 1:
                continue
            try:
                vals = {
                    c: float(v) for c, v in zip(cols, parts[1: len(cols) + 1])
                }
            except ValueError:
                continue
            (nodes if mode == "node" else links)[parts[0]] = vals
    return nodes, links


def results_layers(cfg, net, rpt_path):
    """Pressures (points) and velocities / flows (lines) as styled memory
    layers."""
    from qgis.core import (
        QgsFeature,
        QgsGeometry,
        QgsGraduatedSymbolRenderer,
        QgsPointXY,
        QgsProject,
        QgsVectorLayer,
        QgsClassificationQuantile,
    )

    nodes, links = parse_report(rpt_path)
    if not nodes and not links:
        raise ValueError(
            "No results in the report: in EPANET use Report Options with Nodes"
            " All and Links All (the exported .inp already asks for them),"
            " then run and save the report (.rpt)."
        )
    from .diagrams import cfg_crs

    crs = cfg_crs(cfg)
    pl = QgsVectorLayer(
        "Point?crs=%s&field=node:string&field=pressure:double&field=head:double&field=demand:double"  # noqa: E501
        % crs,
        "EPANET pressures",
        "memory",
    )
    ll = QgsVectorLayer(
        "LineString?crs=%s&field=link:string&field=class_name:string&field=fid:long"  # noqa
        "&field=flow:double&field=velocity:double&field=headloss:double" % crs,
        "EPANET velocities",
        "memory",
    )
    pf, lf = [], []
    for nid, v in nodes.items():
        if (
            not nid.startswith("N")
            or not nid[1:].isdigit()
            or int(nid[1:]) >= len(net.node_xy)
        ):
            continue
        x, y = net.node_xy[int(nid[1:])]
        f = QgsFeature(pl.fields())
        f.setAttributes(
            [nid, v.get("Pressure"), v.get("Head"), v.get("Demand")]
        )
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        pf.append(f)
    for lid, v in links.items():
        if (
            not lid.startswith("P")
            or not lid[1:].isdigit()
            or int(lid[1:]) >= len(net.edges)
        ):
            continue
        e = net.edges[int(lid[1:])]
        key = net.edge_key(int(lid[1:]))
        if key is None:
            continue
        coords = net.lines[e["line"]]["parts"][e["part"]][
            e["v0"]: e["v1"] + 1
        ]
        f = QgsFeature(ll.fields())
        f.setAttributes(
            [
                lid,
                key[0],
                key[1],
                v.get("Flow"),
                v.get("Velocity"),
                v.get("Headloss"),
            ]
        )
        f.setGeometry(
            QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in coords])
        )
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


# ----------------------------------------------------------------------
# hydraulic analysis inside QGIS (no EPANET needed)
STATUS_COLORS = {
    "Low": "#e24b4a",
    "High": "#7f77dd",
    "OK": "#1d9e75",
    "Too fast": "#e24b4a",
    "Slow": "#9c9a92",
    "Closed": "#444441",
    "Pass": "#1d9e75",  # nosec B105 - a status colour, not a password
    "Fail": "#e24b4a",
    "No source": "#9c9a92",
}


def _categorized(lyr, field, size=None):
    from qgis.core import (
        QgsCategorizedSymbolRenderer,
        QgsRendererCategory,
        QgsSymbol,
    )
    from qgis.PyQt.QtGui import QColor

    cats = []
    values = sorted({f[field] for f in lyr.getFeatures() if f[field]})
    for v in values:
        sym = QgsSymbol.defaultSymbol(lyr.geometryType())
        sym.setColor(QColor(STATUS_COLORS.get(v, "#888780")))
        if size is not None:
            try:
                sym.setWidth(size)
            except AttributeError:
                sym.setSize(size * 4)
        cats.append(QgsRendererCategory(v, sym, v))
    lyr.setRenderer(QgsCategorizedSymbolRenderer(field, cats))


def _memory(cfg, kind, name, fields):
    from qgis.core import QgsVectorLayer

    from .diagrams import cfg_crs

    spec = "&".join("field=%s:%s" % (n, t) for n, t in fields)
    return QgsVectorLayer(
        "%s?crs=%s&%s" % (kind, cfg_crs(cfg), spec), name, "memory"
    )


def _edge_geometry(net, eid):
    from qgis.core import QgsGeometry, QgsPointXY

    e = net.edges[eid]
    if e["line"] is None:
        coords = [net.node_xy[e["a"]], net.node_xy[e["b"]]]
    else:
        coords = net.lines[e["line"]]["parts"][e["part"]][
            e["v0"]: e["v1"] + 1
        ]
    return QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in coords])


def _point_geometry(net, n, model=None):
    from qgis.core import QgsGeometry, QgsPointXY

    if model is not None and n in model.get("xy", {}):
        x, y = model["xy"][n]
    else:
        x, y = net.node_xy[n]
    return QgsGeometry.fromPointXY(QgsPointXY(x, y))


def analyse(
    cfg,
    net,
    params,
    min_pressure=20.0,
    max_pressure=70.0,
    max_velocity=2.0,
    min_velocity=0.1,
    demand_factor=1.0,
    add_layers=True,
    title="",
):
    """Solve the network and classify pressures and velocities.

    params: build_model keyword arguments. Returns (summary, model, result,
    layers)."""
    from qgis.core import QgsFeature, QgsProject

    from .hydro_solver import solve

    model = build_model(cfg, net, **params)
    res = solve(model, demand_factor=demand_factor)
    title = title or "Hydraulics"
    pl = _memory(
        cfg,
        "Point",
        "%s - pressures" % title,
        [
            ("node", "integer"),
            ("elevation", "double"),
            ("head", "double"),
            ("pressure", "double"),
            ("demand", "double"),
            ("customers", "integer"),
            ("status", "string"),
        ],
    )
    ll = _memory(
        cfg,
        "LineString",
        "%s - pipes" % title,
        [
            ("pipe", "integer"),
            ("class_name", "string"),
            ("fid", "long"),
            ("diameter", "double"),
            ("c", "double"),
            ("flow", "double"),
            ("velocity", "double"),
            ("headloss", "double"),
            ("status", "string"),
        ],
    )
    counts = {"Low": 0, "High": 0, "OK": 0}
    low_customers = 0
    feats = []
    for n, pr in res.pressure.items():
        if n in model["fixed"]:
            continue
        st = (
            "Low"
            if pr < min_pressure
            else ("High" if pr > max_pressure else "OK")
        )
        counts[st] += 1
        cust = len(model["customers"].get(n, ()))
        if st == "Low":
            low_customers += cust
        f = QgsFeature(pl.fields())
        f.setAttributes(
            [
                n,
                model["junctions"][n][0],
                round(res.head[n], 3),
                round(pr, 3),
                round(res.demand.get(n, 0.0), 4),
                cust,
                st,
            ]
        )
        f.setGeometry(_point_geometry(net, n, model))
        feats.append(f)
    for n in res.unfed:
        f = QgsFeature(pl.fields())
        f.setAttributes(
            [n, model["junctions"][n][0], None, None, None, 0, "No source"]
        )
        f.setGeometry(_point_geometry(net, n, model))
        feats.append(f)
    pl.dataProvider().addFeatures(feats)
    vcounts = {"Too fast": 0, "Slow": 0, "OK": 0, "Closed": 0}
    feats = []
    for k, p in enumerate(model["pipes"]):
        if p.get("kind") == "pump":
            continue
        v = res.velocity[k]
        if v is None:
            st = "Closed"
        elif abs(v) > max_velocity:
            st = "Too fast"
        elif abs(v) < min_velocity:
            st = "Slow"
        else:
            st = "OK"
        vcounts[st] += 1
        key = p["key"] or ("", None)
        f = QgsFeature(ll.fields())
        f.setAttributes(
            [
                p["id"],
                key[0],
                key[1],
                p["diameter"],
                p["c"],
                None if v is None else round(res.flow[k], 4),
                None if v is None else round(abs(v), 3),
                None if v is None else round(res.headloss[k], 3),
                st,
            ]
        )
        f.setGeometry(_edge_geometry(net, p["id"]))
        feats.append(f)
    ll.dataProvider().addFeatures(feats)
    _categorized(pl, "status", 0.9)
    _categorized(ll, "status", 0.8)
    if add_layers:
        QgsProject.instance().addMapLayer(ll)
        QgsProject.instance().addMapLayer(pl)
    summary = res.summary()
    summary.update(
        {
            "nodes below %g %s" % (min_pressure, res.units[0]): counts["Low"],
            "nodes above %g %s" % (max_pressure, res.units[0]): counts["High"],
            "customers with low pressure": low_customers,
            "pipes faster than %.2f m/s" % max_velocity: vcounts["Too fast"],
            "pipes slower than %.2f m/s" % min_velocity: vcounts["Slow"],
            "closed pipes": vcounts["Closed"],
            "demand factor": demand_factor,
        }
    )
    return summary, model, res, (pl, ll)


def hydrant_nodes(cfg, net, selected_only=False):
    """{node: (class, fid)} of the hydrants (device groups named
    'hydrant'), or of the selected devices."""
    lyr = class_layers(cfg).get("Device")
    cls = cfg.classes.get("Device")
    sel = set(lyr.selectedFeatureIds()) if lyr is not None else set()
    out = {}
    for pi, p in enumerate(net.points):
        if p["key"][0] != cls:
            continue
        if selected_only:
            if p["key"][1] not in sel:
                continue
        elif "hydrant" not in cfg.label(cls, p["ag"]).lower():
            continue
        out[net.point_node[pi]] = tuple(p["key"])
    return out


def fire_flow(
    cfg,
    net,
    params,
    test_flow=16.0,
    min_residual=14.0,
    selected_only=False,
    demand_factor=1.0,
    feedback=None,
    add_layer=True,
):
    """For every hydrant: static pressure, residual pressure while it
    delivers `test_flow` L/s on top of the demand, lowest pressure in the
    network during the test and the flow available at `min_residual` m."""
    from qgis.core import QgsFeature, QgsProject

    from .hydro_solver import available_fire_flow, solve

    model = build_model(cfg, net, **params)
    hyd = {
        n: k
        for n, k in hydrant_nodes(cfg, net, selected_only).items()
        if n in model["junctions"]
    }
    if not hyd:
        raise ValueError(
            "No hydrant reached by a source: select hydrants (Device layer)"
            " or name a device group 'Hydrant'."
        )
    base = solve(model, demand_factor=demand_factor)
    lyr = _memory(
        cfg,
        "Point",
        "Fire flow (%.0f L/s)" % test_flow,
        [
            ("class_name", "string"),
            ("fid", "long"),
            ("static_p", "double"),
            ("residual_p", "double"),
            ("min_system_p", "double"),
            ("available", "double"),
            ("status", "string"),
        ],
    )
    feats = []
    passed = 0
    for i, (n, key) in enumerate(sorted(hyd.items())):
        if feedback and feedback(i, len(hyd)):
            break
        r = solve(
            model, demand_factor=demand_factor, extra_demand={n: test_flow}
        )
        static = base.pressure.get(n)
        resid = r.pressure.get(n)
        others = [
            v
            for m, v in r.pressure.items()
            if m in model["junctions"] and m != n
        ]
        low = min(others) if others else None
        avail = available_fire_flow(static, resid, test_flow, min_residual)
        ok = resid is not None and resid >= min_residual
        passed += 1 if ok else 0
        f = QgsFeature(lyr.fields())
        f.setAttributes(
            [
                key[0],
                key[1],
                None if static is None else round(static, 2),
                None if resid is None else round(resid, 2),
                None if low is None else round(low, 2),
                None if avail is None else round(avail, 1),
                "Pass" if ok else "Fail",
            ]
        )
        f.setGeometry(_point_geometry(net, n, model))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    _categorized(lyr, "status", 1.6)
    if add_layer:
        QgsProject.instance().addMapLayer(lyr)
    return {
        "hydrants tested": len(feats),
        "pass": passed,
        "fail": len(feats) - passed,
        "test flow (L/s)": test_flow,
        "minimum residual pressure (m)": min_residual,
    }, lyr


def pipe_groups(cfg, model, group_by):
    """Group label of every model pipe: '' = one group, 'assetgroup',
    'assettype' (names) or any field of the lines."""
    lyr = class_layers(cfg).get("Line")
    cls = cfg.classes.get("Line")
    if not group_by:
        return ["All pipes" if p["key"] else None for p in model["pipes"]]
    vals = {}
    if lyr is not None and lyr.fields().indexFromName(group_by) >= 0:
        for f in lyr.getFeatures():
            v = _val(f, group_by)
            if group_by == "assettype":
                v = "%s / %s" % (
                    cfg.label(cls, _val(f, "assetgroup")),
                    cfg.type_label(cls, _val(f, "assetgroup"), v),
                )
            elif group_by == "assetgroup":
                v = cfg.label(cls, v)
            vals[(cls, f.id())] = "(empty)" if v in (None, "") else str(v)
    return [
        vals.get(tuple(p["key"])) if p["key"] else None for p in model["pipes"]
    ]


def observations_from_layer(
    cfg, net, model, layer, field, kind="pressure", max_distance=10.0
):
    """Field measurements (a point layer with a numeric field) snapped to the
    nearest model node (pressure / head) or pipe (flow)."""
    from .engine import seg_distance

    out, skipped = [], 0
    nodes = list(model["junctions"])
    for f in layer.getFeatures():
        v = _num(_val(f, field))
        if v is None or not f.hasGeometry():
            skipped += 1
            continue
        pt = f.geometry().centroid().asPoint()
        x, y = pt.x(), pt.y()
        if kind in ("pressure", "head"):
            best, bd = None, max_distance
            for n in nodes:
                nx, ny = net.node_xy[n]
                d = ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
                if d <= bd:
                    best, bd = n, d
            if best is None:
                skipped += 1
                continue
            if kind == "head":
                v -= model["junctions"][best][0]
            out.append(("pressure", best, v))
        else:
            best, bd = None, max_distance
            for k, p in enumerate(model["pipes"]):
                if p.get("kind") == "pump":
                    continue
                geom = _edge_geometry(net, p["id"]).asPolyline()
                for a, b in zip(geom[:-1], geom[1:]):
                    d = seg_distance(x, y, a.x(), a.y(), b.x(), b.y())
                    if d <= bd:
                        best, bd = k, d
            if best is None:
                skipped += 1
                continue
            out.append(("flow", best, v))
    return out, skipped


def calibrate(
    cfg,
    net,
    params,
    observations,
    group_by="",
    demand=False,
    c_min=40.0,
    c_max=160.0,
    feedback=None,
):
    from .hydro_solver import calibrate as _cal

    model = build_model(cfg, net, **params)
    groups = pipe_groups(cfg, model, group_by)
    out = _cal(
        model,
        observations,
        groups,
        demand=demand,
        c_min=c_min,
        c_max=c_max,
        feedback=feedback,
    )
    out["model"] = model
    out["groups"] = groups
    out["c_min"], out["c_max"] = c_min, c_max
    return out


def calibration_text(cal):
    b, a = cal["before"], cal["after"]

    def fmt(v):
        return "-" if v is None else "%.3f" % v

    lines = ["Calibration", ""]
    lines.append("%-28s %10s %10s" % ("", "before", "after"))
    for k in ("rmse", "mean abs error", "max abs error", "r2"):
        lines.append("%-28s %10s %10s" % (k, fmt(b.get(k)), fmt(a.get(k))))
    lines += ["", "Hazen-Williams C per pipe group:"]
    at_limit = False
    for lab, c in sorted(cal["c"].items(), key=lambda t: str(t[0])):
        limit = (
            c <= cal.get("c_min", 0) + 0.05
            or c >= cal.get("c_max", 1e9) - 0.05
        )
        at_limit = at_limit or limit
        lines.append(
            "  %-30s %7.1f  ->  %7.1f%s"
            % (
                lab,
                cal["c_before"][lab],
                c,
                "  (at the limit)" if limit else "",
            )
        )
    if at_limit:
        lines += [
            "",
            (
                "A C at the limit means the measurements say little about that"
                " group (low flows, or an error that roughness cannot explain:"
                " check elevations, source heads, closed valves and demands)"
                " before writing it to the lines."
            ),
        ]
    lines.append("Demand factor: %.3f" % cal["demand_factor"])
    lines += ["", "Measurements (measured / computed after calibration):"]
    for kind, ref, val, sim in a["rows"]:
        lines.append(
            "  %-8s %-8s %10.2f %10s"
            % (
                kind,
                ("N%d" % ref) if kind == "pressure" else ("P%d" % ref),
                val,
                "-" if sim is None else "%.2f" % sim,
            )
        )
    return "\n".join(lines)


def apply_roughness(cfg, cal):
    """Write the calibrated C into the 'roughness' field of the lines (the
    field is added when missing). Returns the number of lines changed."""
    from .qgis_io import make_field

    lyr = class_layers(cfg).get("Line")
    if lyr is None:
        raise ValueError("The network has no line class.")
    prov = lyr.dataProvider()
    if lyr.fields().indexFromName("roughness") < 0:
        prov.addAttributes([make_field("roughness", "double")])
        lyr.updateFields()
    idx = lyr.fields().indexFromName("roughness")
    per_line = {}
    for p, g in zip(cal["model"]["pipes"], cal["groups"]):
        if p["key"] and g in cal["c"]:
            per_line[p["key"][1]] = round(float(cal["c"][g]), 1)
    if lyr.isEditable():
        for fid, c in per_line.items():
            lyr.changeAttributeValue(fid, idx, c)
    else:
        prov.changeAttributeValues(
            {fid: {idx: c} for fid, c in per_line.items()}
        )
        lyr.triggerRepaint()
    return len(per_line)


DEFAULT_PATTERN = [
    0.6,
    0.5,
    0.45,
    0.45,
    0.5,
    0.7,
    1.1,
    1.45,
    1.4,
    1.25,
    1.15,
    1.1,
    1.15,
    1.1,
    1.0,
    0.95,
    1.05,
    1.25,
    1.45,
    1.4,
    1.2,
    1.0,
    0.85,
    0.7,
]


def parse_pattern(text):
    vals = [float(v) for v in text.replace(";", " ").replace(",", " ").split()]
    if not vals or any(v < 0 for v in vals):
        raise ValueError(
            "The demand pattern is a list of hourly multipliers, e.g."
            " 0.6 0.5 ... 1.4 (24 values)."
        )
    return vals


def simulate_day(
    cfg,
    net,
    params,
    hours=24,
    pattern=None,
    min_pressure=20.0,
    demand_factor=1.0,
    add_layer=True,
    title="",
):
    """24 h (extended period) simulation: lowest and highest pressure of
    every node over the day, hourly lowest pressure, demand and tank
    levels."""
    from qgis.core import QgsFeature, QgsProject

    from .hydro_solver import simulate

    model = build_model(cfg, net, **params)
    pattern = pattern or DEFAULT_PATTERN
    sim = simulate(model, hours, pattern, demand_factor)
    lyr = _memory(
        cfg,
        "Point",
        title or "Pressures over %d h" % hours,
        [
            ("node", "integer"),
            ("min_pressure", "double"),
            ("max_pressure", "double"),
            ("customers", "integer"),
            ("status", "string"),
        ],
    )
    feats, low_nodes, low_cust = [], 0, 0
    for n, lo in sim["min_pressure"].items():
        if n >= len(net.node_xy):
            continue
        st = "Low" if lo < min_pressure else "OK"
        cust = len(model["customers"].get(n, ()))
        if st == "Low":
            low_nodes += 1
            low_cust += cust
        f = QgsFeature(lyr.fields())
        f.setAttributes(
            [n, round(lo, 3), round(sim["max_pressure"][n], 3), cust, st]
        )
        f.setGeometry(_point_geometry(net, n, model))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    _categorized(lyr, "status", 0.9)
    if add_layer:
        QgsProject.instance().addMapLayer(lyr)
    unit = {"gas_lp": "mbar", "gas_mp": "bar"}.get(model["law"], "m")
    qunit = "m3/h" if model["law"] != "hw" else "L/s"
    lines = [
        "%-6s %14s %16s %s"
        % (
            "hour",
            "demand (%s)" % qunit,
            "lowest p (%s)" % unit,
            "  ".join("tank N%d (m)" % n for n in sim["tank_levels"]),
        )
    ]
    for i, t in enumerate(sim["times"]):
        lines.append(
            "%-6s %14.2f %16s %s"
            % (
                "%g" % t,
                sim["total_demand"][i],
                "-" if sim["lowest"][i] is None else "%.2f" % sim["lowest"][i],
                "  ".join(
                    "%12.2f" % lv[i] for lv in sim["tank_levels"].values()
                ),
            )
        )
    summary = {
        "hours": hours,
        "nodes below %.2f %s at some hour" % (min_pressure, unit): low_nodes,
        "customers affected": low_cust,
        "pumps": model["pumps"],
        "pressure reducing valves": len(model["valves"]),
        "tanks": len(model["tanks"]),
    }
    return summary, "\n".join(lines + sim["warnings"][:10]), lyr
