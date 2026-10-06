"""Criticality and risk.

Consequence: how many customers lose supply when the line breaks (isolation of
its valve
segment). Likelihood: from age (install date vs. expected life) and condition
(1 good - 5 bad).
Risk = likelihood x consequence (1 - 25)."""

from collections import defaultdict
from datetime import date

from .qgis_io import _int, _val, class_layers
from .tools import _apply

CLASSES = [(4, "Low"), (9, "Medium"), (15, "High"), (25, "Very high")]


def _age_score(install, life):
    if install is None:
        return 3
    try:
        y = (
            install.year()
            if hasattr(install, "year") and callable(install.year)
            else int(str(install)[:4])
        )
    except (TypeError, ValueError):
        return 3
    if not y:
        return 3
    ratio = (date.today().year - y) / float(life)
    return (
        1
        if ratio < 0.25
        else 2 if ratio < 0.5 else 3 if ratio < 0.75 else 4 if ratio < 1 else 5
    )


def criticality(cfg, net):
    """{line key: customers affected} for every line, in one pass.

    The network is cut into valve segments (areas between isolating devices).
    Segments touching the
    same open valve are neighbours. Breaking a line isolates its segment: its
    customers lose supply,
    and so do the customers of every segment that is only fed through it. With
    all sources tied to
    one root these are the DFS sub-trees cut off at that segment (articulation
    points), so the
    network is solved in linear time instead of one isolation trace per
    segment.
    """
    isolating = {
        net.point_node[pi]
        for pi in range(len(net.points))
        if "isolating" in net.point_cats(pi)
        and not net.points[pi].get("closed")
    }
    stop = isolating | net.closed_nodes()
    seg_node, seg_edges, seen_e = {}, [], set()
    for eid in range(len(net.edges)):
        if eid in seen_e:
            continue
        nodes, edges = net._bfs(set(), {eid}, stop, ())
        seen_e |= edges
        sid = len(seg_edges)
        for n in nodes - stop:
            seg_node.setdefault(n, sid)
        seg_edges.append(edges)
    cust = defaultdict(int)
    for pi in range(len(net.points)):
        if "customer" in net.point_cats(pi) and net.point_node[pi] in seg_node:
            cust[seg_node[net.point_node[pi]]] += 1
    adj = defaultdict(set)
    for v in isolating:
        segs = {seg_node.get(m) for _e, m in net.adj[v]} - {None}
        for x in segs:
            adj[x] |= segs - {x}
    root = -1
    for n in net.source_nodes():
        around = (
            {seg_node[n]}
            if n in seg_node
            else {seg_node[m] for _e, m in net.adj[n] if m in seg_node}
        )
        for sg in around:
            adj[root].add(sg)
            adj[sg].add(root)
    disc, low, sub, lost = {root: 0}, {root: 0}, {root: 0}, {}
    timer = 1
    stack = [(root, None, iter(sorted(adj[root])))]
    while stack:
        v, parent, it = stack[-1]
        advanced = False
        for w in it:
            if w == parent:
                continue
            if w in disc:
                low[v] = min(low[v], disc[w])
            else:
                disc[w] = low[w] = timer
                timer += 1
                sub[w] = lost[w] = cust[w]
                stack.append((w, v, iter(sorted(adj[w]))))
                advanced = True
                break
        if not advanced:
            stack.pop()
            if stack:
                p = stack[-1][0]
                low[p] = min(low[p], low[v])
                sub[p] = sub.get(p, 0) + sub[v]
                if p != root and low[v] >= disc[p]:
                    lost[p] = lost[p] + sub[v]
    result = defaultdict(int)
    for sid, edges in enumerate(seg_edges):
        n = lost.get(
            sid, cust[sid]
        )  # segments no source reaches lose only themselves
        for eid in edges:
            k = net.edge_key(eid)
            if k is not None:
                result[k] = max(result[k], n)
    return result, len(seg_edges)


def run(cfg, net, life=50):
    crit, nseg = criticality(cfg, net)
    mx = max(crit.values()) if crit else 0
    layers = class_layers(cfg)
    lyr = layers["Line"]
    cls = cfg.classes["Line"]
    idx = {
        n: lyr.fields().indexFromName(n)
        for n in (
            "crit_customers",
            "likelihood",
            "consequence",
            "risk_score",
            "risk_class",
        )
    }
    if min(idx.values()) < 0:
        raise ValueError(
            "The Line class has no risk fields: use Editing > Upgrade network"
            " first."
        )
    rows, updates = [], {}
    for f in lyr.getFeatures():
        c = crit.get((cls, f.id()), 0)
        cons = 1 if c == 0 or mx == 0 else min(5, 1 + int(4 * c / mx + 0.999))
        cond = _int(_val(f, "condition"))
        like = max(
            _age_score(_val(f, "installdate"), life),
            cond if cond in (1, 2, 3, 4, 5) else 1,
        )
        score = like * cons
        rc = next(name for lim, name in CLASSES if score <= lim)
        updates[f.id()] = {
            idx["crit_customers"]: c,
            idx["likelihood"]: like,
            idx["consequence"]: cons,
            idx["risk_score"]: score,
            idx["risk_class"]: rc,
        }
        ag = _int(_val(f, "assetgroup"))
        rows.append(
            {
                "fid": f.id(),
                "assetid": _val(f, "assetid"),
                "Asset group": cfg.label(cls, ag),
                "Length": (
                    round(f.geometry().length(), 2)
                    if f.hasGeometry()
                    else None
                ),
                "Customers affected": c,
                "Likelihood": like,
                "Consequence": cons,
                "Risk score": score,
                "Risk class": rc,
            }
        )
    _apply(lyr, updates)
    rows.sort(key=lambda r: (-r["Risk score"], -r["Customers affected"]))
    for i, r in enumerate(rows, 1):
        r["Priority"] = i
    return rows, nseg


def risk_layer(cfg):
    """A copy of the Line layer styled by risk class."""
    from qgis.core import (
        QgsCategorizedSymbolRenderer,
        QgsProject,
        QgsRendererCategory,
        QgsSymbol,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtGui import QColor
    from . import storage as S

    lyr = QgsVectorLayer(
        S.uri(cfg.path, cfg.classes["Line"]), "Risk map", "ogr"
    )
    cats = []
    for (lim, name), color in zip(
        CLASSES, ("#1a9850", "#fee08b", "#f46d43", "#a50026")
    ):
        sym = QgsSymbol.defaultSymbol(lyr.geometryType())
        sym.setColor(QColor(color))
        sym.setWidth(1.2)
        cats.append(QgsRendererCategory(name, sym, name))
    lyr.setRenderer(QgsCategorizedSymbolRenderer("risk_class", cats))
    QgsProject.instance().addMapLayer(lyr)
    return lyr
