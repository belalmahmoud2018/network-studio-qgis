"""Steady-state hydraulic solver for pressure networks (water, district
cooling).

Pure Python + numpy, no QGIS: the Global Gradient Algorithm of Todini and
Pilati, the method EPANET uses, with Hazen-Williams head losses.

Model (built by hydraulics.build_model):
    junctions  {node: (elevation m, base demand L/s)}
    fixed      {node: total head m}          reservoirs, tanks, pumps
    pipes      [{"a", "b", "length" m, "diameter" mm, "c", "open"}]

Units: lengths and heads in m, diameters in mm, flows in L/s (m3/s inside).
The linear system is solved with scipy when QGIS has it, otherwise with a
preconditioned conjugate gradient in numpy.
"""

import math

import numpy as np

N_HW = 1.852
K_HW = 10.667  # SI Hazen-Williams constant (head loss in m, Q in m3/s)
G_MIN = 1e-4  # smallest head loss gradient: only changes the step size


class HydraulicError(Exception):
    pass


def _solver():
    try:
        from scipy.sparse import csr_matrix
        from scipy.sparse.linalg import spsolve

        def solve(n, rows, cols, vals, rhs):
            a = csr_matrix((vals, (rows, cols)), shape=(n, n))
            return spsolve(a.tocsc(), rhs)

        return solve
    except ImportError:
        return _cg


def _cg(n, rows, cols, vals, rhs, tol=1e-10, max_iter=20000):
    """Jacobi preconditioned conjugate gradient (the matrix is symmetric
    positive definite)."""
    rows = np.asarray(rows)
    cols = np.asarray(cols)
    vals = np.asarray(vals, dtype=float)
    diag = np.bincount(rows[rows == cols], vals[rows == cols], minlength=n)
    diag[diag == 0] = 1.0
    inv = 1.0 / diag

    def mv(x):
        return np.bincount(rows, vals * x[cols], minlength=n)

    x = rhs * inv
    r = rhs - mv(x)
    z = r * inv
    p = z.copy()
    rz = r @ z
    norm = max(np.linalg.norm(rhs), 1e-30)
    for _i in range(max_iter):
        ap = mv(p)
        alpha = rz / max(p @ ap, 1e-300)
        x += alpha * p
        r -= alpha * ap
        if np.linalg.norm(r) / norm < tol:
            break
        z = r * inv
        rz2 = r @ z
        p = z + (rz2 / rz) * p
        rz = rz2
    return x


def resistance(length, diameter_mm, c):
    d = max(diameter_mm, 1.0) / 1000.0
    return K_HW * max(length, 0.01) / (max(c, 1.0) ** N_HW * d**4.871)


class Result:
    def __init__(self):
        self.head = {}  # node -> m
        self.pressure = {}  # node -> m
        self.demand = {}  # node -> L/s
        self.flow = []  # per pipe, L/s (a -> b positive), None when closed
        self.velocity = []  # m/s
        self.headloss = []  # m/km
        self.iterations = 0
        self.converged = False
        self.unfed = set()  # junctions no fixed head can reach

    def summary(self):
        skip = getattr(self, "fixed_nodes", set())
        p = [
            v
            for n, v in self.pressure.items()
            if v is not None and n not in skip
        ]
        v = [abs(x) for x in self.velocity if x is not None]
        pu, qu = getattr(self, "units", ("m", "L/s"))
        return {
            "iterations": self.iterations,
            "converged": self.converged,
            "nodes": len(p),
            "pipes": sum(1 for f in self.flow if f is not None),
            "min pressure (%s)" % pu: round(min(p), 3) if p else None,
            "max pressure (%s)" % pu: round(max(p), 3) if p else None,
            "max velocity (m/s)": round(max(v), 2) if v else None,
            "total demand (%s)" % qu: round(sum(self.demand.values()), 3),
            "junctions without a source": len(self.unfed),
        }


GAS_LAWS = ("gas_lp", "gas_mp")


def _qfac(model):
    """Model flow unit -> m3/s (water L/s, gas m3/h at standard
    conditions)."""
    return 1.0 / 3600.0 if model.get("law") in GAS_LAWS else 1e-3


