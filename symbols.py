"""Professional point symbols: SVG icons chosen from the asset group name.

The SVGs use QGIS parameters (param(fill), param(outline)) so each asset group
keeps its colour.
"""

import os

HERE = os.path.join(os.path.dirname(__file__), "symbols")
S = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">%s</svg>'
F = (
    'fill="param(fill) #378add" stroke="param(outline) #222"'
    ' stroke-width="param(outline-width) 2"'
)
ICONS = {
    "valve": '<path d="M4 8 L20 20 L4 32 Z M36 8 L20 20 L36 32 Z" %s/>' % F,
    "hydrant": (
        '<rect x="12" y="10" width="16" height="24" rx="3" %s/><rect x="6"'
        ' y="18" width="28" height="6" %s/><rect x="15" y="4" width="10"'
        ' height="6" %s/>' % (F, F, F)
    ),
    "meter": (
        '<circle cx="20" cy="20" r="16" %s/><path d="M12 27 V13 L20 22 L28 13'
        ' V27" fill="none" stroke="#fff" stroke-width="3"/>' % F
    ),
    "pump": (
        '<circle cx="20" cy="20" r="16" %s/><path d="M14 12 L30 20 L14 28 Z"'
        ' fill="#fff"/>' % F
    ),
    "tank": (
        '<rect x="6" y="8" width="28" height="26" rx="2" %s/><path d="M6 16'
        ' H34" stroke="#fff" stroke-width="3"/>' % F
    ),
    "source": (
        '<path d="M20 3 L25 15 L38 15 L27 23 L31 36 L20 28 L9 36 L13 23 L2 15'
        ' L15 15 Z" %s/>' % F
    ),
    "transformer": (
        '<circle cx="14" cy="20" r="11" %s/><circle cx="26" cy="20" r="11" %s'
        ' fill-opacity="0.75"/>' % (F, F)
    ),
    "switch": (
        '<rect x="5" y="5" width="30" height="30" %s/><path d="M10 30 L30 10"'
        ' stroke="#fff" stroke-width="4"/>' % F
    ),
    "breaker": (
        '<rect x="5" y="5" width="30" height="30" %s/><path d="M12 12 L28 28'
        ' M28 12 L12 28" stroke="#fff" stroke-width="4"/>' % F
    ),
    "fuse": '<rect x="4" y="13" width="32" height="14" rx="7" %s/>' % F,
    "manhole": (
        '<circle cx="20" cy="20" r="16" %s/><path d="M9 9 L31 31 M31 9 L9 31"'
        ' stroke="#fff" stroke-width="3"/>' % F
    ),
    "splitter": '<path d="M4 20 L36 4 L36 36 Z" %s/>' % F,
    "pole": (
        '<circle cx="20" cy="20" r="14" fill="none" stroke="param(fill)'
        ' #378add" stroke-width="5"/><circle cx="20" cy="20" r="5" %s/>' % F
    ),
    "customer": '<path d="M20 4 L36 18 H31 V36 H9 V18 H4 Z" %s/>' % F,
    "outfall": '<path d="M4 6 H36 L20 34 Z" %s/>' % F,
    "signal": (
        '<rect x="12" y="2" width="16" height="36" rx="4" %s/><circle cx="20"'
        ' cy="11" r="4" fill="#fff"/><circle cx="20" cy="20" r="4"'
        ' fill="#fff"/><circle cx="20" cy="29" r="4" fill="#fff"/>' % F
    ),
    "junction": '<circle cx="20" cy="20" r="12" %s/>' % F,
    "tap": '<path d="M20 4 L36 20 L20 36 L4 20 Z" %s/>' % F,
}
KEYWORDS = [  # first match wins
    (("hydrant",), "hydrant"),
    (
        (
            "meter",
            "ont",
            "customer",
            "terminal",
            "address",
            "energy transfer",
            "street light",
            "connection",
        ),
        "meter",
    ),
    (("pump", "lift station"), "pump"),
    (("tank", "reservoir", "detention"), "tank"),
    (("transformer",), "transformer"),
    (("breaker", "recloser"), "breaker"),
    (("fuse",), "fuse"),
    (("switch", "disconnector"), "switch"),
    (("valve", "gate", "barrier", "efv"), "valve"),
    (("outfall", "treatment", "plant"), "outfall"),
    (
        ("source", "city gate", "olt", "exchange", "well", "regulator"),
        "source",
    ),
    (("manhole", "cleanout", "chamber", "handhole", "inlet"), "manhole"),
    (
        (
            "splitter",
            "odf",
            "closure",
            "joint",
            "cabinet",
            "distribution point",
            "termination box",
        ),
        "splitter",
    ),
    (("pole", "tower"), "pole"),
    (("signal", "sign"), "signal"),
    (("tap", "service point", "drop point", "access point"), "tap"),
]


def ensure():
    os.makedirs(HERE, exist_ok=True)
    for name, body in ICONS.items():
        path = os.path.join(HERE, name + ".svg")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(S % body)


def icon_for(group_name, role):
    low = (group_name or "").lower()
    for words, icon in KEYWORDS:
        if any(w in low for w in words):
            return os.path.join(HERE, icon + ".svg")
    return os.path.join(
        HERE, ("junction" if "Junction" in role else "junction") + ".svg"
    )


def marker(group_name, role, color, size):
    from qgis.core import QgsMarkerSymbol, QgsSvgMarkerSymbolLayer

    ensure()
    lay = QgsSvgMarkerSymbolLayer(icon_for(group_name, role), size)
    lay.setFillColor(color)
    lay.setStrokeColor(color.darker(180))
    lay.setStrokeWidth(0.2)
    sym = QgsMarkerSymbol()
    sym.changeSymbolLayer(0, lay)
    return sym
