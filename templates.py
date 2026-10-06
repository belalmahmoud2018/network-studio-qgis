"""Network type templates.

Choosing a network type decides the classes (layers), their asset groups and
asset
types, which groups are sources / isolating devices / customers, the tiers, the
flow
model and how service connections are drawn.

Categories used by the engine and tools:
    source     subnetwork controller (pump, tank, substation breaker, OLT,
    outfall...)
    isolating  closes the flow during an isolation trace (valve, switch,
    fuse...)
    customer   end user / meter (counted in isolation and subnetworks)
    tap        junction where a service line joins the main
    service    the service line that reaches the building
    main       line a service connection may be connected to
    structure  supporting structure (pole, manhole...), not part of the flow
"""

ROLES = (
    "Device",
    "Line",
    "Junction",
    "Assembly",
    "StructureJunction",
    "StructureLine",
    "StructureBoundary",
)
GEOMETRY = {
    "Device": "Point",
    "Line": "LineString",
    "Junction": "Point",
    "Assembly": "Polygon",
    "StructureJunction": "Point",
    "StructureLine": "LineString",
    "StructureBoundary": "Polygon",
}

LIFECYCLE = [
    (1, "Proposed"),
    (2, "Under construction"),
    (3, "In service"),
    (4, "Out of service"),
    (5, "Abandoned"),
    (6, "Removed"),
]
OPERATING = [(1, "Open / On"), (0, "Closed / Off")]
FLOW_DIRECTION = [
    (1, "With digitized direction"),
    (2, "Against digitized direction"),
]
ONEWAY = [
    (0, "Both directions"),
    (1, "With digitized direction"),
    (2, "Against digitized direction"),
]


def G(name, types, cats="", tier=None, terminals=None):
    """Asset group definition: name, [asset type names], categories, tier,
    [(terminal, tier)]."""
    return {
        "name": name,
        "types": list(types),
        "cats": set(cats.split()) if cats else set(),
        "tier": tier,
        "terminals": list(terminals or []),
    }


def _structures(kind):
    if kind == "telecom":
        return {
            "StructureJunction": [
                G("Manhole", ["Standard", "Large"], "structure"),
                G("Handhole", ["Standard"], "structure"),
                G("Pole", ["Wood", "Concrete", "Steel"], "structure"),
            ],
            "StructureLine": [
                G("Duct", ["Single", "Multi-way"], "structure"),
                G("Conduit", ["PVC", "HDPE"], "structure"),
            ],
            "StructureBoundary": [
                G("Building", ["Exchange", "Data center"], "structure"),
                G("Cabinet enclosure", ["Street cabinet"], "structure"),
            ],
        }
    if kind == "electric":
        return {
            "StructureJunction": [
                G("Pole", ["Wood", "Concrete", "Steel"], "structure"),
                G("Manhole", ["Standard"], "structure"),
                G("Tower", ["Lattice", "Monopole"], "structure"),
            ],
            "StructureLine": [
                G("Duct bank", ["Standard"], "structure"),
                G("Trench", ["Standard"], "structure"),
            ],
            "StructureBoundary": [
                G("Substation", ["Primary", "Secondary"], "structure"),
                G("Vault", ["Standard"], "structure"),
            ],
        }
    return {
        "StructureJunction": [
            G("Manhole", ["Standard"], "structure"),
            G("Valve chamber", ["Standard"], "structure"),
        ],
        "StructureLine": [
            G("Casing", ["Steel", "Concrete"], "structure"),
            G("Tunnel", ["Standard"], "structure"),
        ],
        "StructureBoundary": [
            G("Station", ["Pump station", "Treatment plant"], "structure"),
            G("Vault", ["Standard"], "structure"),
        ],
    }


