"""Parses Tableau <dashboard> XML elements into IRDashboard objects."""
from __future__ import annotations
from uuid import uuid4
from lxml import etree

from accelerator.ir.schema import IRDashboard, IRZone, IRAction


def _attr(el, name: str, default: str = "") -> str:
    return el.get(name, default) or default


def _int(val: str, default: int = 0) -> int:
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def _parse_zone(zone_el) -> IRZone:
    raw_type = zone_el.get("type")  # None when attribute is absent
    name = _attr(zone_el, "name")

    # Tableau convention: zones WITHOUT a 'type' attribute that carry a 'name'
    # are worksheet references — the name IS the worksheet name.
    # Real Tableau files also use type="view" for worksheet zones (type="worksheet"
    # only appears in synthetic/simplified files).
    # Zones with other explicit types (layout-basic, layout-flow, title, text, …)
    # are structural containers, not worksheets.
    if (raw_type is None or raw_type == "view") and name:
        zone_type = "worksheet"
    elif raw_type is None:
        zone_type = "blank"
    else:
        zone_type = raw_type

    type_map = {
        "worksheet": "worksheet", "text": "text", "image": "image",
        "web": "web", "blank": "blank", "container": "container",
        "layout-container": "layout-container",
    }
    is_floating = _attr(zone_el, "fixed") == "true" or _attr(zone_el, "float", "") == "true"

    zone = IRZone(
        id=uuid4(),
        zone_type=type_map.get(zone_type, "blank"),
        worksheet_name=name if zone_type == "worksheet" else None,
        x=_int(_attr(zone_el, "x")),
        y=_int(_attr(zone_el, "y")),
        w=_int(_attr(zone_el, "w"), 400),
        h=_int(_attr(zone_el, "h"), 300),
        is_floating=is_floating,
    )

    for child_el in zone_el.findall("zone"):
        zone.children.append(_parse_zone(child_el))

    return zone


def _parse_action(action_el) -> IRAction:
    action_type = _attr(action_el, "type", "filter")
    type_map = {
        "filter": "filter", "highlight": "highlight", "url": "url",
        "set": "set", "parameter": "parameter",
    }
    source_sheets = [_attr(s, "sheet") for s in action_el.findall(".//source-sheet")]
    target_sheets = [_attr(t, "sheet") for t in action_el.findall(".//target-sheet")]
    url = _attr(action_el, "url") or None

    return IRAction(
        id=uuid4(),
        name=_attr(action_el, "name", "Action"),
        action_type=type_map.get(action_type, "filter"),
        source_sheets=source_sheets,
        target_sheets=target_sheets,
        url=url,
        run_on=_attr(action_el, "run-on", "select") or "select",
    )


def parse_dashboard(dash_el) -> IRDashboard:
    name = _attr(dash_el, "name", "Dashboard")

    # Size
    size_el = dash_el.find("size")
    width = _int(_attr(size_el, "maxwidth", "1000") if size_el is not None else "1000", 1000)
    height = _int(_attr(size_el, "maxheight", "800") if size_el is not None else "800", 800)
    sizing_mode = _attr(size_el, "sizing", "fixed") if size_el is not None else "fixed"
    if sizing_mode not in ("fixed", "automatic", "range"):
        sizing_mode = "fixed"

    # Collect worksheet zones from anywhere in the dashboard tree.
    # Deduplicate by worksheet name — the same sheet can appear in multiple
    # nested layout containers but should only produce one visual on the page.
    zones: list[IRZone] = []
    seen_ws: set[str] = set()
    for zone_el in dash_el.findall(".//zone"):
        z = _parse_zone(zone_el)
        if z.zone_type == "worksheet" and z.worksheet_name:
            if z.worksheet_name not in seen_ws:
                seen_ws.add(z.worksheet_name)
                zones.append(z)

    actions: list[IRAction] = []
    for action_el in dash_el.findall(".//action"):
        actions.append(_parse_action(action_el))

    return IRDashboard(
        id=uuid4(),
        name=name,
        width=width,
        height=height,
        sizing_mode=sizing_mode,
        zones=zones,
        actions=actions,
    )
