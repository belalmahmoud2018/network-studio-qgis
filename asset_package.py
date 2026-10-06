"""ArcGIS Utility Network asset package (export and import).

Follows the table / field descriptions of the Esri 'Utility Network Package - Asset Package Reference'.
The real column names of Esri packages are not published; columns are written with the documented
names without spaces and the documented text as field alias, and the reader matches columns by alias
or name ignoring case, spaces and underscores, so it reads both.

GDAL cannot create subtypes in a File Geodatabase, but ArcGIS needs ASSETGROUP to be the subtype
field. The export therefore writes a small ArcPy script next to the package: run it once in the
ArcGIS Pro Python window, then run 'Apply Asset Package'."""
import os
import shutil
import uuid
from datetime import date

from osgeo import gdal, ogr

from . import storage as S
from .qgis_io import _val, class_layers, gkind

CAT_NAMES = {"source": "Subnetwork Controller", "isolating": "Isolating", "customer": "Service Point",
             "tap": "Tap", "service": "Service", "main": "Main", "structure": "Structure", "container": "Container",
             "endvertex": "End Vertex Only"}
ROLE_FC = {"Device": "Device", "Line": "Line", "Junction": "Junction", "Assembly": "Assembly",
           "StructureJunction": "StructureJunction", "StructureLine": "StructureLine",
           "StructureBoundary": "StructureBoundary"}
SKIP = {"assetgroup", "assettype", "globalid", "fid", "objectid", "subnetwork"}

TABLES = {
    "_Version": ["Schema Version", "Pro Version", "untools Version", "Date Exported"],
    "A_DomainNetwork": ["Target Domain Network", "Tier Definition", "Subnetwork Controller Type",
                        "Domain Network Alias Name"],
    "B_NetworkCategory": ["Category Name"],
    "B_NetworkCategory_Assignment": ["Target Domain Network", "Feature Class", "Asset Group", "Asset Type",
                                     "Category Name"],
    "B_NetworkAttribute": ["Attribute Name", "Attribute Type", "Domain Name", "Network Attribute to Substitute",
                           "Nullable", "Apportionable", "Is Overridable"],
    "B_NetworkAttribute_Assignment": ["Target Domain Network", "Feature Class", "Field", "Attribute Name"],
    "B_EdgeConnectivity": ["Target Domain Network", "Asset Group", "Asset Type", "Edge Connectivity"],
    "B_AssociationRole": ["Target Domain Network", "Feature Class", "Asset Group", "Asset Type", "Role Type",
                          "Delete Semantics", "View Scale", "Split Content"],
    "B_TerminalConfiguration": ["Configuration Name", "Directionality", "Default Path"],
    "B_TerminalConfiguration_Terminals": ["Configuration Name", "Name", "Upstream"],
    "B_TerminalConfiguration_ValidPaths": ["Configuration Name", "Name", "Value"],
    "B_TerminalConfiguration_Assignment": ["Target Domain Network", "Asset Group", "Asset Type", "Configuration Name"],
    "B_TierGroup": ["Target Domain Network", "Tier Group Name"],
    "B_Tier": ["Target Domain Network", "Tier Name", "Rank", "Topology Type", "Tier Group Name",
               "Subnetwork Field Name", "Support Disjoint Subnetwork", "Include Barrier Features",
               "Apply Traversability To"],
    "B_Subnetwork_Devices": ["Tier Name", "Asset Group", "Asset Type", "Valid Subnetwork Controllers"],
    "B_Subnetwork_Lines": ["Tier Name", "Asset Group", "Asset Type", "Aggregated Lines for SubnetLine Feature Class"],
    "B_Rules": ["Rule Type", "From Domain Network", "From Feature Class", "From Asset Group", "From Asset Type",
                "From Terminal", "To Domain Network", "To Feature Class", "To Asset Group", "To Asset Type",
                "To Terminal", "Via Domain Network", "Via Feature Class", "Via Asset Group", "Via Asset Type",
                "Via Terminal"],
    "C_Associations": ["Association Type", "From Domain", "From Feature Class", "From Asset Group", "From Asset Type",
                       "From Global ID", "From Terminal", "To Domain", "To Feature Class", "To Asset Group",
                       "To Asset Type", "To Global ID", "To Terminal", "Is Content Visible"],
    "C_SubnetworkControllers": ["Subnetwork Controller Name", "Feature Global ID", "Target Domain Network",
                                "Feature Asset Group", "Feature Asset Type", "Feature Terminal", "Tier Name",
                                "Subnetwork Name", "Description", "Notes"],
}


