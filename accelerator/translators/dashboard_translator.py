"""
Translates IRDashboard layouts into Power BI report page JSON.
Implements the coordinate scaling algorithm from the spec.
"""
from __future__ import annotations
import json
from uuid import uuid4

from accelerator.ir.schema import IRDashboard, IRZone, IRAction, TranslationResult

PBI_PAGE_WIDTH = 1280
PBI_PAGE_HEIGHT = 720
GRID_SNAP = 10


def _snap(v: float, snap: int = GRID_SNAP) -> int:
    return int(round(v / snap) * snap)


def _scale_zone(zone: IRZone, sx: float, sy: float) -> dict:
    return {
        "name": str(uuid4()).replace("-", "")[:16],
        "worksheet_name": zone.worksheet_name,
        "x": _snap(zone.x * sx),
        "y": _snap(zone.y * sy),
        "width": max(_snap(zone.w * sx), 80),
        "height": max(_snap(zone.h * sy), 60),
        "is_floating": zone.is_floating,
        "zone_type": zone.zone_type,
    }


def _translate_action(action: IRAction) -> dict:
    type_map = {
        "filter": "crossFilter",
        "highlight": "crossHighlight",
        "url": "webUrl",
        "set": "crossFilter",
        "parameter": "parameter",
    }
    return {
        "type": type_map.get(action.action_type, "crossFilter"),
        "name": action.name,
        "source_sheets": action.source_sheets,
        "target_sheets": action.target_sheets,
        "url": action.url,
    }


def translate_dashboard(dash: IRDashboard) -> TranslationResult:
    src_w = dash.width or 1000
    src_h = dash.height or 800
    sx = min(PBI_PAGE_WIDTH / src_w, PBI_PAGE_HEIGHT / src_h)
    sy = sx   # uniform scale to preserve aspect

    caveats = []

    scaled_zones = []
    floating_zones = []
    for zone in dash.zones:
        sz = _scale_zone(zone, sx, sy)
        if sz["is_floating"]:
            floating_zones.append(sz)
            caveats.append(f"Zone '{zone.worksheet_name or zone.zone_type}' is floating — Power BI supports overlapping but it's discouraged. Review layout.")
        else:
            scaled_zones.append(sz)

    # Extract slicer zones (filters/parameters)
    slicer_zones = [z for z in scaled_zones if z.get("zone_type") in ("filter", "blank")]
    content_zones = [z for z in scaled_zones if z not in slicer_zones]

    # Build report page JSON
    page = {
        "name": str(uuid4()).replace("-", "")[:16],
        "displayName": dash.name,
        "width": PBI_PAGE_WIDTH,
        "height": PBI_PAGE_HEIGHT,
        "visualContainers": [],
        "actions": [_translate_action(a) for a in dash.actions],
        "config": {"defaultDisplayOption": "FitToPage"},
    }

    for zone in content_zones + floating_zones:
        page["visualContainers"].append({
            "x": zone["x"],
            "y": zone["y"],
            "z": 1000,
            "width": zone["width"],
            "height": zone["height"],
            "config": json.dumps({
                "name": zone["name"],
                "layouts": [{"id": 0, "position": {
                    "x": zone["x"], "y": zone["y"],
                    "width": zone["width"], "height": zone["height"],
                }}],
                "singleVisual": {
                    "visualType": "report",
                    "sourceWorksheet": zone.get("worksheet_name"),
                },
            }),
            "filters": "[]",
        })

    has_set_actions = any(a.action_type == "set" for a in dash.actions)

    if len(dash.actions) > 0:
        action_types = {a.action_type for a in dash.actions}
        if "url" in action_types:
            caveats.append("URL actions translated to action buttons — configure URL in button properties.")
        if has_set_actions:
            caveats.append(
                "Set Actions require manual rebuild in Power BI — there is no direct equivalent. "
                "Recommended approach: (1) create a disconnected slicer table, (2) use "
                "Field Parameters (Modeling > New Parameter > Fields) for dynamic selection, "
                "or (3) use Bookmarks + Selection Pane to toggle visuals. "
                "The cross-filter approximation here loses the Set membership semantic."
            )

    scale_note = f"Source: {src_w}×{src_h}px → Target: {PBI_PAGE_WIDTH}×{PBI_PAGE_HEIGHT}px (scale={sx:.2f})"
    caveats.append(scale_note)

    confidence = 0.85 if not floating_zones else 0.65
    if has_set_actions:
        confidence = min(confidence, 0.5)

    return TranslationResult(
        source_id=dash.id,
        target_artifact=json.dumps(page, indent=2),
        target_kind="page",
        confidence=confidence,
        method="deterministic",
        rationale="Dashboard layout scaled and zones mapped to visual containers.",
        caveats=caveats,
        needs_review=bool(floating_zones) or len(dash.actions) > 0 or has_set_actions,
        review_priority="high" if has_set_actions else ("medium" if floating_zones else "low"),
    )