def _gas_k(model, length, diameter_mm):
    """Geometric part of the gas pressure loss (times f gives r)."""
    g = model.get("gas", {})
    d = max(diameter_mm, 1.0) / 1000.0
    rho_n = g.get("rho_n", 0.73)
    if model["law"] == "gas_lp":  # dP = 8 f L rho Q2 / (pi2 D5), Pa
        return 8.0 * length * rho_n / (math.pi**2 * d**5)
    # P1^2 - P2^2 = 16 f L rho_n Pn Z T Q2 / (pi2 D5 Tn), Pa^2
    return (
        16.0
        * length
        * rho_n
        * g.get("p_n", 101325.0)
        * g.get("z", 1.0)
        * g.get("t", 288.15)
        / (math.pi**2 * d**5 * g.get("t_n", 288.15))
    )


def _friction(model, q, d_m, eps_m):
    """Darcy friction factor (laminar 64/Re, Swamee-Jain turbulent)."""
    g = model.get("gas", {})
    re = (
        4.0
        * g.get("rho_n", 0.73)
        * np.abs(q)
        / (math.pi * d_m * g.get("mu", 1.1e-5))
    )
    re = np.maximum(re, 1.0)
    turb = 0.25 / np.log10(eps_m / (3.7 * d_m) + 5.74 / re**0.9) ** 2
    return np.where(re < 2000.0, 64.0 / re, turb)


