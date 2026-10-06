"""Network diagrams: a schematic (tree) drawing of a subnetwork or of a trace
result.

The diagram is written to two memory layers (nodes / links) placed to the right
of the
network, in the network CRS, so they can be printed or exported like any layer.
Each node
and link keeps the class / fid of the real feature."""

from collections import defaultdict, deque

from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor


def build(cfg, net, edges, root_nodes, spacing=None, title="Diagram"):
    """edges: set of edge ids to draw; root_nodes: nodes where the tree starts
    (controllers / starts)."""
    adj = defaultdict(list)
    for eid in edges:
        e = net.edges[eid]
        adj[e["a"]].append((eid, e["b"]))
        adj[e["b"]].append((eid, e["a"]))
    if not adj:
        raise ValueError("Nothing to draw: the result has no links.")
    roots = [r for r in root_nodes if r in adj] or [next(iter(adj))]
    children, depth, seen, order = defaultdict(list), {}, set(), []
    tree_edges = {}
    for r in roots:
        if r in seen:
            continue
        depth[r] = 0
        seen.add(r)
        q = deque([r])
        while q:
            n = q.popleft()
            order.append(n)
            for eid, m in sorted(adj[n], key=lambda x: x[1]):
                if m in seen:
                    continue
                seen.add(m)
                depth[m] = depth[n] + 1
                children[n].append(m)
                tree_edges[m] = eid
                q.append(m)
    for n in list(adj):  # parts not reachable from the roots
        if n not in seen:
            seen.add(n)
            depth[n] = 0
            roots.append(n)
            q = deque([n])
            while q:
                a = q.popleft()
                for eid, m in adj[a]:
                    if m not in seen:
                        seen.add(m)
                        depth[m] = depth[a] + 1
                        children[a].append(m)
                        tree_edges[m] = eid
                        q.append(m)
    # tidy layout: leaves get consecutive rows, parents sit in the middle of
    # their children
    row = {}
    counter = [0]

    def place(n):
        stack = [(n, False)]
        while stack:
            v, done = stack.pop()
            if done or not children[v]:
                if children[v]:
                    row[v] = sum(row[c] for c in children[v]) / len(
                        children[v]
                    )
                else:
                    row[v] = counter[0]
                    counter[0] += 1
                continue
            stack.append((v, True))
            for c in reversed(children[v]):
                stack.append((c, False))

    for r in roots:
        place(r)
    xs = [x for x, _y in net.node_xy] or [0]
    ys = [y for _x, y in net.node_xy] or [0]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    step = spacing or span / max(10, counter[0])
    ox, oy = max(xs) + span * 0.15, max(ys)
    pos = {
        n: QgsPointXY(ox + depth[n] * step * 1.6, oy - row[n] * step)
        for n in row
    }

    crs = cfg_crs(cfg)
    nodes_l = QgsVectorLayer(
        "Point?crs=%s&field=node:integer&field=class_name:string&field=fid:long"  # noqa
        "&field=label:string&field=depth:integer" % crs,
        "%s - nodes" % title,
        "memory",
    )
    links_l = QgsVectorLayer(
        "LineString?crs=%s&field=class_name:string&field=fid:long&field=label:string"  # noqa: E501
        % crs,
        "%s - links" % title,
        "memory",
    )
    nf = []
    for n, p in pos.items():
        f = QgsFeature(nodes_l.fields())
        pts = net.node_points[n]
        if pts:
            pt = net.points[pts[0]]
            f.setAttributes(
                [
                    n,
                    pt["key"][0],
                    pt["key"][1],
                    cfg.label(pt["key"][0], pt["ag"]),
                    depth[n],
                ]
            )
        else:
            f.setAttributes([n, None, None, "", depth[n]])
        f.setGeometry(QgsGeometry.fromPointXY(p))
        nf.append(f)
    lf = []
    for child, eid in tree_edges.items():
        parent = next((p for p, cs in children.items() if child in cs), None)
        if parent is None:
            continue
        e = net.edges[eid]
        key = net.edge_key(eid)
        f = QgsFeature(links_l.fields())
        ln = net.lines[e["line"]] if e["line"] is not None else None
        f.setAttributes(
            [
                key[0] if key else None,
                key[1] if key else None,
                cfg.label(key[0], ln["ag"]) if ln else "association",
            ]
        )
        a, b = pos[parent], pos[child]
        f.setGeometry(
            QgsGeometry.fromPolylineXY(
                [
                    a,
                    QgsPointXY(a.x() + (b.x() - a.x()) / 2, a.y()),
                    QgsPointXY(a.x() + (b.x() - a.x()) / 2, b.y()),
                    b,
                ]
            )
        )
        lf.append(f)
    nodes_l.dataProvider().addFeatures(nf)
    links_l.dataProvider().addFeatures(lf)
    links_l.renderer().symbol().setColor(QColor("#185fa5"))
    links_l.renderer().symbol().setWidth(0.5)
    nodes_l.renderer().symbol().setColor(QColor("#0f6e56"))
    _labels(nodes_l)
    QgsProject.instance().addMapLayers([links_l, nodes_l])
    return {"nodes": len(nf), "links": len(lf), "layers": (nodes_l, links_l)}


def cfg_crs(cfg):
    from .qgis_io import class_layers

    layers = class_layers(cfg)
    lyr = layers.get("Line") or next(iter(layers.values()))
    return lyr.crs().authid() or lyr.crs().toWkt()


def _labels(lyr):
    from qgis.core import (
        QgsPalLayerSettings,
        QgsTextFormat,
        QgsVectorLayerSimpleLabeling,
    )

    s = QgsPalLayerSettings()
    s.fieldName = "label"
    fmt = QgsTextFormat()
    fmt.setSize(8)
    s.setFormat(fmt)
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
    lyr.setLabelsEnabled(True)