NETWORK_TYPES = [
    {
        "key": "water",
        "label": "Water distribution",
        "prefix": "Water",
        "flow": "source",
        "size_field": ("diameter", "Diameter (mm)"),
        "structures": "general",
        "description": (
            "Pipes, valves, hydrants and meters fed by pumps, tanks and"
            " reservoirs."
        ),
        "classes": {
            "Device": [
                G(
                    "Source",
                    ["Reservoir", "Tank", "Well", "Treatment plant"],
                    "source",
                ),
                G("Pump", ["Booster", "Well pump"], "source"),
                G("System valve", ["Gate", "Butterfly", "Ball"], "isolating"),
                G(
                    "Control valve",
                    ["Pressure reducing", "Pressure sustaining", "Check"],
                ),
                G("Hydrant", ["Wet barrel", "Dry barrel"]),
                G("Air release", ["Air valve", "Blow off"]),
                G("Meter", ["Residential", "Commercial", "Bulk"], "customer"),
            ],
            "Line": [
                G(
                    "Transmission main",
                    ["Ductile iron", "Steel", "Concrete"],
                    "main",
                    "Transmission",
                ),
                G(
                    "Distribution main",
                    ["Ductile iron", "PVC", "HDPE", "Asbestos cement"],
                    "main",
                    "Distribution",
                ),
                G(
                    "Service",
                    ["Copper", "PE", "PVC"],
                    "service",
                    "Distribution",
                ),
                G(
                    "Hydrant lateral",
                    ["Ductile iron", "PVC"],
                    "",
                    "Distribution",
                ),
            ],
            "Junction": [
                G(
                    "Fitting",
                    ["Tee", "Cross", "Bend", "Reducer", "Coupling", "Cap"],
                ),
                G("Tap", ["Saddle", "Tapping sleeve"], "tap"),
                G("Connection point", ["Line end"]),
            ],
            "Assembly": [
                G("Pump station", ["Standard"]),
                G("Valve assembly", ["PRV station", "Meter vault"]),
            ],
        },
        "service": {"line": "Service", "tap": "Tap", "end": "Meter"},
    },
    {
        "key": "wastewater",
        "label": "Wastewater (sewer)",
        "prefix": "Sewer",
        "flow": "gravity",
        "size_field": ("diameter", "Diameter (mm)"),
        "structures": "general",
        "description": (
            "Gravity mains and force mains draining to outfalls and treatment"
            " plants."
        ),
        "classes": {
            "Device": [
                G("Treatment plant", ["Plant"], "source"),
                G("Outfall", ["Outfall"], "source"),
                G("Lift station", ["Submersible", "Dry well"]),
                G("Isolation valve", ["Gate", "Plug valve"], "isolating"),
                G("Cleanout", ["Standard"]),
                G(
                    "Customer connection",
                    ["Residential", "Commercial", "Industrial"],
                    "customer",
                ),
            ],
            "Line": [
                G(
                    "Gravity main",
                    ["PVC", "Concrete", "Vitrified clay", "GRP"],
                    "main",
                ),
                G("Force main", ["Ductile iron", "HDPE"], "main"),
                G("Lateral", ["PVC", "HDPE"], "service"),
            ],
            "Junction": [
                G("Manhole", ["Standard", "Drop", "Inspection chamber"]),
                G("Fitting", ["Wye", "Tee", "Bend", "Reducer"]),
                G("Tap", ["Saddle", "Wye connection"], "tap"),
            ],
            "Assembly": [G("Pump station", ["Standard"])],
        },
        "service": {
            "line": "Lateral",
            "tap": "Tap",
            "end": "Customer connection",
        },
    },
    {
        "key": "stormwater",
        "label": "Stormwater",
        "prefix": "Storm",
        "flow": "gravity",
        "size_field": ("diameter", "Diameter (mm)"),
        "structures": "general",
        "description": (
            "Inlets, culverts, channels and outfalls of a gravity drainage"
            " system."
        ),
        "classes": {
            "Device": [
                G("Outfall", ["Outfall", "Headwall"], "source"),
                G("Inlet", ["Curb inlet", "Grate inlet", "Catch basin"]),
                G("Detention", ["Pond", "Tank"]),
                G("Gate", ["Flap gate", "Sluice gate"], "isolating"),
            ],
            "Line": [
                G("Storm main", ["Concrete", "PVC", "HDPE"], "main"),
                G("Culvert", ["Box", "Pipe"], "main"),
                G("Open channel", ["Lined", "Unlined"], "main"),
                G("Lateral", ["PVC", "Concrete"], "service"),
            ],
            "Junction": [
                G("Manhole", ["Standard", "Junction box"]),
                G("Fitting", ["Wye", "Bend"]),
                G("Tap", ["Connection"], "tap"),
            ],
            "Assembly": [G("Pump station", ["Standard"])],
        },
        "service": {"line": "Lateral", "tap": "Tap", "end": "Inlet"},
    },
    {
        "key": "gas",
        "label": "Gas",
        "prefix": "Gas",
        "flow": "source",
        "size_field": ("diameter", "Diameter (mm)"),
        "structures": "general",
        "description": (
            "Pressure systems fed by city gates and regulator stations."
        ),
        "classes": {
            "Device": [
                G("City gate", ["Standard"], "source", "Transmission"),
                G(
                    "Regulator station",
                    ["District", "Farm tap"],
                    "source",
                    "Distribution",
                    [("Inlet", "Transmission"), ("Outlet", "Distribution")],
                ),
                G("Valve", ["Ball", "Plug", "Gate"], "isolating"),
                G("Excess flow valve", ["Service EFV"], "isolating"),
                G(
                    "Meter",
                    ["Residential", "Commercial", "Industrial"],
                    "customer",
                ),
                G("Cathodic protection", ["Anode", "Test point"]),
            ],
            "Line": [
                G("Transmission pipe", ["Steel"], "main", "Transmission"),
                G(
                    "Distribution main",
                    ["Steel", "PE"],
                    "main",
                    "Distribution",
                ),
                G(
                    "Service",
                    ["PE", "Steel", "Copper"],
                    "service",
                    "Distribution",
                ),
            ],
            "Junction": [
                G("Fitting", ["Tee", "Elbow", "Reducer", "Cap", "Coupling"]),
                G("Tap", ["Tapping tee", "Saddle"], "tap"),
            ],
            "Assembly": [
                G("Regulator station", ["Standard"]),
                G("Meter set", ["Standard"]),
            ],
        },
        "service": {"line": "Service", "tap": "Tap", "end": "Meter"},
    },
    {
        "key": "electric",
        "label": "Electricity",
        "prefix": "Electric",
        "flow": "source",
        "size_field": ("voltage_kv", "Voltage (kV)"),
        "structures": "electric",
        "description": (
            "Medium and low voltage feeders, transformers, switches and"
            " meters."
        ),
        "classes": {
            "Device": [
                G(
                    "Circuit breaker",
                    ["Feeder breaker"],
                    "source isolating",
                    "Medium voltage",
                ),
                G(
                    "Transformer",
                    ["Pole mounted", "Pad mounted", "Kiosk"],
                    "source",
                    "Low voltage",
                    [
                        ("High side", "Medium voltage"),
                        ("Low side", "Low voltage"),
                    ],
                ),
                G(
                    "Switch",
                    ["Load break", "Disconnector", "Ring main unit"],
                    "isolating",
                ),
                G("Fuse", ["Cutout", "LV fuse"], "isolating"),
                G("Recloser", ["Standard"], "isolating"),
                G("Capacitor", ["Fixed", "Switched"]),
                G("Street light", ["LED", "Sodium"], "customer"),
                G(
                    "Meter",
                    ["Residential", "Commercial", "Industrial"],
                    "customer",
                ),
            ],
            "Line": [
                G("MV overhead", ["ACSR", "AAC"], "main", "Medium voltage"),
                G("MV underground", ["XLPE"], "main", "Medium voltage"),
                G("LV overhead", ["ABC", "Bare"], "main", "Low voltage"),
                G("LV underground", ["XLPE", "PVC"], "main", "Low voltage"),
                G(
                    "Service",
                    ["Overhead drop", "Underground"],
                    "service",
                    "Low voltage",
                ),
                G("Busbar", ["Standard"]),
            ],
            "Junction": [
                G("Connection point", ["Splice", "Termination"]),
                G("Service point", ["Pole tap", "Service box"], "tap"),
            ],
            "Assembly": [
                G("Substation", ["Primary", "Secondary", "Kiosk"]),
                G("Switchgear", ["Ring main unit"]),
            ],
        },
        "service": {"line": "Service", "tap": "Service point", "end": "Meter"},
    },
    {
        "key": "fiber",
        "label": "Fiber optic",
        "prefix": "Fiber",
        "flow": "source",
        "size_field": ("fiber_count", "Fiber count"),
        "structures": "telecom",
        "description": (
            "FTTH/FTTx: OLT, splitters, splice closures, cables and customer"
            " ONTs."
        ),
        "classes": {
            "Device": [
                G("OLT", ["Central office OLT"], "source"),
                G("ODF", ["Patch panel"]),
                G("Splitter", ["1x4", "1x8", "1x16", "1x32", "1x64"]),
                G("Splice closure", ["Inline", "Dome"]),
                G("Termination box", ["FDB", "FAT"]),
                G("ONT", ["Residential", "Business"], "customer"),
            ],
            "Line": [
                G("Feeder cable", ["Underground", "Aerial"], "main"),
                G("Distribution cable", ["Underground", "Aerial"], "main"),
                G("Drop cable", ["Underground", "Aerial"], "service"),
                G("Patch cord", ["Standard"]),
            ],
            "Junction": [
                G("Connection point", ["Splice point", "Cable end"]),
                G("Drop point", ["Tap"], "tap"),
            ],
            "Assembly": [
                G("Cabinet", ["FDH", "Street cabinet"]),
                G("Central office", ["Exchange"]),
            ],
        },
        "service": {"line": "Drop cable", "tap": "Drop point", "end": "ONT"},
    },
    {
        "key": "telecom",
        "label": "Telecommunications (copper)",
        "prefix": "Telecom",
        "flow": "source",
        "size_field": ("pairs", "Pair count"),
        "structures": "telecom",
        "description": (
            "Exchanges, cabinets, distribution points and copper cables."
        ),
        "classes": {
            "Device": [
                G("Exchange", ["Main distribution frame"], "source"),
                G("Cabinet", ["Primary", "Secondary"]),
                G("Distribution point", ["Pole DP", "Wall DP"]),
                G("Joint", ["Straight", "Branch"]),
                G(
                    "Customer terminal",
                    ["Residential", "Business"],
                    "customer",
                ),
            ],
            "Line": [
                G("Primary cable", ["Underground", "Aerial"], "main"),
                G("Secondary cable", ["Underground", "Aerial"], "main"),
                G("Drop wire", ["Aerial", "Underground"], "service"),
            ],
            "Junction": [
                G("Connection point", ["Splice", "Cable end"]),
                G("Drop point", ["Tap"], "tap"),
            ],
            "Assembly": [G("Exchange building", ["Standard"])],
        },
        "service": {
            "line": "Drop wire",
            "tap": "Drop point",
            "end": "Customer terminal",
        },
    },
    {
        "key": "heating",
        "label": "District heating / cooling",
        "prefix": "Thermal",
        "flow": "source",
        "size_field": ("diameter", "Diameter (mm)"),
        "structures": "general",
        "description": (
            "Plants, supply/return pipes, valves and building substations."
        ),
        "classes": {
            "Device": [
                G("Plant", ["Cooling plant", "Heating plant"], "source"),
                G("Pump", ["Circulation"]),
                G("Valve", ["Ball", "Butterfly"], "isolating"),
                G(
                    "Energy transfer station",
                    ["Building substation"],
                    "customer",
                ),
            ],
            "Line": [
                G("Supply pipe", ["Pre-insulated steel", "PEX"], "main"),
                G("Return pipe", ["Pre-insulated steel", "PEX"], "main"),
                G("Service pipe", ["Pre-insulated steel", "PEX"], "service"),
            ],
            "Junction": [
                G("Fitting", ["Tee", "Bend", "Reducer"]),
                G("Tap", ["Branch"], "tap"),
            ],
            "Assembly": [G("Plant", ["Standard"])],
        },
        "service": {
            "line": "Service pipe",
            "tap": "Tap",
            "end": "Energy transfer station",
        },
    },
    {
        "key": "roads",
        "label": "Roads",
        "prefix": "Road",
        "flow": "undirected",
        "size_field": ("lanes", "Lanes"),
        "structures": None,
        "description": (
            "Road segments, intersections and road furniture (connected and"
            " shortest-path analysis)."
        ),
        "classes": {
            "Device": [
                G("Traffic signal", ["Signal", "Pedestrian signal"]),
                G("Sign", ["Stop", "Yield", "Information"]),
                G("Barrier", ["Gate", "Bollard"], "isolating"),
                G("Address point", ["Building entrance"], "customer"),
            ],
            "Line": [
                G("Highway", ["Divided", "Undivided"], "main"),
                G("Arterial", ["Main road"], "main"),
                G("Collector", ["Secondary"], "main"),
                G("Local street", ["Residential"], "main"),
                G("Access road", ["Driveway", "Service road"], "service"),
                G("Ramp", ["On ramp", "Off ramp"]),
            ],
            "Junction": [
                G(
                    "Intersection",
                    ["Signalized", "Unsignalized", "Roundabout"],
                ),
                G("Access point", ["Driveway connection"], "tap"),
                G("Road end", ["Dead end", "Boundary"]),
            ],
            "Assembly": [G("Interchange", ["Standard"])],
        },
        "service": {
            "line": "Access road",
            "tap": "Access point",
            "end": "Address point",
        },
    },
    {
        "key": "generic",
        "label": "Generic / custom",
        "prefix": "Net",
        "flow": "source",
        "size_field": ("size", "Size"),
        "structures": "general",
        "description": (
            "A neutral starting point: rename groups and types for any other"
            " network."
        ),
        "classes": {
            "Device": [
                G("Source", ["Standard"], "source"),
                G("Isolating device", ["Standard"], "isolating"),
                G("Device", ["Standard"]),
                G("Customer", ["Standard"], "customer"),
            ],
            "Line": [
                G("Main", ["Standard"], "main"),
                G("Branch", ["Standard"], "main"),
                G("Service", ["Standard"], "service"),
            ],
            "Junction": [
                G("Junction", ["Standard"]),
                G("Tap", ["Standard"], "tap"),
            ],
            "Assembly": [G("Assembly", ["Standard"])],
        },
        "service": {"line": "Service", "tap": "Tap", "end": "Customer"},
    },
]