def _solve_once(
    model,
    demand_factor=1.0,
    extra_demand=None,
    c_factor=None,
    accuracy=1e-4,
    max_iter=200,
):
    """One Global Gradient solution of the links in model["pipes"]
    (kind 'pipe' Hazen-Williams or Darcy for gas, 'pump' with a curve
    H = h0 - r Q2 and a check valve)."""
    junctions = model["junctions"]
    fixed = model["fixed"]
    pipes = model["pipes"]
    gas = model.get("law") in GAS_LAWS
    qf = _qfac(model)
    if not fixed:
        raise HydraulicError(
            "The model has no source (reservoir, tank, pump or regulator):"
            " nothing feeds the network."
        )
    extra_demand = extra_demand or {}
    closed_pumps = set()
    for _status in range(6):
        adj = {}
        for k, p in enumerate(pipes):
            if p["open"] and k not in closed_pumps:
                adj.setdefault(p["a"], []).append(p["b"])
                adj.setdefault(p["b"], []).append(p["a"])
        seen = set(fixed)
        stack = list(fixed)
        while stack:
            n = stack.pop()
            for m in adj.get(n, ()):
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        jlist = [n for n in junctions if n in seen and n not in fixed]
        jidx = {n: i for i, n in enumerate(jlist)}
        nj = len(jlist)
        res = Result()
        res.unfed = {n for n in junctions if n not in seen and n not in fixed}
        act = [
            k
            for k, p in enumerate(pipes)
            if p["open"]
            and k not in closed_pumps
            and p["a"] != p["b"]
            and (p["a"] in jidx or p["a"] in fixed)
            and (p["b"] in jidx or p["b"] in fixed)
        ]
        m = len(act)
        ia = np.array(
            [jidx.get(pipes[k]["a"], -1) for k in act], dtype=np.int64
        )
        ib = np.array(
            [jidx.get(pipes[k]["b"], -1) for k in act], dtype=np.int64
        )
        ha = np.array([fixed.get(pipes[k]["a"], 0.0) for k in act])
        hb = np.array([fixed.get(pipes[k]["b"], 0.0) for k in act])
        cf = (
            np.array([c_factor[k] if k < len(c_factor) else 1.0 for k in act])
            if c_factor is not None
            else np.ones(m)
        )
        is_pump = np.array(
            [pipes[k].get("kind") == "pump" for k in act], dtype=bool
        )
        h0 = np.array([pipes[k].get("h0", 0.0) for k in act])
        rp = np.array([pipes[k].get("rp", 0.0) for k in act])
        dia = np.array(
            [max(pipes[k].get("diameter", 100.0), 1.0) / 1000.0 for k in act]
        )
        if gas:
            kgeo = np.array(
                [
                    _gas_k(model, pipes[k]["length"], pipes[k]["diameter"])
                    for k in act
                ]
            )
            eps = (
                np.array([pipes[k].get("eps", 0.05) / 1000.0 for k in act])
                * cf
            )
            r_hw = np.zeros(m)
        else:
            r_hw = np.array(
                [
                    (
                        resistance(
                            pipes[k]["length"],
                            pipes[k]["diameter"],
                            pipes[k]["c"] * f,
                        )
                        if pipes[k].get("kind") != "pump"
                        else 0.0
                    )
                    for k, f in zip(act, cf)
                ]
            )
        area = math.pi * dia**2 / 4.0
        dem = np.zeros(nj)
        for n, i in jidx.items():
            dem[i] = (
                junctions[n][1] * demand_factor + extra_demand.get(n, 0.0)
            ) * qf
        q = area * (5.0 if gas else 0.3048)
        q = np.where(is_pump, np.maximum(q, 1e-3), q)
        solve_lin = _solver()
        a_j = ia >= 0
        b_j = ib >= 0
        both = a_j & b_j
        h = np.zeros(nj)
        for it in range(1, max_iter + 1):
            aq = np.abs(q)
            if gas:
                r = kgeo * _friction(model, q, dia, eps)
                hl = r * aq * q
                g = 2.0 * r * aq
            else:
                hl = r_hw * aq**N_HW * np.sign(q)
                g = N_HW * r_hw * aq ** (N_HW - 1.0)
            hl = np.where(is_pump, rp * aq * q - h0, hl)
            g = np.where(is_pump, 2.0 * rp * aq, g)
            gmin = (
                G_MIN
                if not gas
                else 1e-9 * max(float(np.max(np.abs(hl))) if m else 1.0, 1.0)
            )
            g = np.maximum(g, gmin)
            p = 1.0 / g
            c = q - p * hl
            rhs = -dem.copy()
            np.add.at(rhs, ia[a_j], -c[a_j] + p[a_j] * hb[a_j] * ~b_j[a_j])
            np.add.at(rhs, ib[b_j], c[b_j] + p[b_j] * ha[b_j] * ~a_j[b_j])
            rows = np.concatenate([ia[a_j], ib[b_j], ia[both], ib[both]])
            cols = np.concatenate([ia[a_j], ib[b_j], ib[both], ia[both]])
            vals = np.concatenate([p[a_j], p[b_j], -p[both], -p[both]])
            if nj:
                h = np.asarray(
                    solve_lin(nj, rows, cols, vals, rhs), dtype=float
                )
            h_a = np.where(a_j, h[np.maximum(ia, 0)] if nj else 0.0, ha)
            h_b = np.where(b_j, h[np.maximum(ib, 0)] if nj else 0.0, hb)
            q_new = c + p * (h_a - h_b)
            change = np.abs(q_new - q).sum() / max(np.abs(q_new).sum(), 1e-12)
            q = q_new
            res.iterations = it
            if change < accuracy:
                res.converged = True
                break
        back = [act[j] for j in range(m) if is_pump[j] and q[j] < -1e-9]
        if not back:
            break
        closed_pumps.update(back)  # check valve: no flow back
    if not np.all(np.isfinite(h)):
        raise HydraulicError("The hydraulic solution did not converge.")
    for n, i in jidx.items():
        res.head[n] = float(h[i])
        res.pressure[n] = to_pressure(model, float(h[i]), junctions[n][0])
        res.demand[n] = float(dem[i] / qf)
    for n, head in fixed.items():
        res.head[n] = head
        elev = junctions.get(n, (model.get("fixed_elev", {}).get(n, 0.0),))[0]
        res.pressure[n] = to_pressure(model, head, elev)
    res.flow = [None] * len(pipes)
    res.velocity = [None] * len(pipes)
    res.headloss = [None] * len(pipes)
    for j, k in enumerate(act):
        res.flow[k] = float(q[j] / qf)
        res.velocity[k] = float(q[j] / area[j])
        if is_pump[j]:
            res.headloss[k] = float(-(h0[j] - rp[j] * q[j] * abs(q[j])))
        elif gas:
            res.headloss[k] = float(h_a[j] - h_b[j])
        else:
            res.headloss[k] = float(
                r_hw[j]
                * abs(q[j]) ** N_HW
                / max(pipes[k]["length"], 0.01)
                * 1000.0
            )
    res.closed_pumps = closed_pumps
    res.fixed_nodes = set(fixed)
    res.units = {
        "gas_lp": ("mbar", "m3/h"),
        "gas_mp": ("bar", "m3/h"),
    }.get(model.get("law"), ("m", "L/s"))
    return res


