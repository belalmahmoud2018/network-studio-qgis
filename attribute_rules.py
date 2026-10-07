"""Attribute rules: calculation, constraint and validation rules written as
QGIS expressions and stored inside the network (un_attribute_rules).

Calculation  fills a field automatically when a feature is added and / or
             updated (a default value expression of the layer, so it also
             works in QField). "Evaluate rules" recalculates existing
             features.
Constraint   a condition every feature must meet. "error" blocks saving the
             attribute form, "warning" only warns. Checked again by Validate.
Validation   a condition checked in batch by Validate / Evaluate rules; the
             features that break it go to the error layers (code AR).

Extra expression functions (group "Network Studio"):
    ns_next_id('V-', 6)      next free number for a prefix  -> 'V-000124'
    ns_asset_group_name()    name of the feature's asset group
    ns_asset_type_name()     name of the feature's asset type
    ns_valid_asset_type()    true when the asset type belongs to the group
The QGIS functions overlay_nearest(), overlay_intersects(), aggregate() and
get_feature() reach the other layers (e.g. copy the diameter of the main a
valve sits on, or the zone a feature is in).
"""

import re

from qgis.core import (
    QgsDefaultValue,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeatureRequest,
    QgsFieldConstraints,
)

from . import storage as S

TYPES = ["calculation", "constraint", "validation"]
TRIGGERS = ["insert", "insert,update", "update"]
TABLE = "un_attribute_rules"
FIELDS = S.SYSTEM_TABLES[TABLE]
PROP = "network_studio/rule_fields"


# ------------------------------------------------------------------ storage
def load(path):
    rows = S.read_table(path, TABLE)
    for r in rows:
        r["enabled"] = 1 if r.get("enabled") in (None, 1, "1") else 0
    return sorted(
        rows, key=lambda r: (r.get("rule_order") or 0, r.get("name") or "")
    )


def save(path, rows):
    clean = []
    for i, r in enumerate(rows):
        r = dict(r)
        r["rule_order"] = i + 1
        clean.append({k: r.get(k) for k, _t in FIELDS})
    S.save_table(path, TABLE, clean)


def check(expression, layer=None):
    """'' when the expression is valid, else the error message."""
    e = QgsExpression(expression or "")
    if not (expression or "").strip():
        return "The expression is empty."
    if e.hasParserError():
        return e.parserErrorString()
    if layer is not None:
        ctx = _context(layer)
        f = next(layer.getFeatures(QgsFeatureRequest().setLimit(1)), None)
        if f is not None:
            ctx.setFeature(f)
            e.prepare(ctx)
            e.evaluate(ctx)
            if e.hasEvalError():
                return e.evalErrorString()
    return ""


# ------------------------------------------------------------------ layers
def _strength(kind):
    name = (
        "ConstraintStrengthHard"
        if kind == "hard"
        else ("ConstraintStrengthSoft")
    )
    v = getattr(QgsFieldConstraints, name, None)
    if v is None:
        v = getattr(
            QgsFieldConstraints.ConstraintStrength,
            name.replace("ConstraintStrength", ""),
        )
    return v


def _expr_constraint():
    v = getattr(QgsFieldConstraints, "ConstraintExpression", None)
    if v is None:
        v = QgsFieldConstraints.Constraint.ConstraintExpression
    return v


def clear_layer(lyr):
    """Remove what the rules set on a layer (before they are set again)."""
    names = [n for n in (lyr.customProperty(PROP, "") or "").split(",") if n]
    for n in names:
        i = lyr.fields().indexFromName(n)
        if i < 0:
            continue
        lyr.setDefaultValueDefinition(i, QgsDefaultValue())
        lyr.setConstraintExpression(i, "", "")
        try:
            lyr.removeFieldConstraint(i, _expr_constraint())
        except (AttributeError, TypeError):
            pass
    lyr.setCustomProperty(PROP, "")