BY_KEY = {t["key"]: t for t in NETWORK_TYPES}


def class_names(tpl, include_structures=True):
    """{role: class name} for a template."""
    out = {
        role: tpl["prefix"] + role
        for role in ("Device", "Line", "Junction", "Assembly")
    }
    if include_structures and tpl.get("structures"):
        for role in (
            "StructureJunction",
            "StructureLine",
            "StructureBoundary",
        ):
            out[role] = role
    return out


def groups_for(tpl, role):
    if role in tpl["classes"]:
        return tpl["classes"][role]
    if tpl.get("structures"):
        return _structures(tpl["structures"]).get(role, [])
    return []


def asset_rows(tpl, include_structures=True):
    """Flat rows for the un_asset_types table."""
    rows = []
    for role, cls in class_names(tpl, include_structures).items():
        for gi, g in enumerate(groups_for(tpl, role), start=1):
            for ti, t in enumerate(g["types"], start=1):
                rows.append(
                    {
                        "class_name": cls,
                        "role": role,
                        "ag_code": gi,
                        "ag_name": g["name"],
                        "at_code": ti,
                        "at_name": t,
                        "categories": " ".join(sorted(g["cats"])),
                        "tier": g["tier"],
                    }
                )
    return rows


def terminal_rows(tpl, include_structures=True):
    rows = []
    for role, cls in class_names(tpl, include_structures).items():
        for gi, g in enumerate(groups_for(tpl, role), start=1):
            for name, tier in g.get("terminals", []):
                rows.append(
                    {
                        "class_name": cls,
                        "ag_code": gi,
                        "terminal_name": name,
                        "tier": tier,
                    }
                )
    return rows


