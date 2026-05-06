"""
Translates IRWorksheet objects into Power BI visual JSON fragments.
Uses mark_visual_map.yaml for type mapping.
"""
from __future__ import annotations
import json
from pathlib import Path
from uuid import uuid4
import yaml

from accelerator.ir.schema import IRWorksheet, TranslationResult

_CONFIG = Path(__file__).parent.parent.parent / "config" / "mark_visual_map.yaml"

_NEEDS_CUSTOM = {"custom_gantt", "custom_deneb"}


def _load_map() -> dict:
    if _CONFIG.exists():
        return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    return {}


def translate_visual(ws: IRWorksheet) -> TranslationResult:
    vmap = _load_map()
    entry = vmap.get(ws.mark_type) or vmap.get("auto", {})
    pbi_type = entry.get("pbi_visual", "clusteredBarChart")
    notes = entry.get("notes", "")
    shelf_mapping: dict = entry.get("shelf_mapping", {})

    needs_custom = pbi_type in _NEEDS_CUSTOM
    caveats = []
    if notes:
        caveats.append(notes)
    if needs_custom:
        caveats.append(f"This visual requires a custom or AppSource visual: {pbi_type}.")

    # Build field projections
    projections: dict[str, list] = {}
    for shelf, pbi_field in shelf_mapping.items():
        if not pbi_field or pbi_field == "~":
            continue
        fields = []
        if shelf == "rows":
            fields = ws.shelves.rows
        elif shelf == "cols":
            fields = ws.shelves.cols
        elif shelf == "color" and ws.shelves.color:
            fields = [ws.shelves.color]
        elif shelf == "size" and ws.shelves.size:
            fields = [ws.shelves.size]
        elif shelf == "label" and ws.shelves.label:
            fields = [ws.shelves.label]
        elif shelf == "detail":
            fields = ws.shelves.detail
        elif shelf == "tooltip":
            fields = ws.shelves.tooltip

        if fields:
            projections[pbi_field] = [{"field": f, "displayName": f} for f in fields]

    visual_json = {
        "name": str(uuid4()).replace("-", "")[:16],
        "position": {"x": 0, "y": 0, "z": 0, "width": 400, "height": 300},
        "visual": {
            "visualType": pbi_type,
            "projections": projections,
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": "t", "Entity": "<table>", "Type": 0}],
                "Select": [
                    {"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": f}, "Name": f}
                    for shelf_fields in projections.values()
                    for item in shelf_fields
                    for f in [item["field"]]
                ],
            },
            "title": {"show": True, "text": ws.name},
        },
        "filters": [],
    }

    confidence = 0.9 if not needs_custom else 0.5
    return TranslationResult(
        source_id=ws.id,
        target_artifact=json.dumps(visual_json, indent=2),
        target_kind="visual",
        confidence=confidence,
        method="deterministic",
        caveats=caveats,
        needs_review=needs_custom or bool(caveats),
        review_priority="high" if needs_custom else "low",
    )