def apply_layer(lyr, cls, rules):
    """Set the calculation rules as default values and the constraint rules
    as expression constraints of `lyr`."""
    used = []
    constraints = {}
    for r in rules:
        if not r.get("enabled") or r.get("class_name") != cls:
            continue
        field = r.get("field_name") or ""
        i = lyr.fields().indexFromName(field) if field else -1
        if r["rule_type"] == "calculation" and i >= 0:
            on_update = "update" in (r.get("triggers") or "insert")
            lyr.setDefaultValueDefinition(
                i, QgsDefaultValue(r.get("expression") or "", on_update)
            )
            used.append(field)
        elif r["rule_type"] == "constraint":
            if i < 0:  # feature level rule: shown on the first editable field
                for name in ("assetid", "assettype", "assetgroup"):
                    i = lyr.fields().indexFromName(name)
                    if i >= 0:
                        field = name
                        break
            if i < 0:
                continue
            constraints.setdefault(field, []).append(r)
    for field, rs in constraints.items():
        i = lyr.fields().indexFromName(field)
        expr = " AND ".join("(%s)" % r["expression"] for r in rs)
        desc = "; ".join(r.get("message") or r.get("name") or "" for r in rs)
        lyr.setConstraintExpression(i, expr, desc)
        hard = any((r.get("severity") or "error") == "error" for r in rs)
        lyr.setFieldConstraint(
            i, _expr_constraint(), _strength("hard" if hard else "soft")
        )
        used.append(field)
    lyr.setCustomProperty(PROP, ",".join(sorted(set(used))))
    return len(used)


# ------------------------------------------------------------------ evaluate
def _context(lyr):
    ctx = QgsExpressionContext()
    ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
    return ctx


def _truthy(v):
    if v is None:
        return False
    if hasattr(v, "isNull") and v.isNull():
        return False
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "f", "no")
    return bool(v)


def evaluate(
    cfg,
    layers,
    rules,
    selected_only=False,
    calculate=True,
    feedback=None,
    only=None,
):
    """Run the rules on existing features.

    Calculation rules write their value (only when it changes); constraint
    and validation rules return issues for the error layers.
    only {class: [fid]} limits the run to those features (edits made
    outside QGIS). Returns (issues, {rule name: features changed},
    errors)."""
    issues, changed, errors = [], {}, []
    by_cls = {cfg.classes[r]: lyr for r, lyr in layers.items()}
    _NEXT.clear()
    for rule in rules:
        if not rule.get("enabled"):
            continue
        lyr = by_cls.get(rule.get("class_name"))
        if lyr is None:
            continue
        expr = QgsExpression(rule.get("expression") or "")
        if expr.hasParserError():
            errors.append("%s: %s" % (rule["name"], expr.parserErrorString()))
            continue
        ctx = _context(lyr)
        expr.prepare(ctx)
        req = QgsFeatureRequest()
        if only is not None:
            fids = list(only.get(rule.get("class_name"), []))
            if not fids:
                continue
            req.setFilterFids(fids)
        elif selected_only:
            req.setFilterFids(lyr.selectedFeatureIds())
        kind = rule["rule_type"]
        if kind == "calculation":
            if not calculate:
                continue
            i = lyr.fields().indexFromName(rule.get("field_name") or "")
            if i < 0:
                errors.append(
                    "%s: no field %s" % (rule["name"], rule.get("field_name"))
                )
                continue
            updates = {}
            for f in lyr.getFeatures(req):
                ctx.setFeature(f)
                v = expr.evaluate(ctx)
                if expr.hasEvalError():
                    errors.append(
                        "%s: %s" % (rule["name"], expr.evalErrorString())
                    )
                    break
                old = f.attribute(i)
                if str(old) != str(v):
                    updates[f.id()] = {i: v}
            if updates:
                if lyr.isEditable():
                    for fid, a in updates.items():
                        lyr.changeAttributeValue(fid, i, a[i])
                else:
                    lyr.dataProvider().changeAttributeValues(updates)
                    lyr.triggerRepaint()
            changed[rule["name"]] = len(updates)
            continue
        from .qgis_io import gkind

        line = gkind(lyr) == "line"
        bad = 0
        for f in lyr.getFeatures(req):
            ctx.setFeature(f)
            v = expr.evaluate(ctx)
            if expr.hasEvalError():
                errors.append(
                    "%s: %s" % (rule["name"], expr.evalErrorString())
                )
                break
            if _truthy(v) or not f.hasGeometry():
                continue
            pt = f.geometry().pointOnSurface().asPoint()
            bad += 1
            issues.append(
                {
                    "code": "AR",
                    "severity": rule.get("severity") or "error",
                    "message": (
                        "%s: %s"
                        % (rule["name"], rule.get("message") or "rule broken")
                    ),
                    "x": pt.x(),
                    "y": pt.y(),
                    "key": (rule["class_name"], f.id()),
                    "geom": "line" if line else "point",
                }
            )
        changed[rule["name"]] = bad
        if feedback:
            feedback(rule["name"])
    return issues, changed, errors