def default_rules(tpl, include_structures=True):
    """Connectivity rules ((cls, ag), (cls, ag)) a new network starts with."""
    names = class_names(tpl, include_structures)
    lines = [
        (names["Line"], gi, g)
        for gi, g in enumerate(groups_for(tpl, "Line"), 1)
    ]
    points = [
        (names[r], gi, g)
        for r in ("Device", "Junction")
        for gi, g in enumerate(groups_for(tpl, r), 1)
    ]
    has_tap = any("tap" in g["cats"] for _c, _i, g in points)
    rules = set()

    def add(a, b):
        rules.add(tuple(sorted((a, b), key=str)))

    for lc, li, lg in lines:
        service = "service" in lg["cats"]
        for pc, pi, pg in points:
            if service:
                ok = bool(pg["cats"] & {"tap", "customer"}) or not pg["cats"]
            else:
                ok = (
                    "customer" not in pg["cats"] or tpl["flow"] == "undirected"
                )
            if ok:
                add((lc, li), (pc, pi))
        for lc2, li2, lg2 in lines:
            same_tier = (
                lg["tier"] is None
                or lg2["tier"] is None
                or lg["tier"] == lg2["tier"]
            )
            s1, s2 = "service" in lg["cats"], "service" in lg2["cats"]
            if s1 != s2 and has_tap and tpl["flow"] != "undirected":
                continue  # a service joins the main through a tap
            if same_tier:
                add((lc, li), (lc2, li2))
    return rules