def to_pressure(model, head, elev):
    """Head variable -> pressure: water m, gas low pressure mbar, gas medium
    pressure bar (gauge)."""
    law = model.get("law")
    if law == "gas_lp":
        return head / 100.0
    if law == "gas_mp":
        patm = model.get("gas", {}).get("p_atm", 101325.0)
        return (math.sqrt(max(head, 0.0)) - patm) / 1e5
    return head - elev


def from_pressure(model, pressure, elev=0.0):
    law = model.get("law")
    if law == "gas_lp":
        return pressure * 100.0
    if law == "gas_mp":
        patm = model.get("gas", {}).get("p_atm", 101325.0)
        return (pressure * 1e5 + patm) ** 2
    return pressure + elev


def solve(
    model,
    demand_factor=1.0,
    extra_demand=None,
    c_factor=None,
    accuracy=1e-4,
    max_iter=200,
):
    """Solve the network, with the pressure reducing valves of
    model["valves"] (active: fixed outlet pressure, open, or closed against
    back flow). extra_demand {node: flow} (fire flow), c_factor [per pipe]
    multiplies the roughness (calibration)."""
    valves = model.get("valves") or []
    if not valves:
        return _solve_once(
            model, demand_factor, extra_demand, c_factor, accuracy, max_iter
        )
    state = ["active"] * len(valves)
    qv = [0.0] * len(valves)
    res = None
    for _outer in range(40):
        fixed = dict(model["fixed"])
        pipes = list(model["pipes"])
        extra = dict(extra_demand or {})
        idx = {}
        for i, v in enumerate(valves):
            if state[i] == "active":
                fixed[v["b"]] = v["head"]
                extra[v["a"]] = extra.get(v["a"], 0.0) + qv[i]
            elif state[i] == "open":
                idx[i] = len(pipes)
                pipes.append(
                    {
                        "a": v["a"],
                        "b": v["b"],
                        "length": 1.0,
                        "diameter": v.get("diameter", 150.0),
                        "c": 140.0,
                        "eps": 0.01,
                        "open": True,
                    }
                )
        m2 = dict(model)
        m2["fixed"] = fixed
        m2["pipes"] = pipes
        res = _solve_once(
            m2, demand_factor, extra, c_factor, accuracy, max_iter
        )
        changed = False
        for i, v in enumerate(valves):
            ha = res.head.get(v["a"])
            tol = max(abs(v["head"]) * 1e-4, 0.01)
            if state[i] == "active":
                out = 0.0
                for k, p in enumerate(pipes):
                    f = res.flow[k]
                    if f is None:
                        continue
                    if p["a"] == v["b"]:
                        out += f
                    elif p["b"] == v["b"]:
                        out -= f
                if ha is not None and ha < v["head"] - tol:
                    state[i], changed = "open", True
                elif out < -1e-6:
                    state[i], changed = "closed", True
                    qv[i] = 0.0
                else:
                    if abs(out - qv[i]) > 1e-4 * max(1.0, abs(out)):
                        changed = True
                    qv[i] = out
            elif state[i] == "open":
                f = res.flow[idx[i]]
                hb = res.head.get(v["b"])
                if f is not None and f < -1e-6:
                    state[i], changed = "closed", True
                elif hb is not None and hb > v["head"] + tol:
                    state[i], changed = "active", True
                    qv[i] = max(f or 0.0, 0.0)
            else:
                if ha is not None and ha > v["head"] + tol:
                    state[i], changed = "active", True
        if not changed:
            break
    res.flow = res.flow[: len(model["pipes"])]
    res.velocity = res.velocity[: len(model["pipes"])]
    res.headloss = res.headloss[: len(model["pipes"])]
    res.valves = [
        {"a": v["a"], "b": v["b"], "status": st, "flow": q}
        for v, st, q in zip(valves, state, qv)
    ]
    return res


