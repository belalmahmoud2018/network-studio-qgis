"""Longitudinal profile of a gravity line path (sewer / storm).

The path is a chain of line edges (e.g. a shortest path trace or the selected
pipes). Along the
chainage it draws the ground (junction elevation), the pipe invert (us_invert /
ds_invert of every
line) and the pipe crown (invert + diameter), with a node marker and label at
every junction.
"""

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF

from .qgis_io import _num, _val, class_layers


def build(cfg, net, edges, start_nodes=()):
    """Ordered rows along the path: node, chainage, ground, invert in / out,
    diameter, line key."""
    edges = [e for e in edges if net.edges[e]["line"] is not None]
    if not edges:
        raise ValueError("Select or trace the pipes of the profile first.")
    adj = {}
    for eid in edges:
        e = net.edges[eid]
        adj.setdefault(e["a"], []).append((eid, e["b"]))
        adj.setdefault(e["b"], []).append((eid, e["a"]))
    ends = [n for n, l in adj.items() if len(l) == 1]
    if len(ends) != 2 or any(len(v) > 2 for v in adj.values()):
        raise ValueError(
            "The pipes must form one continuous path without branches."
        )
    start = next((n for n in start_nodes if n in ends), None)
    if start is None:
        # start upstream: the end where the flow leaves
        d = net._directed(edges[0])
        start = (
            ends[0]
            if d is None
            else min(
                ends,
                key=lambda n: (
                    0
                    if any(
                        (net._directed(eid) or (0, 0))[0] == n
                        for eid, _m in adj[n]
                    )
                    else 1
                ),
            )
        )
    lines = class_layers(cfg)["Line"]
    cls = cfg.classes["Line"]
    size_field = cfg.settings.get("size_field") or cfg.tpl["size_field"][0]
    attrs = {}
    for f in lines.getFeatures():
        attrs[(cls, f.id())] = (
            _num(_val(f, "us_invert")),
            _num(_val(f, "ds_invert")),
            _num(_val(f, size_field)),
            _val(f, "assetid"),
        )
    ground = {}
    from .hydraulics import _node_values

    ground = _node_values(cfg, net, "elevation")
    rows, chain, n, prev = [], 0.0, start, None
    rows.append({"node": n, "chainage": 0.0, "ground": ground.get(n)})
    while True:
        nxt = [(eid, m) for eid, m in adj[n] if eid != prev]
        if not nxt:
            break
        eid, m = nxt[0]
        e = net.edges[eid]
        key = net.edge_key(eid)
        us, ds, dia, aid = attrs.get(key, (None, None, None, None))
        d = net._directed(eid) or (e["a"], e["b"])
        if d[0] != n:  # walking against the line: swap the inverts
            us, ds = ds, us
        rows[-1].update(
            {"invert_out": us, "pipe": aid or "%s:%s" % key, "diameter": dia}
        )
        chain += e["length"]
        rows.append(
            {
                "node": m,
                "chainage": round(chain, 2),
                "ground": ground.get(m),
                "invert_in": ds,
            }
        )
        prev, n = eid, m
    return rows