def check_edits(cfg, lyr, cls, rules):
    """Constraint / validation rules broken by the uncommitted edits of a
    layer: [(rule name, message, count)]."""
    buf = lyr.editBuffer()
    if buf is None:
        return []
    fids = (
        set(buf.addedFeatures())
        | set(buf.changedAttributeValues())
        | set(buf.changedGeometries())
    )
    if not fids:
        return []
    out = []
    ctx = _context(lyr)
    feats = list(
        lyr.getFeatures(QgsFeatureRequest().setFilterFids(list(fids)))
    )
    for rule in rules:
        if (
            not rule.get("enabled")
            or rule.get("class_name") != cls
            or rule["rule_type"] not in ("constraint", "validation")
        ):
            continue
        expr = QgsExpression(rule.get("expression") or "")
        if expr.hasParserError():
            continue
        expr.prepare(ctx)
        n = 0
        for f in feats:
            ctx.setFeature(f)
            if not _truthy(expr.evaluate(ctx)):
                n += 1
        if n:
            out.append((rule["name"], rule.get("message") or "", n))
    return out


# ------------------------------------------------------------------ functions
_CFG = {}  # layer id -> (cfg, class name)
_NEXT = {}  # (layer id, field, prefix) -> last number given


def register_layers(cfg, layers):
    for role, lyr in layers.items():
        _CFG[lyr.id()] = (cfg, cfg.classes[role])


def _layer_of(context):
    from qgis.core import QgsProject

    lid = context.variable("layer_id") if context else None
    lyr = QgsProject.instance().mapLayer(lid) if lid else None
    if lyr is None and lid:
        from . import qgis_io as Q

        for cached in Q._CACHE.values():
            try:
                if cached.id() == lid:
                    return cached
            except RuntimeError:
                pass
    return lyr


def _next_id(values, feature, parent, context):
    prefix = str(values[0] if values else "")
    width = int(values[1]) if len(values) > 1 and values[1] else 6
    field = str(values[2]) if len(values) > 2 and values[2] else "assetid"
    lyr = _layer_of(context)
    if lyr is None:
        return None
    key = (lyr.id(), field, prefix)
    if key not in _NEXT:
        pat = re.compile(r"^%s(\d+)$" % re.escape(prefix))
        top = 0
        i = lyr.fields().indexFromName(field)
        if i >= 0:
            req = QgsFeatureRequest().setSubsetOfAttributes([i])
            for f in lyr.getFeatures(req):
                m = pat.match(str(f.attribute(i) or ""))
                if m:
                    top = max(top, int(m.group(1)))
        _NEXT[key] = top
    _NEXT[key] += 1
    return "%s%0*d" % (prefix, width, _NEXT[key])


def _cfg_of(context):
    lyr = _layer_of(context)
    return _CFG.get(lyr.id()) if lyr is not None else None


def _group_name(values, feature, parent, context):
    cc = _cfg_of(context)
    if cc is None or feature is None:
        return None
    cfg, cls = cc
    return cfg.label(cls, feature.attribute("assetgroup"))


def _type_name(values, feature, parent, context):
    cc = _cfg_of(context)
    if cc is None or feature is None:
        return None
    cfg, cls = cc
    return cfg.type_label(
        cls, feature.attribute("assetgroup"), feature.attribute("assettype")
    )


def _valid_type(values, feature, parent, context):
    cc = _cfg_of(context)
    if cc is None or feature is None:
        return True
    cfg, cls = cc
    ag, at = feature.attribute("assetgroup"), feature.attribute("assettype")
    return (cls, ag, at) in cfg.type_names


FUNCTIONS = [
    (
        "ns_next_id",
        -1,
        _next_id,
        (
            "ns_next_id(prefix, width, field): the next free number for a"
            " prefix in a field (default assetid), e.g. ns_next_id('V-', 6) ->"
            " 'V-000124'."
        ),
    ),
    (
        "ns_asset_group_name",
        0,
        _group_name,
        "ns_asset_group_name(): name of the asset group of the feature.",
    ),
    (
        "ns_asset_type_name",
        0,
        _type_name,
        "ns_asset_type_name(): name of the asset type of the feature.",
    ),
    (
        "ns_valid_asset_type",
        0,
        _valid_type,
        (
            "ns_valid_asset_type(): true when the asset type belongs to the"
            " asset group (Model page)."
        ),
    ),
]
_REGISTERED = []