def simulate(
    model,
    hours=24,
    pattern=None,
    demand_factor=1.0,
    step=1.0,
    feedback=None,
):
    """Extended period simulation: one solution per time step with the
    demand pattern; tank levels follow their in / out flow.

    Returns {"times", "min_pressure" {node}, "max_pressure" {node},
    "tank_levels" {node: [level per step]}, "total_demand" [per step],
    "lowest" [lowest pressure per step], "warnings"}."""
    pattern = pattern or [1.0]
    tanks = dict(model.get("tanks") or {})
    info = model.get("tank_info") or {}
    level = {n: lv for n, (_e, lv) in tanks.items()}
    qf = _qfac(model)
    out = {
        "times": [],
        "min_pressure": {},
        "max_pressure": {},
        "tank_levels": {n: [] for n in tanks},
        "total_demand": [],
        "lowest": [],
        "warnings": [],
    }
    steps = int(round(hours / step))
    for s in range(steps + 1):
        t = s * step
        mult = pattern[int(t) % len(pattern)]
        m2 = dict(model)
        fixed = dict(model["fixed"])
        for n, (elev, _lv) in tanks.items():
            fixed[n] = elev + level[n]
        m2["fixed"] = fixed
        res = solve(m2, demand_factor=demand_factor * mult)
        out["times"].append(t)
        low = None
        for n, pr in res.pressure.items():
            if n in model["fixed"] or pr is None:
                continue
            a = out["min_pressure"].get(n)
            out["min_pressure"][n] = pr if a is None else min(a, pr)
            b = out["max_pressure"].get(n)
            out["max_pressure"][n] = pr if b is None else max(b, pr)
            low = pr if low is None else min(low, pr)
        out["lowest"].append(low)
        out["total_demand"].append(sum(res.demand.values()))
        for n in tanks:
            out["tank_levels"][n].append(level[n])
            inflow = 0.0
            for k, p in enumerate(model["pipes"]):
                f = res.flow[k]
                if f is None:
                    continue
                if p["b"] == n:
                    inflow += f
                elif p["a"] == n:
                    inflow -= f
            ti = info.get(n, {})
            area = math.pi * ti.get("diameter", 20.0) ** 2 / 4.0
            new = level[n] + inflow * qf * step * 3600.0 / area
            lo, hi = ti.get("min", 0.0), ti.get("max", 2 * tanks[n][1] or 10)
            if new < lo:
                out["warnings"].append(
                    "Tank N%d empty at hour %.1f" % (n, t + step)
                )
                new = lo
            elif new > hi:
                new = hi
            level[n] = new
        if feedback and feedback(s, steps):
            break
    return out


def available_fire_flow(static_p, residual_p, test_flow, min_residual):
    """Fire flow available at `min_residual` pressure from one test (the
    classic hydrant flow test formula, Q ~ (dP)^0.54)."""
    if static_p is None or residual_p is None:
        return None
    drop = static_p - residual_p
    if static_p <= min_residual:
        return 0.0
    if drop <= 1e-6:
        return None  # more than the test flow, no measurable drop
    return test_flow * ((static_p - min_residual) / drop) ** 0.54


