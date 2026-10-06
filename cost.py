"""Cost estimate: unit price list x bill of quantities."""
from . import storage as S
from .qgis_io import _int, _num, _val, class_layers, gkind


def template_rows(cfg):
    """One price row per class / group / type (/ size for lines) found in the data or the model."""
    size_field = cfg.settings.get("size_field") or cfg.tpl["size_field"][0]
    have = {(r["class_name"], r["ag_code"], r.get("at_code"), r.get("size")) for r in S.read_table(cfg.path, "un_unit_prices")}
    rows = []
    for role, lyr in class_layers(cfg).items():
        kind = gkind(lyr)
        if kind == "polygon" or role.startswith("Structure"):
            continue
        cls = cfg.classes[role]
        combos = set()
        for f in lyr.getFeatures():
            ag, at = _int(_val(f, "assetgroup")), _int(_val(f, "assettype"))
            size = _num(_val(f, size_field)) if kind == "line" else None
            if ag is not None:
                combos.add((ag, at, size))
        if not combos:
            combos = {(ag, None, None) for ag, _n in cfg.groups(cls)}
        for ag, at, size in sorted(combos, key=str):
            if (cls, ag, at, size) in have:
                continue
            rows.append({"class_name": cls, "ag_code": ag, "at_code": at, "size": size,
                         "unit": "m" if kind == "line" else "each", "price": 0.0, "currency": "",
                         "description": "%s %s%s" % (cfg.label(cls, ag), cfg.type_label(cls, ag, at) if at else "",
                                                     (" %g" % size) if size else "")})
    return rows


def estimate(cfg, lifecycle=None, selected_only=False):
    size_field = cfg.settings.get("size_field") or cfg.tpl["size_field"][0]
    prices = S.read_table(cfg.path, "un_unit_prices")

    def price_for(cls, ag, at, size):
        best, rank = None, -1
        for p in prices:
            if p["class_name"] != cls or p["ag_code"] != ag:
                continue
            if p.get("at_code") not in (None, at) or (p.get("size") is not None and p["size"] != size):
                continue
            r = (p.get("at_code") is not None) + (p.get("size") is not None)
            if r > rank:
                best, rank = p, r
        return best

    agg = {}
    for role, lyr in class_layers(cfg).items():
        kind = gkind(lyr)
        if kind == "polygon":
            continue
        cls = cfg.classes[role]
        for f in (lyr.getSelectedFeatures() if selected_only else lyr.getFeatures()):
            if lifecycle and _int(_val(f, "lifecyclestatus")) not in lifecycle:
                continue
            ag, at = _int(_val(f, "assetgroup")), _int(_val(f, "assettype"))
            size = _num(_val(f, size_field)) if kind == "line" else None
            k = (cls, ag, at, size, kind)
            agg[k] = agg.get(k, 0.0) + (f.geometry().length() if kind == "line" and f.hasGeometry() else 1)
    rows, total, missing = [], 0.0, 0
    for (cls, ag, at, size, kind), qty in sorted(agg.items(), key=str):
        p = price_for(cls, ag, at, size)
        unit_price = float(p["price"]) if p and p.get("price") is not None else None
        amount = round(qty * unit_price, 2) if unit_price is not None else None
        if amount is None or not unit_price:
            missing += 1
        total += amount or 0.0
        rows.append({"Class": cls, "Asset group": cfg.label(cls, ag), "Asset type": cfg.type_label(cls, ag, at),
                     "Size": size, "Quantity": round(qty, 2), "Unit": "m" if kind == "line" else "each",
                     "Unit price": unit_price, "Amount": amount, "Currency": (p or {}).get("currency") or ""})
    return rows, round(total, 2), missing