def register_functions():
    from qgis.core import QgsExpressionFunction

    class _Fn(QgsExpressionFunction):
        def __init__(self, name, n, fn, help_text):
            QgsExpressionFunction.__init__(
                self, name, n, "Network Studio", "<p>%s</p>" % help_text
            )
            self._fn = fn

        def func(self, values, context, parent, node):
            feature = context.feature() if context else None
            try:
                return self._fn(values, feature, parent, context)
            except Exception as e:  # shown as an expression error
                parent.setEvalErrorString(str(e))
                return None

        def usesGeometry(self, node):
            return False

        def referencedColumns(self, node):
            return {QgsFeatureRequest.ALL_ATTRIBUTES}

        def handlesNull(self):
            return True

    for name, n, fn, help_text in FUNCTIONS:
        if QgsExpression.isFunctionName(name):
            continue
        f = _Fn(name, n, fn, help_text)
        if QgsExpression.registerFunction(f):
            _REGISTERED.append((name, f))


def unregister_functions():
    while _REGISTERED:
        name, _f = _REGISTERED.pop()
        QgsExpression.unregisterFunction(name)


# ------------------------------------------------------------------ templates
def templates(cfg):
    """Ready-made rules for the classes of this network."""
    out = []
    size = cfg.settings.get("size_field") or cfg.tpl["size_field"][0]
    line = cfg.classes.get("Line")
    dev = cfg.classes.get("Device")
    jun = cfg.classes.get("Junction")
    from .tools import _abbr

    prefix = _abbr(cfg.tpl.get("prefix") or "NET")

    def add(name, kind, cls, field, expr, trig, msg, sev, desc):
        if cls:
            out.append(
                {
                    "name": name,
                    "rule_type": kind,
                    "class_name": cls,
                    "field_name": field,
                    "expression": expr,
                    "triggers": trig,
                    "message": msg,
                    "severity": sev,
                    "enabled": 1,
                    "description": desc,
                }
            )

    for role, cls in (("Line", line), ("Device", dev), ("Junction", jun)):
        add(
            "Asset ID (%s)" % role.lower(),
            "calculation",
            cls,
            "assetid",
            "coalesce(\"assetid\", ns_next_id('%s-%s-', 6))"
            % (prefix, role[0]),
            "insert",
            "",
            "error",
            "A new feature gets the next free asset ID.",
        )
    add(
        "Measured length",
        "calculation",
        line,
        "measuredlength",
        "round($length, 2)",
        "insert,update",
        "",
        "error",
        "Length of the line, kept up to date when the geometry changes.",
    )
    if dev and line:
        add(
            "Size from the main",
            "calculation",
            dev,
            size,
            'coalesce("%s", array_first(overlay_nearest(\'%s\', "%s",'
            " max_distance:=%s)))" % (size, line, size, max(cfg.gap, 0.01)),
            "insert",
            "",
            "error",
            "A new device takes the size of the line it is placed on.",
        )
    for role, cls in (("line", line), ("device", dev)):
        add(
            "Size must be positive (%s)" % role,
            "constraint",
            cls,
            size,
            '"%s" IS NULL OR "%s" > 0' % (size, size),
            "",
            "The size must be greater than 0.",
            "error",
            "Stops negative or zero sizes in the form.",
        )
    for cls in (line, dev, jun):
        add(
            "Asset type belongs to the group (%s)" % cls,
            "constraint",
            cls,
            "assettype",
            "ns_valid_asset_type()",
            "",
            "The asset type does not belong to the asset group.",
            "error",
            "The asset type must be one of the types of the asset group.",
        )
        add(
            "Install date not in the future (%s)" % cls,
            "constraint",
            cls,
            "installdate",
            '"installdate" IS NULL OR "installdate" <= now()',
            "",
            "The install date is in the future.",
            "warning",
            "Catches typing mistakes in install dates.",
        )
        add(
            "In service needs an install date (%s)" % cls,
            "validation",
            cls,
            "",
            '"lifecyclestatus" <> 3 OR "installdate" IS NOT NULL',
            "",
            "In service without an install date.",
            "warning",
            "Assets in service should have an install date.",
        )
    if line:
        add(
            "Service line not too long",
            "validation",
            line,
            "",
            "ns_asset_group_name() NOT ILIKE '%service%' OR $length <= 60",
            "",
            "Service line longer than 60 m.",
            "warning",
            "Long service connections are usually drawing mistakes.",
        )
    return out