def calibrate(
    model,
    observations,
    groups,
    demand=False,
    c_min=40.0,
    c_max=160.0,
    max_rounds=15,
    flow_weight=1.0,
    feedback=None,
):
    """Adjust the Hazen-Williams C of pipe groups (and optionally one demand
    factor) so the computed pressures / flows match field measurements.

    observations: [("pressure", node, value m) | ("flow", pipe index, L/s)]
    groups: [group label per pipe] (None = not calibrated)
    Levenberg-Marquardt on the squared errors, finite difference Jacobian.
    Returns a dict with the factors, C per group and the error statistics."""
    pipes = model["pipes"]
    labels = sorted({g for g in groups if g is not None}, key=str)
    if not observations:
        raise HydraulicError("No measurements to calibrate against.")
    if not labels and not demand:
        raise HydraulicError("Nothing to calibrate: no pipe group selected.")
    base_c = {}
    for lab in labels:
        cs = [p["c"] for p, g in zip(pipes, groups) if g == lab]
        base_c[lab] = sum(cs) / len(cs)
    npar = len(labels) + (1 if demand else 0)
    x = np.ones(npar)

    def unpack(x):
        fac = {lab: x[i] for i, lab in enumerate(labels)}
        cf = [fac.get(g, 1.0) if g is not None else 1.0 for g in groups]
        df = x[-1] if demand else 1.0
        return cf, df

    def bounds(x):
        x = x.copy()
        for i, lab in enumerate(labels):
            x[i] = min(max(x[i], c_min / base_c[lab]), c_max / base_c[lab])
        if demand:
            x[-1] = min(max(x[-1], 0.2), 5.0)
        return x

    def residuals(x):
        cf, df = unpack(x)
        res = solve(model, demand_factor=df, c_factor=cf)
        out = []
        for kind, ref, val in observations:
            if kind == "pressure":
                sim = res.pressure.get(ref)
                out.append((sim if sim is not None else 0.0) - val)
            else:
                sim = res.flow[ref] if 0 <= ref < len(res.flow) else None
                out.append(
                    ((abs(sim) if sim is not None else 0.0) - abs(val))
                    * flow_weight
                )
        return np.array(out), res

    r0, res0 = residuals(x)
    before = _stats(observations, res0)
    lam = 1e-2
    cur = r0 @ r0
    for rnd in range(max_rounds):
        if feedback:
            feedback(rnd + 1, max_rounds)
        jac = np.zeros((len(r0), npar))
        for i in range(npar):
            step = 0.02 * max(abs(x[i]), 0.1)
            xp = x.copy()
            xp[i] += step
            rp, _r = residuals(bounds(xp))
            jac[:, i] = (rp - r0) / step
        jtj = jac.T @ jac
        grad = jac.T @ r0
        improved = False
        for _t in range(8):
            try:
                dx = np.linalg.solve(
                    jtj + lam * np.diag(np.maximum(np.diag(jtj), 1e-9)), -grad
                )
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            xn = bounds(x + dx)
            rn, _r = residuals(xn)
            if rn @ rn < cur:
                x, r0, cur = xn, rn, rn @ rn
                lam = max(lam / 3.0, 1e-6)
                improved = True
                break
            lam *= 4.0
        if not improved or np.abs(dx).max() < 1e-4:
            break
    cf, df = unpack(x)
    cf = [float(v) for v in cf]
    _r, res = residuals(x)
    after = _stats(observations, res)
    return {
        "c": {lab: float(base_c[lab] * x[i]) for i, lab in enumerate(labels)},
        "c_before": dict(base_c),
        "demand_factor": float(df),
        "c_factor": cf,
        "before": before,
        "after": after,
        "result": res,
        "result_before": res0,
    }


def _stats(observations, res):
    rows = []
    for kind, ref, val in observations:
        if kind == "pressure":
            sim = res.pressure.get(ref)
        else:
            sim = res.flow[ref] if 0 <= ref < len(res.flow) else None
            sim = abs(sim) if sim is not None else None
            val = abs(val)
        rows.append((kind, ref, val, sim))
    errs = [s - v for _k, _r, v, s in rows if s is not None]
    obs = [v for _k, _r, v, s in rows if s is not None]
    sims = [s for _k, _r, v, s in rows if s is not None]
    out = {"rows": rows}
    if errs:
        out["rmse"] = math.sqrt(sum(e * e for e in errs) / len(errs))
        out["mean abs error"] = sum(abs(e) for e in errs) / len(errs)
        out["max abs error"] = max(abs(e) for e in errs)
        if len(obs) > 1:
            mo = sum(obs) / len(obs)
            ss_tot = sum((v - mo) ** 2 for v in obs)
            ss_res = sum((s - v) ** 2 for s, v in zip(sims, obs))
            out["r2"] = 1 - ss_res / ss_tot if ss_tot > 0 else None
    return out


def pump_curve(design_head, design_flow, qf=1e-3):
    """EPANET single point pump curve: H = 4/3 Hd - (1/3) Hd (Q / Qd)^2.
    Returns (h0, rp) with Q in m3/s."""
    qd = max(design_flow, 1e-6) * qf
    return 4.0 * design_head / 3.0, design_head / 3.0 / qd**2


