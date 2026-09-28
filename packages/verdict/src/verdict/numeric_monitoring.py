"""Frozen numeric evidence helpers for the existing Monitor owner.

No storage, scheduler or lifecycle lives here. Values identify their source
unit, so late exact-revision evaluation replaces a missing value once.
"""

from __future__ import annotations

from statistics import median

from verdict.statistics import mann_whitney_rank

METHOD = "mann_whitney_asymptotic_v1"


def freeze_numeric(units, *, grouped):
    cells = {}
    for unit in units:
        if grouped and unit.group_id is None:
            continue
        group = unit.group_id if grouped else None
        for name, (value, low, high, direction) in (unit.numeric_metrics or {}).items():
            identity = (group, name)
            spec = [low, high, direction]
            cell = cells.setdefault(
                identity, {"group_id": group, "metric": name, "spec": spec, "values": []}
            )
            if cell["spec"] != spec:
                raise ValueError("numeric score specification changed")
            if value is not None:
                cell["values"].append([unit.unit_id, value])
    return tuple(
        {**cell, "values": sorted(cell["values"])}
        for _, cell in sorted(cells.items(), key=lambda p: (p[0][0] or "", p[0][1]))
    )


def merge_numeric(first, second):
    cells = {(c["group_id"], c["metric"]): {**c, "values": list(c["values"])} for c in first}
    for cell in second:
        identity = (cell["group_id"], cell["metric"])
        prior = cells.get(identity)
        if prior is None:
            cells[identity] = cell
        else:
            if prior["spec"] != cell["spec"]:
                raise ValueError("numeric score specification changed")
            values = dict(prior["values"])
            for unit, value in cell["values"]:
                if unit in values and values[unit] != value:
                    raise ValueError("frozen numeric value changed")
                values[unit] = value
            prior["values"] = sorted(values.items())
    return tuple(c for _, c in sorted(cells.items(), key=lambda p: (p[0][0] or "", p[0][1])))


def validate_numeric(cells):
    import math

    if not isinstance(cells, (tuple, list)) or len(cells) > 4000:
        raise ValueError("invalid frozen numeric cells")
    ids, total = set(), 0
    for cell in cells:
        if not isinstance(cell, dict) or set(cell) != {"group_id", "metric", "spec", "values"}:
            raise ValueError("invalid frozen numeric cell")
        identity = (cell["group_id"], cell["metric"])
        if identity in ids or not isinstance(cell["metric"], str):
            raise ValueError("duplicate numeric cell")
        ids.add(identity)
        low, high, direction = cell["spec"]
        if (
            any(type(v) not in (float, int) or not math.isfinite(v) for v in (low, high))
            or low >= high
            or direction not in {"higher_is_better", "lower_is_better"}
        ):
            raise ValueError("invalid frozen numeric specification")
        seen = set()
        for unit, value in cell["values"]:
            if (
                not isinstance(unit, str)
                or not unit
                or unit in seen
                or type(value) not in (int, float)
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise ValueError("invalid frozen numeric value")
            seen.add(unit)
        total += len(seen)
    if total > 60_000:
        raise ValueError("numeric snapshot exceeds bounded value limit")


def numeric_contrasts(reference, current, groups, minimum_reference, minimum_current):
    a = {(c["group_id"], c["metric"]): c for c in reference}
    b = {(c["group_id"], c["metric"]): c for c in current}
    rows = []
    for identity in sorted(set(a) & set(b), key=lambda k: (k[0] or "", k[1])):
        if identity[0] not in groups:
            continue
        left, right = a[identity], b[identity]
        if left["spec"] != right["spec"]:
            raise ValueError("numeric score specification changed")
        x, y = [v for _, v in left["values"]], [v for _, v in right["values"]]
        if len(x) < max(30, minimum_reference) or len(y) < max(30, minimum_current):
            continue
        p, effect = mann_whitney_rank(x, y)
        rows.append((identity[0], identity[1], len(x), len(y), median(x), median(y), effect, p))
    return rows


def early_indicators(reference, current):
    a = {(c["group_id"], c["metric"]): c for c in reference.numeric_evidence}
    b = {(c["group_id"], c["metric"]): c for c in current.numeric_evidence}
    rows = []
    for identity in sorted(set(a) | set(b), key=lambda k: (k[0] or "", k[1])):
        x, y = (
            [v for _, v in a.get(identity, {}).get("values", [])],
            [v for _, v in b.get(identity, {}).get("values", [])],
        )
        rows.append(
            {
                "group_id": identity[0],
                "metric": identity[1],
                "kind": "number",
                "reference_n": len(x),
                "current_n": len(y),
                "reference_value": median(x) if x else None,
                "current_value": median(y) if y else None,
            }
        )
    return rows
