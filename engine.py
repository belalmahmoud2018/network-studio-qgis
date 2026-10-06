"""Network engine: connectivity graph, traces and validation.

Pure Python (no QGIS / GDAL imports) so it behaves the same for every storage
format and can be tested anywhere.

Input
-----
points : [{"key": (class, fid), "x", "y", "ag", "at", "closed": bool}]
lines  : [{"key": (class, fid), "parts": [[(x, y), ...]], "ag", "at",
           "reverse": bool, "oneway": 0|1|2}]
cats   : {(class, ag): set of categories}   e.g. {"source", "isolating",
"customer"}
tiers  : {(class, ag): tier name or None}
flow   : "source" (pressure networks), "gravity" (sewer/storm) or "undirected"
(roads)

Connectivity follows the usual utility-network idea: features connect when they
are
coincident (within the tolerance) at a line END POINT or at a line VERTEX
touched by a
point feature or by another line's end point. Lines that only cross are not
connected.
"""

import heapq
import math
from collections import defaultdict, deque

INF = float("inf")


# --------------------------------------------------------------------- helpers
class _Grid:
    """Simple hash grid for points."""

    def __init__(self, cell):
        self.cell = max(cell, 1e-12)
        self.d = defaultdict(list)

    def add(self, x, y, item):
        self.d[(math.floor(x / self.cell), math.floor(y / self.cell))].append(
            (x, y, item)
        )

    def near(self, x, y, r):
        c = self.cell
        r2 = r * r
        for i in range(math.floor((x - r) / c), math.floor((x + r) / c) + 1):
            for j in range(
                math.floor((y - r) / c), math.floor((y + r) / c) + 1
            ):
                for px, py, item in self.d.get((i, j), ()):
                    if (px - x) ** 2 + (py - y) ** 2 <= r2:
                        yield px, py, item


class _SegGrid:
    """Hash grid for segments (registered in every cell their padded bbox
    covers)."""

    def __init__(self, cell, pad):
        self.cell, self.pad = max(cell, 1e-12), pad
        self.d = defaultdict(list)

    def add(self, x1, y1, x2, y2, item):
        c, p = self.cell, self.pad
        for i in range(
            math.floor((min(x1, x2) - p) / c),
            math.floor((max(x1, x2) + p) / c) + 1,
        ):
            for j in range(
                math.floor((min(y1, y2) - p) / c),
                math.floor((max(y1, y2) + p) / c) + 1,
            ):
                self.d[(i, j)].append((x1, y1, x2, y2, item))

    def near(self, x, y):
        return self.d.get(
            (math.floor(x / self.cell), math.floor(y / self.cell)), ()
        )


def seg_distance(px, py, x1, y1, x2, y2):
    """(distance, t) from a point to a segment, t = position 0..1 along it."""
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    t = (
        0.0
        if l2 == 0
        else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    )
    qx, qy = x1 + t * dx, y1 + t * dy
    return math.hypot(px - qx, py - qy), t


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, a):
        p = self.p
        p.setdefault(a, a)
        root = a
        while p[root] != root:
            root = p[root]
        while p[a] != root:
            p[a], a = root, p[a]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


OPS = ("=", "!=", "<", "<=", ">", ">=", "is empty", "is not empty")


def _match(feature, conditions):
    """True when a feature meets any (attribute, operator, value) condition
    (OR)."""
    attrs = feature.get("attrs") or {}
    for name, op, value in conditions:
        v = attrs.get(name)
        if op == "is empty":
            if v in (None, ""):
                return True
            continue
        if op == "is not empty":
            if v not in (None, ""):
                return True
            continue
        if v is None:
            continue
        try:
            a, b = float(v), float(value)
        except (TypeError, ValueError):
            a, b = str(v).lower(), str(value).lower()
        if (
            (op == "=" and a == b)
            or (op == "!=" and a != b)
            or (op == "<" and a < b)
            or (op == "<=" and a <= b)
            or (op == ">" and a > b)
            or (op == ">=" and a >= b)
        ):
            return True
    return False


class TraceResult:
    def __init__(self):
        self.lines = set()  # line feature keys
        self.points = set()  # point feature keys
        self.extra = {}  # trace specific details
        self.message = ""

    def count(self):
        return len(self.lines) + len(self.points)