def draw(rows, path, title="Longitudinal profile", width=1600, height=800):
    img = QImage(width, height, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    left, right, top, bottom = 170, 50, 70, 140
    xs = [r["chainage"] for r in rows]
    zs = [
        v
        for r in rows
        for v in (r.get("ground"), r.get("invert_in"), r.get("invert_out"))
        if v is not None
    ]
    for r in rows:
        if r.get("invert_out") is not None and r.get("diameter"):
            zs.append(r["invert_out"] + r["diameter"] / 1000.0)
    if not zs:
        p.end()
        raise ValueError(
            "No elevations: fill us_invert / ds_invert of the pipes and"
            " elevation of the junctions."
        )
    zmin, zmax = min(zs), max(zs)
    pad = max((zmax - zmin) * 0.1, 0.5)
    zmin, zmax = zmin - pad, zmax + pad
    xmax = max(xs) or 1.0

    def X(c):
        return left + (width - left - right) * c / xmax

    def Y(z):
        return top + (height - top - bottom) * (zmax - z) / (zmax - zmin)

    f = QFont("Sans")
    f.setPointSize(12)
    f.setBold(True)
    p.setFont(f)
    p.drawText(QRectF(0, 10, width, 40), Qt.AlignmentFlag.AlignCenter, title)
    f.setBold(False)
    f.setPointSize(9)
    p.setFont(f)
    p.setPen(QPen(QColor("#999999"), 1))
    p.drawRect(QRectF(left, top, width - left - right, height - top - bottom))
    for i in range(6):
        z = zmin + (zmax - zmin) * i / 5
        p.drawText(
            QRectF(5, Y(z) - 8, left - 12, 16),
            Qt.AlignmentFlag.AlignRight,
            "%.2f" % z,
        )
    ground = [
        QPointF(X(r["chainage"]), Y(r["ground"]))
        for r in rows
        if r.get("ground") is not None
    ]
    if len(ground) > 1:
        p.setPen(QPen(QColor("#8c6d31"), 2))
        p.drawPolyline(QPolygonF(ground))
    for a, b in zip(rows, rows[1:]):
        if a.get("invert_out") is None or b.get("invert_in") is None:
            continue
        dia = (a.get("diameter") or 0) / 1000.0
        pts = [
            QPointF(X(a["chainage"]), Y(a["invert_out"])),
            QPointF(X(b["chainage"]), Y(b["invert_in"])),
            QPointF(X(b["chainage"]), Y(b["invert_in"] + dia)),
            QPointF(X(a["chainage"]), Y(a["invert_out"] + dia)),
        ]
        p.setPen(QPen(QColor("#185fa5"), 2))
        p.setBrush(QColor(55, 138, 221, 70))
        p.drawPolygon(QPolygonF(pts))
        slope = (
            (a["invert_out"] - b["invert_in"])
            / max(b["chainage"] - a["chainage"], 1e-9)
            * 100
        )
        p.setPen(QPen(QColor("#0c447c"), 1))
        p.drawText(
            QRectF(
                X(a["chainage"]),
                Y(a["invert_out"]) + 6,
                X(b["chainage"]) - X(a["chainage"]),
                16,
            ),
            Qt.AlignmentFlag.AlignCenter,
            "%s  %.2f%%" % (a.get("pipe") or "", slope),
        )
    p.setBrush(Qt.BrushStyle.NoBrush)
    for r in rows:
        x = X(r["chainage"])
        low = min(
            v
            for v in (
                r.get("invert_in"),
                r.get("invert_out"),
                r.get("ground"),
                zmax,
            )
            if v is not None
        )
        p.setPen(QPen(QColor("#444444"), 1, Qt.PenStyle.DashLine))
        if r.get("ground") is not None:
            p.drawLine(QPointF(x, Y(r["ground"])), QPointF(x, Y(low)))
        p.setPen(QPen(QColor("#222222"), 1))
        y0 = height - bottom + 10
        inv = (
            r.get("invert_in")
            if r.get("invert_in") is not None
            else r.get("invert_out")
        )
        p.drawText(
            QRectF(x - 45, y0, 90, 16),
            Qt.AlignmentFlag.AlignCenter,
            "%.1f" % r["chainage"],
        )
        p.drawText(
            QRectF(x - 45, y0 + 18, 90, 16),
            Qt.AlignmentFlag.AlignCenter,
            "G %.2f" % r["ground"] if r.get("ground") is not None else "G -",
        )
        p.drawText(
            QRectF(x - 45, y0 + 36, 90, 16),
            Qt.AlignmentFlag.AlignCenter,
            "IL %.2f" % inv if inv is not None else "IL -",
        )
    p.drawText(
        QRectF(5, height - bottom + 10, left - 60, 16),
        Qt.AlignmentFlag.AlignRight,
        "Chainage",
    )
    p.drawText(
        QRectF(5, height - bottom + 28, left - 60, 16),
        Qt.AlignmentFlag.AlignRight,
        "Ground",
    )
    p.drawText(
        QRectF(5, height - bottom + 46, left - 60, 16),
        Qt.AlignmentFlag.AlignRight,
        "Invert",
    )
    p.end()
    if not img.save(path):
        raise ValueError("Could not save %s" % path)
    return path