def write_inp(model, path, title="", hours=0, pattern=None):
    """EPANET input file of the model (same ids as the solver: N<node>,
    P<pipe>, Pu<pump>, V<valve>)."""
    if model.get("law") in GAS_LAWS:
        raise HydraulicError("EPANET is for water networks, not gas.")
    j = model["junctions"]
    fixed = model["fixed"]
    tanks = model.get("tanks", {})
    tinfo = model.get("tank_info", {})
    linked = set()
    for p in model["pipes"]:
        if p["a"] != p["b"]:
            linked.update((p["a"], p["b"]))
    for v in model.get("valves") or []:
        linked.update((v["a"], v["b"]))
    pat = " P1" if pattern else ""
    out = ["[TITLE]", title, "", "[JUNCTIONS]", ";ID\tElev\tDemand\tPattern"]
    for n in sorted(j):
        if n in fixed or n not in linked:
            continue
        out.append("N%d\t%.3f\t%.5f%s" % (n, j[n][0], j[n][1], pat))
    out += ["", "[RESERVOIRS]", ";ID\tHead"]
    for n in sorted(fixed):
        if n not in tanks:
            out.append("N%d\t%.3f" % (n, fixed[n]))
    out += ["", "[TANKS]", ";ID\tElev\tInitLvl\tMinLvl\tMaxLvl\tDiam\tMinVol"]
    for n in sorted(tanks):
        elev, lvl = tanks[n]
        ti = tinfo.get(n, {})
        out.append(
            "N%d\t%.3f\t%.3f\t%.3f\t%.3f\t%.3f\t0"
            % (
                n,
                elev,
                lvl,
                ti.get("min", 0.0),
                ti.get("max", lvl * 2),
                ti.get("diameter", 20.0),
            )
        )
    out += [
        "",
        "[PIPES]",
        ";ID\tNode1\tNode2\tLength\tDiameter\tRoughness\tMinorLoss\tStatus",
    ]
    pumps, curves = ["", "[PUMPS]", ";ID\tNode1\tNode2\tParameters"], [
        "",
        "[CURVES]",
        ";ID\tX\tY",
    ]
    for k, p in enumerate(model["pipes"]):
        if p["a"] == p["b"]:
            continue
        pid = p.get("id", k)
        if p.get("kind") == "pump":
            hd = p["h0"] * 3.0 / 4.0
            qd = math.sqrt(hd / 3.0 / p["rp"]) * 1000.0
            pumps.append(
                "Pu%d\tN%d\tN%d\tHEAD C%d" % (pid, p["a"], p["b"], pid)
            )
            curves.append("C%d\t%.4f\t%.4f" % (pid, qd, hd))
            continue
        out.append(
            "P%d\tN%d\tN%d\t%.3f\t%.1f\t%.2f\t0\t%s"
            % (
                pid,
                p["a"],
                p["b"],
                max(p["length"], 0.1),
                p["diameter"],
                p["c"],
                "Open" if p["open"] else "Closed",
            )
        )
    out += pumps
    out += [
        "",
        "[VALVES]",
        ";ID\tNode1\tNode2\tDiameter\tType\tSetting\tMinorLoss",
    ]
    for i, v in enumerate(model.get("valves") or []):
        out.append(
            "V%d\tN%d\tN%d\t%.1f\tPRV\t%.3f\t0"
            % (
                i,
                v["a"],
                v["b"],
                v.get("diameter", 150.0),
                v["head"] - j.get(v["b"], (0.0,))[0],
            )
        )
    out += curves
    if pattern:
        out += ["", "[PATTERNS]", ";ID\tMultipliers"]
        for i in range(0, len(pattern), 6):
            out.append(
                "P1\t" + "\t".join("%.4f" % m for m in pattern[i: i + 6])
            )
    out += [
        "",
        "[TIMES]",
        "Duration\t%d:00" % int(hours),
        "Hydraulic Timestep\t1:00",
        "Pattern Timestep\t1:00",
        "Report Timestep\t1:00",
        "",
        "[OPTIONS]",
        "Units\tLPS",
        "Headloss\tH-W",
        "",
        "[REPORT]",
        "Status\tNo",
        "Summary\tNo",
        "Nodes\tAll",
        "Links\tAll",
        "",
        "[COORDINATES]",
        ";Node\tX\tY",
    ]
    for n in sorted((set(j) & linked) | set(fixed)):
        xy = model.get("xy", {}).get(n)
        if xy:
            out.append("N%d\t%.3f\t%.3f" % (n, xy[0], xy[1]))
    out += ["", "[END]", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