# --------------------------------------------------------------------- network
class Network:
    def __init__(
        self,
        points,
        lines,
        tolerance,
        cats=None,
        tiers=None,
        flow="source",
        associations=(),
        gap=None,
        terminals=None,
        controllers=None,
        tier_settings=None,
    ):
        self.points = list(points)
        self.lines = list(lines)
        self.tol = float(tolerance)
        self.gap = float(gap) if gap else self.tol * 500
        self.cats = cats or {}
        self.tiers = tiers or {}
        self.flow = flow
        self.terminals = (
            terminals or {}
        )  # {(class, ag): [(terminal name, tier), ...]}
        self.controllers = dict(
            controllers or {}
        )  # {(class, fid): subnetwork name} explicit controllers
        self.tier_settings = (
            tier_settings or {}
        )  # {tier: {"multi": bool, "require": bool}}
        self.issues = []  # (code, severity, message, x, y, key)
        self._build(associations)

    # ---------------------------------------------------------------- build
    def point_cats(self, pi):
        p = self.points[pi]
        return self.cats.get((p["key"][0], p["ag"]), set())

    def line_cats(self, li):
        ln = self.lines[li]
        return self.cats.get((ln["key"][0], ln["ag"]), set())

    def _build(self, associations):
        tol = self.tol
        uf = _UF()
        vgrid = _Grid(tol * 2)
        pgrid = _Grid(tol * 2)
        seg_lengths = []
        for li, ln in enumerate(self.lines):
            for pt, coords in enumerate(ln["parts"]):
                for vi, (x, y) in enumerate(coords):
                    vgrid.add(x, y, (li, pt, vi))
                    if vi:
                        px, py = coords[vi - 1]
                        seg_lengths.append(math.hypot(x - px, y - py))
        self._gap_grid = _Grid(max(self.gap, tol))
        for pi, p in enumerate(self.points):
            pgrid.add(p["x"], p["y"], pi)
            self._gap_grid.add(p["x"], p["y"], pi)

        mean = (sum(seg_lengths) / len(seg_lengths)) if seg_lengths else 1.0
        sgrid = _SegGrid(max(mean, self.gap, tol * 10), max(self.gap, tol))
        for li, ln in enumerate(self.lines):
            for pt, coords in enumerate(ln["parts"]):
                for vi in range(1, len(coords)):
                    (x1, y1), (x2, y2) = coords[vi - 1], coords[vi]
                    sgrid.add(x1, y1, x2, y2, (li, pt, vi - 1))
        self._sgrid = sgrid

        self._endvertex_hits = []
        conn = defaultdict(set)  # (li, pt) -> connection vertex indexes
        partners = defaultdict(
            int
        )  # candidate token -> number of coincident others
        candidates = []
        for li, ln in enumerate(self.lines):
            for pt, coords in enumerate(ln["parts"]):
                if len(coords) < 2:
                    continue
                last = len(coords) - 1
                conn[(li, pt)].update((0, last))
                candidates.append((("V", li, pt, 0), coords[0], li))
                candidates.append((("V", li, pt, last), coords[last], li))
        for pi, p in enumerate(self.points):
            candidates.append((("P", pi), (p["x"], p["y"]), None))

        for token, (x, y), own_line in candidates:
            uf.find(token)
            for _vx, _vy, (li, pt, vi) in vgrid.near(x, y, tol):
                other = ("V", li, pt, vi)
                if other == token:
                    continue
                if (
                    own_line == li
                    and token[2] == pt
                    and abs(token[3] - vi) == 0
                ):
                    continue
                last_v = len(self.lines[li]["parts"][pt]) - 1
                if (
                    token[0] == "V"
                    and vi in (0, last_v)
                    and self._level(token[1], token[3], token[2])
                    != self._level(li, vi, pt)
                ):
                    # grade separated (bridge / tunnel): same place, different
                    # level
                    continue
                if 0 < vi < last_v and "endvertex" in self.line_cats(li):
                    self._endvertex_hits.append((token, li))
                    continue  # this line connects only at its end vertices
                conn[(li, pt)].add(vi)
                uf.union(token, other)
                partners[token] += 1
            if token[0] == "V":
                for _px, _py, pi in pgrid.near(x, y, tol):
                    uf.union(token, ("P", pi))
                    partners[token] += 1
            else:
                for _px, _py, pi in pgrid.near(x, y, tol):
                    if pi != token[1]:
                        uf.union(token, ("P", pi))
                        partners[token] += 1

        # nodes
        node_of = {}
        self.node_xy = []
        self.node_points = []

        def node_id(token, xy):
            root = uf.find(token)
            n = node_of.get(root)
            if n is None:
                n = node_of[root] = len(self.node_xy)
                self.node_xy.append(xy)
                self.node_points.append([])
            return n

        self.point_node = {}
        for pi, p in enumerate(self.points):
            n = node_id(("P", pi), (p["x"], p["y"]))
            self.node_points[n].append(pi)
            self.point_node[pi] = n

        # edges (a line part split at its connection vertices)
        self.edges = []  # dict(line, part, v0, v1, a, b, length)
        self.line_edges = defaultdict(list)
        for (li, pt), idxs in conn.items():
            coords = self.lines[li]["parts"][pt]
            idxs = sorted(idxs)
            for v0, v1 in zip(idxs, idxs[1:]):
                a = node_id(("V", li, pt, v0), coords[v0])
                b = node_id(("V", li, pt, v1), coords[v1])
                length = sum(
                    math.hypot(
                        coords[k][0] - coords[k - 1][0],
                        coords[k][1] - coords[k - 1][1],
                    )
                    for k in range(v0 + 1, v1 + 1)
                )
                eid = len(self.edges)
                self.edges.append(
                    {
                        "line": li,
                        "part": pt,
                        "v0": v0,
                        "v1": v1,
                        "a": a,
                        "b": b,
                        "length": length,
                    }
                )
                self.line_edges[li].append(eid)

        # connectivity associations become zero-length edges between two point
        # nodes
        key_point = {p["key"]: pi for pi, p in enumerate(self.points)}
        for kind, ka, kb in associations:
            if (
                kind != "connectivity"
                or ka not in key_point
                or kb not in key_point
            ):
                continue
            a, b = (
                self.point_node[key_point[ka]],
                self.point_node[key_point[kb]],
            )
            if a != b:
                self.edges.append(
                    {
                        "line": None,
                        "part": None,
                        "v0": None,
                        "v1": None,
                        "a": a,
                        "b": b,
                        "length": 0.0,
                    }
                )

        self._split_terminals()

        self.adj = defaultdict(list)
        for eid, e in enumerate(self.edges):
            self.adj[e["a"]].append((eid, e["b"]))
            self.adj[e["b"]].append((eid, e["a"]))

        self._candidates = candidates
        self._partners = partners

    def _level(self, li, vi, pt=0):
        """Elevation level (Esri F_ELEV / T_ELEV) of a line end; 0 when
        unknown."""
        lv = self.lines[li].get("levels")
        if not lv:
            return 0
        last = len(self.lines[li]["parts"][pt]) - 1
        return (lv[0] or 0) if vi == 0 else (lv[1] or 0) if vi == last else 0

    def _split_terminals(self):
        """Devices with terminals (e.g. transformer high / low side): every
        terminal gets its own
        node; lines attach to the terminal of their tier; the device joins its
        terminals.
        """
        self.terminal_issues = []
        if not self.terminals:
            return
        for pi, p in enumerate(self.points):
            conf = self.terminals.get((p["key"][0], p["ag"]))
            if not conf:
                continue
            n = self.point_node[pi]
            tnode = {}
            for name, tier in conf:
                tnode[tier] = len(self.node_xy)
                self.node_xy.append(self.node_xy[n])
                self.node_points.append([])
                self.edges.append(
                    {
                        "line": None,
                        "part": None,
                        "v0": None,
                        "v1": None,
                        "a": n,
                        "b": tnode[tier],
                        "length": 0.0,
                        "device": pi,
                        "terminal": name,
                    }
                )
            for e in self.edges:
                if e["line"] is None:
                    continue
                for end in ("a", "b"):
                    if e[end] != n:
                        continue
                    ln = self.lines[e["line"]]
                    tier = self.tiers.get((ln["key"][0], ln["ag"]))
                    if tier in tnode:
                        e[end] = tnode[tier]
                    else:
                        self.terminal_issues.append((pi, e["line"]))

    # --------------------------------------------------------------- lookups
    def edge_key(self, eid):
        li = self.edges[eid]["line"]
        return None if li is None else self.lines[li]["key"]

    def nodes_of_key(self, key):
        """Start/barrier location for a feature: (nodes, edges)."""
        for pi, p in enumerate(self.points):
            if p["key"] == key:
                return {self.point_node[pi]}, set()
        for li, ln in enumerate(self.lines):
            if ln["key"] == key:
                return set(), set(self.line_edges.get(li, ()))
        return set(), set()

    def edge_near(self, key, x, y):
        """The single sub-edge of line `key` closest to (x, y)."""
        best, best_d = None, INF
        for li, ln in enumerate(self.lines):
            if ln["key"] != key:
                continue
            for eid in self.line_edges.get(li, ()):
                e = self.edges[eid]
                coords = ln["parts"][e["part"]]
                for k in range(e["v0"] + 1, e["v1"] + 1):
                    d, _t = seg_distance(x, y, *coords[k - 1], *coords[k])
                    if d < best_d:
                        best, best_d = eid, d
        return best

    def controller_points(self):
        """Indexes of the points that control subnetworks (explicit
        controllers, else 'source' groups)."""
        if self.controllers:
            return [
                pi
                for pi, p in enumerate(self.points)
                if p["key"] in self.controllers
            ]
        return [
            pi
            for pi in range(len(self.points))
            if "source" in self.point_cats(pi)
        ]

    def source_nodes(self):
        return {self.point_node[pi] for pi in self.controller_points()}

    def connected_keys(self):
        """Features topologically connected to any controller (operating status
        ignored): 'Is connected'."""
        nodes, edges = self._bfs(self.source_nodes(), ())
        r = self._collect(nodes, edges)
        return r.lines | r.points

    def closed_nodes(self):
        return {
            self.point_node[pi]
            for pi, p in enumerate(self.points)
            if p.get("closed")
        }

    def _collect(self, nodes, edges, res=None):
        res = res or TraceResult()
        for eid in edges:
            k = self.edge_key(eid)
            if k is not None:
                res.lines.add(k)
        for n in nodes:
            for pi in self.node_points[n]:
                res.points.add(self.points[pi]["key"])
        return res

    # ------------------------------------------------------------ traversal
    def _bfs(
        self,
        start_nodes,
        start_edges,
        blocked_nodes=(),
        blocked_edges=(),
        edge_ok=None,
    ):
        seen_n, seen_e = set(), set(start_edges)
        # a picked point always spreads; the ends of a picked line stop at
        # barriers (a closed or
        # isolating device at the end of a broken pipe must not be crossed)
        starts = set(start_nodes)
        q = deque(start_nodes)
        for eid in start_edges:
            q.extend((self.edges[eid]["a"], self.edges[eid]["b"]))
        while q:
            n = q.popleft()
            if n in seen_n:
                continue
            seen_n.add(n)
            if n in blocked_nodes and n not in starts:
                continue
            for eid, m in self.adj[n]:
                if eid in blocked_edges or (edge_ok and not edge_ok(eid)):
                    continue
                seen_e.add(eid)
                if m not in seen_n:
                    q.append(m)
        return seen_n, seen_e

    def _directed(self, eid):
        """(from, to) for gravity / one-way edges, or None if both ways."""
        e = self.edges[eid]
        if e["line"] is None:
            return None
        ln = self.lines[e["line"]]
        if self.flow == "gravity":
            return (e["b"], e["a"]) if ln.get("reverse") else (e["a"], e["b"])
        if self.flow == "undirected":
            ow = ln.get("oneway") or 0
            if ow == 1:
                return e["a"], e["b"]
            if ow == 2:
                return e["b"], e["a"]
        return None

    def _dijkstra(
        self, sources, blocked_nodes=(), blocked_edges=(), directed=False
    ):
        dist = {s: 0.0 for s in sources}
        parent = {}
        heap = [(0.0, s) for s in sources]
        heapq.heapify(heap)
        while heap:
            d, n = heapq.heappop(heap)
            if d > dist.get(n, INF):
                continue
            if n in blocked_nodes and n not in sources:
                continue
            for eid, m in self.adj[n]:
                if eid in blocked_edges:
                    continue
                if directed:
                    dirn = self._directed(eid)
                    if dirn is not None and dirn[0] != n:
                        continue
                nd = d + self.edges[eid]["length"]
                if nd < dist.get(m, INF):
                    dist[m] = nd
                    parent[m] = (eid, n)
                    heapq.heappush(heap, (nd, m))
        return dist, parent

    # ---------------------------------------------------------------- traces
    def trace(
        self,
        kind,
        start_nodes,
        start_edges,
        barrier_nodes=(),
        barrier_edges=(),
        use_status=True,
        target_nodes=(),
        target_edges=(),
        exclude_lifecycle=(),
        min_size=None,
        output_groups=None,
        conditions=(),
    ):
        blocked = set(barrier_nodes)
        if use_status:
            blocked |= self.closed_nodes()
        bedges = set(barrier_edges)
        if exclude_lifecycle or min_size:
            ex = set(exclude_lifecycle)
            for eid, e in enumerate(self.edges):
                if e["line"] is None:
                    continue
                ln = self.lines[e["line"]]
                if ln.get("lifecycle") in ex:
                    bedges.add(eid)
                elif (
                    min_size
                    and ln.get("size") is not None
                    and ln["size"] < min_size
                ):
                    bedges.add(eid)
            for pi, p in enumerate(self.points):
                if p.get("lifecycle") in ex:
                    blocked.add(self.point_node[pi])
        if conditions:
            for eid, e in enumerate(self.edges):
                if e["line"] is not None and _match(
                    self.lines[e["line"]], conditions
                ):
                    bedges.add(eid)
            for pi, p in enumerate(self.points):
                if _match(p, conditions):
                    blocked.add(self.point_node[pi])
        res = self._trace(
            kind,
            start_nodes,
            start_edges,
            blocked,
            bedges,
            target_nodes,
            target_edges,
        )
        if output_groups:
            keep = set(output_groups)
            ag_of = {p["key"]: (p["key"][0], p["ag"]) for p in self.points}
            ag_of.update(
                {ln["key"]: (ln["key"][0], ln["ag"]) for ln in self.lines}
            )
            res.lines = {k for k in res.lines if ag_of.get(k) in keep}
            res.points = {k for k in res.points if ag_of.get(k) in keep}
            res.message += " Shown after the output filter: %d." % res.count()
        return res

    def _trace(
        self,
        kind,
        start_nodes,
        start_edges,
        blocked,
        bedges,
        target_nodes,
        target_edges,
    ):
        start_nodes, start_edges = set(start_nodes), set(start_edges)
        if not start_nodes and not start_edges and kind not in ("loops",):
            res = TraceResult()
            res.message = "Add at least one starting point."
            return res
        fn = getattr(self, "_trace_" + kind)
        if kind == "shortest_path":
            return fn(
                start_nodes,
                start_edges,
                set(target_nodes),
                set(target_edges),
                blocked,
                bedges,
            )
        return fn(start_nodes, start_edges, blocked, bedges)

    def _trace_connected(self, sn, se, blocked, bedges):
        nodes, edges = self._bfs(sn, se, blocked, bedges)
        res = self._collect(nodes, edges)
        res.message = "%d connected feature(s)." % res.count()
        return res

    def _ends(self, se, sn, dist, low):
        """Root nodes for up/downstream from start edges using source
        distances."""
        roots = set(sn)
        for eid in se:
            e = self.edges[eid]
            da, db = dist.get(e["a"], INF), dist.get(e["b"], INF)
            if low:
                roots.add(e["a"] if da <= db else e["b"])
            else:
                roots.add(e["a"] if da > db else e["b"])
        return roots

    def _trace_downstream(self, sn, se, blocked, bedges):
        return self._updown(sn, se, blocked, bedges, down=True)

    def _trace_upstream(self, sn, se, blocked, bedges):
        return self._updown(sn, se, blocked, bedges, down=False)

    def _trace_upstream_all(self, sn, se, blocked, bedges):
        """Upstream through every possible path: in looped pressure networks
        the flow can reach the
        start along any loop, so every loop (biconnected block) between the
        start and a source counts.
        """
        res = TraceResult()
        if self.flow != "source":
            return self._updown(sn, se, blocked, bedges, down=False)
        sources = self.source_nodes()
        if not sources:
            res.message = "No source (subnetwork controller) found."
            return res
        starts = set(sn) | {self.edges[e][k] for e in se for k in ("a", "b")}
        usable = {
            eid
            for eid, e in enumerate(self.edges)
            if eid not in bedges
            and not (
                (e["a"] in blocked and e["a"] not in starts)
                or (e["b"] in blocked and e["b"] not in starts)
            )
        }
        blocks = self.blocks(usable)
        node_blocks = defaultdict(set)
        for bi, edges in enumerate(blocks):
            for eid in edges:
                node_blocks[self.edges[eid]["a"]].add(bi)
                node_blocks[self.edges[eid]["b"]].add(bi)
        # block-cut tree search from the start node(s)
        parent = {}
        q = deque()
        for n in starts:
            parent[("N", n)] = None
            q.append(("N", n))
        while q:
            v = q.popleft()
            nxt = (
                [("B", b) for b in node_blocks[v[1]]]
                if v[0] == "N"
                else [
                    ("N", n)
                    for eid in blocks[v[1]]
                    for n in (self.edges[eid]["a"], self.edges[eid]["b"])
                ]
            )
            for w in nxt:
                if w not in parent:
                    parent[w] = v
                    q.append(w)
        use = set()
        for s in sources:
            v = ("N", s)
            if v not in parent:
                continue
            while v is not None:
                if v[0] == "B":
                    use.add(v[1])
                v = parent[v]
        edges = set(se)
        for b in use:
            edges |= blocks[b]
        nodes = {self.edges[e][k] for e in edges for k in ("a", "b")} | starts
        res = self._collect(nodes, edges)
        res.message = (
            "%d upstream feature(s) (all paths, loops included)." % res.count()
        )
        return res

    def blocks(self, edge_set):
        """Biconnected components (lists of edge sets) of the sub graph made of
        edge_set."""
        adj = defaultdict(list)
        for eid in edge_set:
            e = self.edges[eid]
            if e["a"] == e["b"]:
                continue
            adj[e["a"]].append((eid, e["b"]))
            adj[e["b"]].append((eid, e["a"]))
        disc, low, out, estack = {}, {}, [], []
        timer = 0
        for root in list(adj):
            if root in disc:
                continue
            disc[root] = low[root] = timer
            timer += 1
            stack = [(root, None, iter(adj[root]))]
            while stack:
                n, pe, it = stack[-1]
                advanced = False
                for eid, m in it:
                    if eid == pe:
                        continue
                    if m not in disc:
                        estack.append(eid)
                        disc[m] = low[m] = timer
                        timer += 1
                        stack.append((m, eid, iter(adj[m])))
                        advanced = True
                        break
                    if disc[m] < disc[n]:
                        estack.append(eid)
                        low[n] = min(low[n], disc[m])
                if not advanced:
                    stack.pop()
                    if stack:
                        p = stack[-1][0]
                        low[p] = min(low[p], low[n])
                        if low[n] >= disc[p]:
                            comp = set()
                            while estack:
                                x = estack.pop()
                                comp.add(x)
                                if x == pe:
                                    break
                            out.append(comp)
        return out

    def _updown(self, sn, se, blocked, bedges, down):
        res = TraceResult()
        if self.flow == "undirected":
            res.message = (
                "Upstream / downstream need a flow direction (not available"
                " for this network type)."
            )
            return res
        if self.flow == "gravity":
            out = defaultdict(list)
            for eid in range(len(self.edges)):
                if eid in bedges:
                    continue
                d = self._directed(eid)
                if d is None:
                    out[self.edges[eid]["a"]].append(
                        (eid, self.edges[eid]["b"])
                    )
                    out[self.edges[eid]["b"]].append(
                        (eid, self.edges[eid]["a"])
                    )
                    continue
                f, t = d if down else (d[1], d[0])
                out[f].append((eid, t))
            q = deque(sn)
            nodes, edges = set(), set(se)
            for eid in se:
                d = self._directed(eid) or (
                    self.edges[eid]["a"],
                    self.edges[eid]["b"],
                )
                q.append(d[1] if down else d[0])
            while q:
                n = q.popleft()
                if n in nodes:
                    continue
                nodes.add(n)
                if n in blocked and n not in sn:
                    continue
                for eid, m in out[n]:
                    edges.add(eid)
                    q.append(m)
            res = self._collect(nodes, edges)
            res.message = "%d %s feature(s)." % (
                res.count(),
                "downstream" if down else "upstream",
            )
            return res

        sources = self.source_nodes()
        if not sources:
            res.message = (
                "No source (subnetwork controller) found. Add a source device"
                " first."
            )
            return res
        dist, parent = self._dijkstra(sources, blocked, bedges)
        roots = self._ends(se, sn, dist, low=not down)
        if not any(r in dist for r in roots):
            res.message = (
                "The start is not fed by any source (check closed devices or"
                " gaps)."
            )
            return res
        if down:
            children = defaultdict(list)
            for m, (eid, n) in parent.items():
                children[n].append((eid, m))
            nodes, edges = set(), set(se)
            q = deque(r for r in roots if r in dist)
            while q:
                n = q.popleft()
                if n in nodes:
                    continue
                nodes.add(n)
                for eid, m in children[n]:
                    edges.add(eid)
                    q.append(m)
        else:
            nodes, edges = set(), set(se)
            for r in roots:
                n = r
                while n in dist:
                    nodes.add(n)
                    if n not in parent:
                        break
                    eid, n = parent[n]
                    edges.add(eid)
        res = self._collect(nodes, edges)
        res.message = "%d %s feature(s) (main feed path)." % (
            res.count(),
            "downstream" if down else "upstream",
        )
        return res

    def _trace_isolation(self, sn, se, blocked, bedges):
        res = TraceResult()
        if self.flow == "undirected":
            res.message = (
                "Isolation is for pressure networks (valves / switches)."
            )
            return res
        isolating = {
            self.point_node[pi]
            for pi in range(len(self.points))
            if "isolating" in self.point_cats(pi)
            and not self.points[pi].get("closed")
        }
        # area reached from the start before hitting isolating devices
        area_n, area_e = self._bfs(sn, se, blocked | isolating, bedges)
        to_close = (area_n & isolating) - sn
        sources = self.source_nodes()
        before_n, before_e = (
            self._bfs(sources, (), blocked, bedges)
            if sources
            else (set(), set())
        )
        after_n, after_e = (
            self._bfs(
                sources - to_close - area_n,
                (),
                blocked | to_close,
                bedges | area_e,
            )
            if sources
            else (set(), set())
        )
        lost_n = (before_n - after_n) | (area_n - to_close)
        lost_e = (before_e - after_e) | area_e
        res = self._collect(lost_n - to_close, lost_e)
        valves = [
            self.points[pi]["key"]
            for n in to_close
            for pi in self.node_points[n]
            if "isolating" in self.point_cats(pi)
        ]
        customers = [
            self.points[pi]["key"]
            for n in lost_n
            for pi in self.node_points[n]
            if "customer" in self.point_cats(pi)
        ]
        res.extra = {
            "to_close": sorted(valves),
            "customers": sorted(customers),
        }
        res.points |= set(valves)
        res.message = (
            "Close %d isolating device(s); %d feature(s) and %d customer(s)"
            " lose supply."
            % (
                len(valves),
                len(res.lines) + len(res.points) - len(valves),
                len(customers),
            )
        )
        if not sources:
            res.message += (
                " (No source defined: only the isolated area is shown.)"
            )
        return res

    def _trace_shortest_path(self, sn, se, tn, te, blocked, bedges):
        res = TraceResult()
        starts = set(sn) | {self.edges[e][k] for e in se for k in ("a", "b")}
        targets = set(tn) | {self.edges[e][k] for e in te for k in ("a", "b")}
        if not targets:
            res.message = "Shortest path needs a start and an end point."
            return res
        dist, parent = self._dijkstra(
            starts, blocked, bedges, directed=self.flow == "undirected"
        )
        reach = [t for t in targets if t in dist]
        if not reach:
            res.message = "No path between the start and the end."
            return res
        t = min(reach, key=lambda n: dist[n])
        nodes, edges = {t}, set(se) | set(te)
        n = t
        while n in parent:
            eid, n = parent[n]
            edges.add(eid)
            nodes.add(n)
        res = self._collect(nodes, edges)
        res.extra = {"length": dist[t]}
        res.message = "Path length: %.2f map units, %d feature(s)." % (
            dist[t],
            res.count(),
        )
        return res

    def _trace_loops(self, sn, se, blocked, bedges):
        """Edges that are part of a loop (non-bridge edges) in the connected
        area."""
        if sn or se:
            _area_n, area_e = self._bfs(sn, se, blocked, bedges)
        else:
            area_e = set(range(len(self.edges)))
        bridges = self.bridges(area_e)
        loop_e = area_e - bridges
        nodes = {self.edges[e][k] for e in loop_e for k in ("a", "b")}
        res = self._collect(set(), loop_e)
        res.extra = {"loop_nodes": len(nodes)}
        res.message = "%d line feature(s) are part of loops." % len(res.lines)
        return res

    def bridges(self, edge_set):
        """Tarjan bridge finding (iterative, handles parallel edges)."""
        adj = defaultdict(list)
        for eid in edge_set:
            e = self.edges[eid]
            if e["a"] == e["b"]:
                continue
            adj[e["a"]].append((eid, e["b"]))
            adj[e["b"]].append((eid, e["a"]))
        disc, low, out = {}, {}, set()
        timer = 0
        for root in list(adj):
            if root in disc:
                continue
            disc[root] = low[root] = timer
            timer += 1
            stack = [(root, None, iter(adj[root]))]
            while stack:
                n, pe, it = stack[-1]
                advanced = False
                for eid, m in it:
                    if eid == pe:
                        continue
                    if m in disc:
                        low[n] = min(low[n], disc[m])
                    else:
                        disc[m] = low[m] = timer
                        timer += 1
                        stack.append((m, eid, iter(adj[m])))
                        advanced = True
                        break
                if not advanced:
                    stack.pop()
                    if stack:
                        p = stack[-1][0]
                        low[p] = min(low[p], low[n])
                        if low[n] > disc[p]:
                            out.add(pe)
        return out

    def _trace_subnetwork(self, sn, se, blocked, bedges):
        subs = self.subnetworks()
        names = set()
        keys = self._collect(sn, se)
        for k in keys.lines | keys.points:
            names |= subs["by_key"].get(k, set())
        res = TraceResult()
        for name in names:
            info = subs["subnetworks"][name]
            res.lines |= info["lines"]
            res.points |= info["points"]
        res.extra = {"names": sorted(names)}
        res.message = (
            "Subnetwork(s): %s - %d feature(s)."
            % (", ".join(sorted(names)), res.count())
            if names
            else "The start is not inside any subnetwork."
        )
        return res

    def _trace_controllers(self, sn, se, blocked, bedges):
        """Subnetwork controller trace: the controllers feeding the start."""
        subs = self.subnetworks()
        keys = self._collect(sn, se)
        names = set()
        for k in keys.lines | keys.points:
            names |= subs["by_key"].get(k, set())
        res = TraceResult()
        for n in names:
            res.points.add(subs["subnetworks"][n]["controller"])
        res.extra = {"names": sorted(names)}
        res.message = (
            "%d controller(s) feed the start: %s."
            % (len(res.points), ", ".join(sorted(names)))
            if names
            else "No controller feeds the start."
        )
        return res

    # ------------------------------------------------------------ subnetworks
    def subnetworks(self, name_of=None):
        """Every controller feeds the area reachable through lines of its
        tier."""
        closed = self.closed_nodes()
        controllers = self.controller_points()
        ctrl_nodes = {self.point_node[pi] for pi in controllers}
        out = {"subnetworks": {}, "by_key": defaultdict(set)}
        for pi in controllers:
            p = self.points[pi]
            tier = self.tiers.get((p["key"][0], p["ag"]))
            name = (
                self.controllers.get(p["key"])
                or (name_of(p) if name_of else None)
                or p.get("assetid")
                or "%s-%s" % p["key"]
            )

            def ok(eid, tier=tier):
                e = self.edges[eid]
                if e["line"] is None:
                    return True
                lt = self.tiers.get(
                    (
                        self.lines[e["line"]]["key"][0],
                        self.lines[e["line"]]["ag"],
                    )
                )
                return tier is None or lt is None or lt == tier

            start = self.point_node[pi]
            nodes, edges = self._bfs(
                {start}, (), (closed | ctrl_nodes) - {start}, (), ok
            )
            r = self._collect(nodes, edges)
            info = out["subnetworks"].setdefault(
                name,
                {
                    "lines": set(),
                    "points": set(),
                    "tier": tier,
                    "controller": p["key"],
                    "customers": 0,
                },
            )
            info["lines"] |= r.lines
            info["points"] |= r.points
            for k in r.lines | r.points:
                out["by_key"][k].add(name)
        cust = {
            self.points[pi]["key"]
            for pi in range(len(self.points))
            if "customer" in self.point_cats(pi)
        }
        for info in out["subnetworks"].values():
            info["customers"] = len(info["points"] & cust)
        return out

    def subnetwork_issues(self, subs=None):
        """E14: fed by more than one controller where the tier does not allow
        it;
        E16: features of a tier that requires a controller but have none."""
        subs = subs or self.subnetworks()
        out = []
        tier_of = {n: i["tier"] for n, i in subs["subnetworks"].items()}
        pos = {}
        for p in self.points:
            pos[p["key"]] = (p["x"], p["y"], "point")
        for ln in self.lines:
            if ln["parts"] and ln["parts"][0]:
                pos[ln["key"]] = (
                    ln["parts"][0][0][0],
                    ln["parts"][0][0][1],
                    "line",
                )
        for key, names in subs["by_key"].items():
            if len(names) < 2:
                continue
            tiers = {tier_of.get(n) for n in names}
            # no tier = one system (e.g. several tanks feeding one looped water
            # network): allowed
            if all(
                t is None or self.tier_settings.get(t, {}).get("multi", False)
                for t in tiers
            ):
                continue
            x, y, g = pos.get(key, (0, 0, "point"))
            out.append(
                {
                    "code": "E14",
                    "severity": "error",
                    "message": (
                        "Fed by more than one controller: %s"
                        % ", ".join(sorted(names))
                    ),
                    "x": x,
                    "y": y,
                    "key": key,
                    "geom": g,
                }
            )
        required = {
            t for t, s in self.tier_settings.items() if s.get("require")
        }
        if required:
            for ln in self.lines:
                t = self.tiers.get((ln["key"][0], ln["ag"]))
                if (
                    t in required
                    and ln["key"] not in subs["by_key"]
                    and ln["key"] in pos
                ):
                    x, y, g = pos[ln["key"]]
                    out.append(
                        {
                            "code": "E16",
                            "severity": "warning",
                            "message": (
                                "No controller feeds this feature (tier %s)"
                                % t
                            ),
                            "x": x,
                            "y": y,
                            "key": ln["key"],
                            "geom": "line",
                        }
                    )
        return out

    # ------------------------------------------------------------- validation
    def validate(
        self, rules=None, check_dangles=True, check_islands=True, names=None
    ):
        """List of issues: dict(code, severity, message, x, y, key,
        geom='point'|'line')."""
        issues = []
        tol = self.tol

        def add(code, sev, msg, x, y, key, geom="point"):
            issues.append(
                {
                    "code": code,
                    "severity": sev,
                    "message": msg,
                    "x": x,
                    "y": y,
                    "key": key,
                    "geom": geom,
                }
            )

        # geometry problems
        for li, ln in enumerate(self.lines):
            parts = [p for p in ln["parts"] if len(p) >= 2]
            if not parts:
                add(
                    "E09",
                    "error",
                    "Line without a valid geometry",
                    0,
                    0,
                    ln["key"],
                    "line",
                )
                continue
            if len(ln["parts"]) > 1:
                x, y = parts[0][0]
                add(
                    "E09",
                    "warning",
                    "Multipart line (each part is treated separately)",
                    x,
                    y,
                    ln["key"],
                    "line",
                )
            if (
                sum(
                    self.edges[e]["length"]
                    for e in self.line_edges.get(li, ())
                )
                <= tol
            ):
                x, y = parts[0][0]
                add(
                    "E09", "error", "Zero-length line", x, y, ln["key"], "line"
                )
            if ln.get("ag") is None:
                x, y = parts[0][0]
                add(
                    "E06",
                    "error",
                    "Asset group is empty",
                    x,
                    y,
                    ln["key"],
                    "line",
                )
        for p in self.points:
            if p.get("ag") is None:
                add(
                    "E06",
                    "error",
                    "Asset group is empty",
                    p["x"],
                    p["y"],
                    p["key"],
                )

        # unconnected ends / points, missing vertices, gaps
        ev_tokens = {t for t, _l in getattr(self, "_endvertex_hits", [])}
        for token, (x, y), own_line in self._candidates:
            if self._partners.get(token) or token in ev_tokens:
                continue
            is_point = token[0] == "P"
            key = (
                self.points[token[1]]["key"]
                if is_point
                else self.lines[token[1]]["key"]
            )
            on_segment = None
            gap_d = INF
            for x1, y1, x2, y2, (li, pt, _s) in self._sgrid.near(x, y):
                if not is_point and li == own_line and pt == token[2]:
                    continue
                d, t = seg_distance(x, y, x1, y1, x2, y2)
                if d <= tol and 0 < t < 1:
                    on_segment = self.lines[li]["key"]
                gap_d = min(gap_d, d)
            for px, py, _i in self._near_points(x, y):
                if is_point and _i == token[1]:
                    continue
                gap_d = min(gap_d, math.hypot(px - x, py - y))
            if on_segment is not None:
                add(
                    "E03",
                    "error",
                    "Touches line %s:%s between vertices (needs a vertex /"
                    " split)" % on_segment,
                    x,
                    y,
                    key,
                )
            elif (
                gap_d <= tol
                and not is_point
                and any(ln.get("levels") for ln in self.lines[:1])
            ):
                add(
                    "E21",
                    "warning",
                    "Meets another line end at a different elevation level"
                    " (F_ELEV / T_ELEV): not connected",
                    x,
                    y,
                    key,
                )
            elif gap_d <= self.gap:
                add(
                    "E07",
                    "error",
                    "Gap of %.3f to the nearest feature (not connected)"
                    % gap_d,
                    x,
                    y,
                    key,
                )
            elif is_point:
                cats = self.point_cats(token[1])
                if "structure" not in cats:
                    add(
                        "E02",
                        "error",
                        "Point is not connected to any line",
                        x,
                        y,
                        key,
                    )
            elif check_dangles and self.flow != "undirected":
                add("E01", "warning", "Dangling line end", x, y, key)

        for token, li in getattr(self, "_endvertex_hits", []):
            if token[0] == "P":
                p = self.points[token[1]]
                x, y, key = p["x"], p["y"], p["key"]
            else:
                x, y = self.lines[token[1]]["parts"][token[2]][token[3]]
                key = self.lines[token[1]]["key"]
            add(
                "E15",
                "error",
                "Connects in the middle of %s:%s, which only connects at its"
                " ends"
                % self.lines[li]["key"],
                x,
                y,
                key,
            )

        # lines attached to a device with terminals but matching none of them
        for pi, li in getattr(self, "terminal_issues", []):
            p = self.points[pi]
            add(
                "E11",
                "error",
                "Line tier matches no terminal of the device (check the line"
                " asset group)",
                p["x"],
                p["y"],
                self.lines[li]["key"],
            )

        # size changes where two lines meet without a fitting (pipes / cables
        # only, not roads)
        for n in (
            range(len(self.node_xy)) if self.flow != "undirected" else ()
        ):
            if self.node_points[n]:
                continue
            sizes = {}
            for eid, _m in self.adj[n]:
                li = self.edges[eid]["line"]
                if li is not None and self.lines[li].get("size") is not None:
                    if "service" in self.line_cats(li):
                        continue
                    sizes[li] = self.lines[li]["size"]
            if len(set(sizes.values())) > 1:
                x, y = self.node_xy[n]
                add(
                    "E12",
                    "warning",
                    "Size changes (%s) without a fitting / reducer"
                    % " / ".join(
                        "%g" % v for v in sorted(set(sizes.values()))
                    ),
                    x,
                    y,
                    self.lines[next(iter(sizes))]["key"],
                )

        # stacked devices
        for n, pts in enumerate(self.node_points):
            devs = [pi for pi in pts if "structure" not in self.point_cats(pi)]
            if len(devs) > 1:
                x, y = self.node_xy[n]
                add(
                    "E08",
                    "warning",
                    "%d point features at the same location" % len(devs),
                    x,
                    y,
                    self.points[devs[0]]["key"],
                )

        # connectivity rules
        if rules:
            seen = set()
            for n in range(len(self.node_xy)):
                groups = []
                for eid, _m in self.adj[n]:
                    li = self.edges[eid]["line"]
                    if li is not None:
                        groups.append(
                            (
                                "L",
                                li,
                                (
                                    self.lines[li]["key"][0],
                                    self.lines[li]["ag"],
                                ),
                            )
                        )
                for pi in self.node_points[n]:
                    if "structure" not in self.point_cats(pi):
                        groups.append(
                            (
                                "P",
                                pi,
                                (
                                    self.points[pi]["key"][0],
                                    self.points[pi]["ag"],
                                ),
                            )
                        )
                has_point = any(k == "P" for k, _i, _g in groups)
                for i, (ka, ia, ga) in enumerate(groups):
                    for kb, ib, gb in groups[i + 1:]:
                        if (ka, ia) == (kb, ib) or (ka == "P" and kb == "P"):
                            continue
                        if ka == "L" and kb == "L" and has_point:
                            # lines meet through the junction / device there
                            continue
                        if ga[1] is None or gb[1] is None:
                            continue
                        pair = tuple(sorted((ga, gb), key=str))
                        if pair in rules or (n, pair) in seen:
                            continue
                        seen.add((n, pair))
                        x, y = self.node_xy[n]

                        def nm(g):
                            return "%s: %s" % (
                                g[0],
                                (names or {}).get(g, g[1]),
                            )

                        add(
                            "E05",
                            "error",
                            "No connectivity rule between %s and %s"
                            % (nm(ga), nm(gb)),
                            x,
                            y,
                            (
                                self.lines[ia]["key"]
                                if ka == "L"
                                else self.points[ia]["key"]
                            ),
                        )

        # islands without any source
        if check_islands and self.flow == "source":
            sources = self.source_nodes()
            seen_n = set()
            for n in list(self.adj):
                if n in seen_n:
                    continue
                comp_n, comp_e = self._bfs({n}, ())
                seen_n |= comp_n
                if comp_n & sources:
                    continue
                keys = {self.edge_key(e) for e in comp_e} - {None}
                for li, ln in enumerate(self.lines):
                    if ln["key"] in keys:
                        x, y = ln["parts"][0][0]
                        add(
                            "E10",
                            "warning",
                            "Not fed by any source (isolated island, %d lines)"
                            % len(keys),
                            x,
                            y,
                            ln["key"],
                            "line",
                        )
        return issues

    def _near_points(self, x, y):
        return list(self._gap_grid.near(x, y, self.gap))

    def learn_rules(self):
        """Connectivity rules found in the data: set of ((cls, ag), (cls,
        ag))."""
        found = set()
        for n in range(len(self.node_xy)):
            groups = set()
            for eid, _m in self.adj[n]:
                li = self.edges[eid]["line"]
                if li is not None and self.lines[li]["ag"] is not None:
                    groups.add(
                        (
                            "L",
                            li,
                            (self.lines[li]["key"][0], self.lines[li]["ag"]),
                        )
                    )
            for pi in self.node_points[n]:
                if self.points[pi][
                    "ag"
                ] is not None and "structure" not in self.point_cats(pi):
                    groups.add(
                        (
                            "P",
                            pi,
                            (self.points[pi]["key"][0], self.points[pi]["ag"]),
                        )
                    )
            groups = list(groups)
            has_point = any(k == "P" for k, _i, _g in groups)
            for i, (ka, ia, ga) in enumerate(groups):
                for kb, ib, gb in groups[i + 1:]:
                    if (ka == "P" and kb == "P") or (ka, ia) == (kb, ib):
                        continue
                    if ka == "L" and kb == "L" and has_point:
                        continue
                    found.add(tuple(sorted((ga, gb), key=str)))
        return found