def _col(alias):
    return "".join(ch for ch in alias.title() if ch.isalnum())


def _key(text):
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _table(ds, name, rows):
    lyr = ds.CreateLayer(name, None, ogr.wkbNone)
    for alias in TABLES[name]:
        fd = ogr.FieldDefn(_col(alias), ogr.OFTString)
        fd.SetAlternativeName(alias)
        lyr.CreateField(fd)
    defn = lyr.GetLayerDefn()
    for r in rows:
        f = ogr.Feature(defn)
        for i, alias in enumerate(TABLES[name]):
            v = r.get(alias)
            if v is not None:
                f.SetField(i, str(v))
        lyr.CreateFeature(f)


def export(cfg, path, include_data=True):
    """Write an asset package (.gdb) and <name>_make_subtypes.py next to it."""
    if not path.lower().endswith(".gdb"):
        path += ".gdb"
    if os.path.exists(path):
        shutil.rmtree(path)
    domain = cfg.tpl["prefix"]
    struct = "Structure"
    layers = class_layers(cfg)
    ds = gdal.GetDriverByName("OpenFileGDB").Create(path, 0, 0, 0, gdal.GDT_Unknown)
    if ds is None:
        raise S.StoreError("Could not create %s" % path)
    groups = {}            # role -> {ag: (name, {at: name})}
    for r in cfg.assets:
        role = cfg.role_of.get(r["class_name"])
        g = groups.setdefault(role, {}).setdefault(r["ag_code"], [r["ag_name"], {}])
        g[1][r["at_code"]] = r["at_name"]
    fc_name = {}
    for role in layers:
        fc_name[role] = role if role.startswith("Structure") else domain + role
    # domains: one asset group domain per class (stand-in for the subtypes) and one asset type domain per group
    for role, gs in groups.items():
        fc = fc_name.get(role)
        if not fc:
            continue
        ds.AddFieldDomain(ogr.CreateCodedFieldDomain(
            "AssetGroup_%s" % fc, "Asset groups of %s" % fc, ogr.OFTInteger, ogr.OFSTNone,
            dict([(0, "Unknown")] + [(ag, g[0]) for ag, g in sorted(gs.items())])))
        for ag, (gname, types) in gs.items():
            ds.AddFieldDomain(ogr.CreateCodedFieldDomain(
                "AssetType_%s_%s" % (fc, "".join(c for c in gname if c.isalnum())), "%s asset types" % gname,
                ogr.OFTInteger, ogr.OFSTNone, dict([(0, "Unknown")] + sorted(types.items()))))
    # feature classes in the UtilityNetwork feature dataset (with data)
    gids = {}
    for role, lyr in layers.items():
        fc = fc_name[role]
        src = ds.CreateLayer(fc, _srs(lyr), {"point": ogr.wkbPoint, "line": ogr.wkbMultiLineString,
                                             "polygon": ogr.wkbMultiPolygon}[gkind(lyr)],
                             ["FEATURE_DATASET=UtilityNetwork"])
        fd = ogr.FieldDefn("ASSETGROUP", ogr.OFTInteger)
        fd.SetDomainName("AssetGroup_%s" % fc)
        fd.SetAlternativeName("Asset group")
        src.CreateField(fd)
        fd = ogr.FieldDefn("ASSETTYPE", ogr.OFTInteger)
        fd.SetAlternativeName("Asset type")
        src.CreateField(fd)
        src.CreateField(ogr.FieldDefn("GLOBALID", ogr.OFTString))
        copy = []
        for f in lyr.fields():
            if f.name().lower() in SKIP:
                continue
            t = {"Integer": ogr.OFTInteger, "Integer64": ogr.OFTInteger64, "Real": ogr.OFTReal}.get(
                f.typeName(), ogr.OFTReal if f.typeName().lower() in ("double", "real") else ogr.OFTString)
            src.CreateField(ogr.FieldDefn(f.name().upper(), t))
            copy.append(f.name())
        src.CreateField(ogr.FieldDefn("SUBNETWORKNAME", ogr.OFTString))
        if not include_data:
            continue
        defn = src.GetLayerDefn()
        cls = cfg.classes[role]
        for f in lyr.getFeatures():
            if not f.hasGeometry():
                continue
            g = _val(f, "globalid") or "{%s}" % str(uuid.uuid4()).upper()
            g = g if str(g).startswith("{") else "{%s}" % str(g).upper()
            gids[(cls, f.id())] = g
            o = ogr.Feature(defn)
            o.SetGeometry(ogr.CreateGeometryFromWkb(bytes(f.geometry().asWkb())))
            o.SetField("ASSETGROUP", _val(f, "assetgroup") or 0)
            o.SetField("ASSETTYPE", _val(f, "assettype") or 0)
            o.SetField("GLOBALID", g)
            for name in copy:
                v = _val(f, name)
                if v is not None:
                    o.SetField(name.upper(), v.toString("yyyy-MM-dd") if hasattr(v, "toString") else v)
            sn = _val(f, "subnetwork")
            if sn:
                o.SetField("SUBNETWORKNAME", sn)
            src.CreateFeature(o)
    # tables
    dn = lambda role: struct if role.startswith("Structure") else domain
    name_of = lambda cls, ag: cfg.label(cls, ag)
    tier_rows = cfg.tier_rows or [{"name": t, "rank": i + 1} for i, t in enumerate(cfg.tier_names())]
    rows = {k: [] for k in TABLES}
    rows["_Version"].append({"Schema Version": "Network Studio export", "Pro Version": "",
                             "untools Version": "", "Date Exported": date.today().isoformat()})
    rows["A_DomainNetwork"].append({"Target Domain Network": domain, "Tier Definition": "PARTITIONED",
                                    "Subnetwork Controller Type": "SUBNETWORK SINK" if cfg.flow == "gravity"
                                    else "SUBNETWORK SOURCE", "Domain Network Alias Name": cfg.tpl["label"]})
    cats_used = set()
    for r in cfg.assets:
        role = cfg.role_of.get(r["class_name"])
        for c in (r.get("categories") or "").split():
            cats_used.add(CAT_NAMES.get(c, c))
            rows["B_NetworkCategory_Assignment"].append({
                "Target Domain Network": dn(role), "Feature Class": ROLE_FC[role], "Asset Group": r["ag_name"],
                "Asset Type": r["at_name"], "Category Name": CAT_NAMES.get(c, c)})
        if role == "Line":
            rows["B_EdgeConnectivity"].append({
                "Target Domain Network": dn(role), "Asset Group": r["ag_name"], "Asset Type": r["at_name"],
                "Edge Connectivity": "EndVertex" if "endvertex" in (r.get("categories") or "") else "AnyVertex"})
        if role in ("Assembly", "StructureBoundary", "StructureJunction", "StructureLine"):
            container = role in ("Assembly", "StructureBoundary")
            rows["B_AssociationRole"].append({
                "Target Domain Network": dn(role), "Feature Class": ROLE_FC[role], "Asset Group": r["ag_name"],
                "Asset Type": r["at_name"], "Role Type": "CONTAINER" if container else "STRUCTURE",
                "Delete Semantics": "RESTRICTED", "View Scale": 200 if container else None,
                "Split Content": None})
        tier = r.get("tier")
        if tier and role == "Device":
            rows["B_Subnetwork_Devices"].append({
                "Tier Name": tier, "Asset Group": r["ag_name"], "Asset Type": r["at_name"],
                "Valid Subnetwork Controllers": "True" if "source" in (r.get("categories") or "") else "False"})
        if tier and role == "Line":
            rows["B_Subnetwork_Lines"].append({
                "Tier Name": tier, "Asset Group": r["ag_name"], "Asset Type": r["at_name"],
                "Aggregated Lines for SubnetLine Feature Class": "True"})
    rows["B_NetworkCategory"] = [{"Category Name": c} for c in sorted(cats_used)]
    for name, by_cls in cfg.network_attributes.items():
        rows["B_NetworkAttribute"].append({"Attribute Name": name, "Attribute Type": "LONG" if name in (
            "Asset group", "Asset type", "Lifecycle status", "Operating status", "Flow direction", "One way")
            else "DOUBLE", "Nullable": "True", "Apportionable": "False", "Is Overridable": "False"})
        for cls, field in by_cls.items():
            role = cfg.role_of.get(cls)
            if role:
                rows["B_NetworkAttribute_Assignment"].append({
                    "Target Domain Network": dn(role), "Feature Class": ROLE_FC[role], "Field": field.upper(),
                    "Attribute Name": name})
    for (cls, ag), terms in cfg.terminals.items():
        conf = "%s terminals" % name_of(cls, ag)
        rows["B_TerminalConfiguration"].append({"Configuration Name": conf, "Directionality": "DIRECTIONAL",
                                                "Default Path": "All"})
        for i, (tname, _tier) in enumerate(terms):
            rows["B_TerminalConfiguration_Terminals"].append({"Configuration Name": conf, "Name": tname,
                                                              "Upstream": "True" if i == 0 else "False"})
        rows["B_TerminalConfiguration_ValidPaths"].append({"Configuration Name": conf, "Name": "All", "Value": "All"})
        role = cfg.role_of.get(cls)
        for at, tname in sorted(groups.get(role, {}).get(ag, ["", {}])[1].items()):
            rows["B_TerminalConfiguration_Assignment"].append({"Target Domain Network": dn(role),
                                                               "Asset Group": name_of(cls, ag), "Asset Type": tname,
                                                               "Configuration Name": conf})
    tgroups = sorted({r.get("tier_group") for r in tier_rows if r.get("tier_group")})
    rows["B_TierGroup"] = [{"Target Domain Network": domain, "Tier Group Name": g} for g in tgroups]
    for r in tier_rows:
        rows["B_Tier"].append({"Target Domain Network": domain, "Tier Name": r["name"], "Rank": r.get("rank") or 1,
                               "Topology Type": "MESH" if r.get("multi_controllers") else "RADIAL",
                               "Tier Group Name": r.get("tier_group") or None,
                               "Subnetwork Field Name": "SUBNETWORKNAME", "Support Disjoint Subnetwork": "False",
                               "Include Barrier Features": "True",
                               "Apply Traversability To": "BOTH_JUNCTIONS_AND_EDGES"})
    for a, b in sorted(cfg.rules, key=str):
        ra, rb = cfg.role_of.get(a[0]), cfg.role_of.get(b[0])
        if not ra or not rb or (ra == "Line") == (rb == "Line") and ra == "Line":
            continue          # edge-edge needs a junction in ArcGIS: covered by the junction rules
        if ra == "Line":
            a, b, ra, rb = b, a, rb, ra
        rows["B_Rules"].append({"Rule Type": "Junction Edge Connectivity", "From Domain Network": dn(ra),
                                "From Feature Class": ROLE_FC[ra], "From Asset Group": name_of(*a), "From Asset Type": "*",
                                "From Terminal": "*" if ra == "Device" else None, "To Domain Network": dn(rb),
                                "To Feature Class": ROLE_FC[rb], "To Asset Group": name_of(*b), "To Asset Type": "*",
                                "To Terminal": None})
    seen = set()
    ag_at, aid = {}, {}
    for role, lyr in layers.items():
        cls = cfg.classes[role]
        for f in lyr.getFeatures():
            ag_at[(cls, f.id())] = (_val(f, "assetgroup"), _val(f, "assettype"))
            aid[(cls, f.id())] = _val(f, "assetid")
    kinds = {"connectivity": "Junction Junction Connectivity", "containment": "Containment",
             "attachment": "Structural Attachment"}
    for kind, a, b in cfg.associations:
        ra, rb = cfg.role_of.get(a[0]), cfg.role_of.get(b[0])
        if not ra or not rb or a not in gids or b not in gids:
            continue
        aa, bb = ag_at.get(a, (None, None)), ag_at.get(b, (None, None))
        rows["C_Associations"].append({
            "Association Type": kinds[kind], "From Domain": dn(ra), "From Feature Class": ROLE_FC[ra],
            "From Asset Group": name_of(a[0], aa[0]), "From Asset Type": cfg.type_label(a[0], aa[0], aa[1]),
            "From Global ID": gids[a], "To Domain": dn(rb), "To Feature Class": ROLE_FC[rb],
            "To Asset Group": name_of(b[0], bb[0]), "To Asset Type": cfg.type_label(b[0], bb[0], bb[1]),
            "To Global ID": gids[b], "Is Content Visible": "False" if kind == "containment" else None})
        if kind != "connectivity":
            key = (kind, ra, aa[0], rb, bb[0])
            if key not in seen:
                seen.add(key)
                rows["B_Rules"].append({"Rule Type": kinds[kind], "From Domain Network": dn(ra),
                                        "From Feature Class": ROLE_FC[ra], "From Asset Group": name_of(a[0], aa[0]),
                                        "From Asset Type": "*", "To Domain Network": dn(rb),
                                        "To Feature Class": ROLE_FC[rb], "To Asset Group": name_of(b[0], bb[0]),
                                        "To Asset Type": "*"})
    net_ctrl = cfg.controllers or {}
    dev = cfg.classes.get("Device")
    for (cls, fid), (ag, at) in ag_at.items():
        if cls != dev or (cls, fid) not in gids:
            continue
        explicit = (cls, fid) in net_ctrl
        if not explicit and (net_ctrl or "source" not in cfg.cats.get((cls, ag), set())):
            continue
        name = net_ctrl.get((cls, fid)) or aid.get((cls, fid)) or "%s-%d" % (name_of(cls, ag), fid)
        rows["C_SubnetworkControllers"].append({
            "Subnetwork Controller Name": name, "Feature Global ID": gids[(cls, fid)], "Target Domain Network": domain,
            "Feature Asset Group": name_of(cls, ag), "Feature Asset Type": cfg.type_label(cls, ag, at),
            "Feature Terminal": (cfg.terminals.get((cls, ag)) or [("Single Terminal", None)])[-1][0],
            "Tier Name": cfg.tiers.get((cls, ag)) or (tier_rows[0]["name"] if tier_rows else ""),
            "Subnetwork Name": name, "Description": "", "Notes": "Exported by Network Studio"})
    for name in TABLES:
        _table(ds, name, rows[name])
    ds = None
    script = _subtype_script(path, fc_name, groups)
    return path, script, {k: len(v) for k, v in rows.items() if v}


def _srs(lyr):
    from osgeo import osr
    s = osr.SpatialReference()
    s.ImportFromWkt(lyr.crs().toWkt())
    return s


def _subtype_script(path, fc_name, groups):
    lines = ['"""Run once in the ArcGIS Pro Python window, then run Apply Asset Package.',
             'Adds the ASSETGROUP subtypes, the asset type domains per subtype and the Global IDs."""',
             "import arcpy", "gdb = r'%s'" % os.path.abspath(path), "arcpy.env.workspace = gdb + r'\\UtilityNetwork'",
             "GROUPS = {"]
    for role, gs in groups.items():
        fc = fc_name.get(role)
        if fc:
            items = ", ".join("%d: (%r, %r)" % (ag, g[0], "AssetType_%s_%s" % (fc, "".join(c for c in g[0] if c.isalnum())))
                              for ag, g in sorted(gs.items()))
            lines.append("    %r: {%s}," % (fc, items))
    lines += ["}",
              "for fc, groups in GROUPS.items():",
              "    arcpy.management.RemoveDomainFromField(fc, 'ASSETGROUP')",
              "    arcpy.management.SetSubtypeField(fc, 'ASSETGROUP')",
              "    arcpy.management.AddSubtype(fc, 0, 'Unknown')",
              "    for code, (name, dom) in groups.items():",
              "        arcpy.management.AddSubtype(fc, code, name)",
              "        arcpy.management.AssignDomainToField(fc, 'ASSETTYPE', dom, str(code))",
              "    arcpy.management.SetDefaultSubtype(fc, 0)",
              "    arcpy.management.DeleteField(fc, 'GLOBALID') if False else None",
              "    arcpy.management.AddGlobalIDs(fc)",
              "    print('done', fc)",
              "print('Asset package ready: run Apply Asset Package.')", ""]
    script = os.path.splitext(path.rstrip('/\\\\'))[0] + "_make_subtypes.py"
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return script


# ---------------------------------------------------------------------- import
def _read_table(path, name):
    ds = gdal.OpenEx(path, gdal.OF_VECTOR)
    try:
        lyr = ds.GetLayerByName(name) if ds else None
        if lyr is None:
            return []
        d = lyr.GetLayerDefn()
        keys = []
        for i in range(d.GetFieldCount()):
            fd = d.GetFieldDefn(i)
            keys.append(_key(fd.GetAlternativeName() or fd.GetName()))
        return [{keys[i]: (f.GetField(i) if f.IsFieldSetAndNotNull(i) else None) for i in range(len(keys))}
                for f in lyr]
    finally:
        ds = None


def import_tables(cfg, path):
    """Associations (C_Associations) and subnetwork controllers (C_SubnetworkControllers) of a package,
    matched through the Global IDs of features already imported into the network."""
    gid = {}
    for role, lyr in class_layers(cfg).items():
        if lyr.fields().indexFromName("globalid") < 0:
            continue
        for f in lyr.getFeatures():
            if f["globalid"]:
                gid[str(f["globalid"]).strip("{}").upper()] = (cfg.classes[role], f.id())
    kinds = {"junctionjunctionconnectivity": "connectivity", "containment": "containment",
             "structuralattachment": "attachment"}
    assoc, miss = list(cfg.associations), 0
    for r in _read_table(path, "C_Associations"):
        a = gid.get(str(r.get("fromglobalid") or "").strip("{}").upper())
        b = gid.get(str(r.get("toglobalid") or "").strip("{}").upper())
        k = kinds.get(_key(r.get("associationtype")))
        if a and b and k:
            assoc.append((k, a, b))
        else:
            miss += 1
    ctrl = [dict(x) for x in cfg.controller_rows]
    nc = 0
    for r in _read_table(path, "C_SubnetworkControllers"):
        key = gid.get(str(r.get("featureglobalid") or "").strip("{}").upper())
        if key:
            ctrl.append({"class_name": key[0], "feature_fid": key[1],
                         "subnetwork_name": r.get("subnetworkname") or r.get("subnetworkcontrollername"),
                         "tier": r.get("tiername")})
            nc += 1
    S.save_associations(cfg.path, assoc)
    S.save_table(cfg.path, "un_controllers", ctrl)
    return {"associations": len(assoc) - len(cfg.associations), "not matched": miss, "controllers": nc}
