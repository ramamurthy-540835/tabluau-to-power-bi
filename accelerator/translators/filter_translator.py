"""
Translates Tableau filters and parameters to Power BI slicers and what-if parameters.
"""
from __future__ import annotations
import json
from uuid import uuid4

from accelerator.ir.schema import IRFilter, IRParameter, TranslationResult


def translate_filter(f: IRFilter) -> TranslationResult:
    caveats = []
    confidence = 0.9

    if f.is_context_filter:
        # Context filters become DAX CALCULATE wrappers — no direct slicer equivalent
        dax_snippet = (
            f"// Context filter on '{f.column_name}'\n"
            f"// Wrap affected measures in CALCULATE with explicit filter context:\n"
            f"// CALCULATE([Measure], KEEPFILTERS({f.column_name} = <value>))"
        )
        caveats.append("Context filter has no direct Power BI equivalent. Apply as measure-level CALCULATE filter.")
        return TranslationResult(
            source_id=f.id,
            target_artifact=dax_snippet,
            target_kind="measure",
            confidence=0.6,
            method="deterministic",
            caveats=caveats,
            needs_review=True,
            review_priority="high",
        )

    slicer = {
        "name": str(uuid4()).replace("-", "")[:16],
        "position": {"x": 0, "y": 0, "z": 0, "width": 200, "height": 300},
        "visual": {
            "visualType": "slicer",
            "projections": {
                "Values": [{"field": f.column_name or "Field", "displayName": f.column_name or "Field"}]
            },
            "objects": {
                "selection": [{"properties": {"selectAllCheckboxEnabled": {"bool": True}}}],
                "header": [{"properties": {"show": {"bool": True}}}],
            },
        },
        "filters": [],
    }

    if f.filter_type == "range":
        slicer["visual"]["objects"]["general"] = [{"properties": {"outlineColor": {"solid": {"color": "#000000"}}}}]
        slicer["visual"]["objects"]["data"] = [{"properties": {"mode": {"value": "Between"}}}]
        if f.min_value or f.max_value:
            caveats.append(f"Range filter: min={f.min_value}, max={f.max_value}. Configure in slicer settings.")

    if f.include_values:
        slicer["defaultFilteredItems"] = f.include_values[:50]
        if len(f.include_values) > 50:
            caveats.append(f"Filter has {len(f.include_values)} values — Power BI slicer shows top 50 by default.")

    return TranslationResult(
        source_id=f.id,
        target_artifact=json.dumps(slicer, indent=2),
        target_kind="visual",
        confidence=confidence,
        method="deterministic",
        caveats=caveats,
        needs_review=bool(caveats),
        review_priority="medium" if caveats else "low",
    )


def translate_parameter(param: IRParameter) -> TranslationResult:
    caveats = []

    if param.datatype in ("int", "decimal"):
        # What-if parameter
        tmdl = (
            f"// What-if parameter for Tableau parameter '{param.name}'\n"
            f"table '{param.name}'\n"
            f"    annotation PBI_Id = \"{uuid4()}\"\n\n"
            f"    column '{param.name}'\n"
            f"        dataType: {param.datatype}\n"
            f"        summarizeBy: none\n"
            f"        sourceColumn: '{param.name}'\n\n"
            f"    measure '{param.name} Value' = SELECTEDVALUE('{param.name}'[{param.name}], {param.current_value or '0'})\n"
        )
        if param.min_value and param.max_value:
            tmdl += f"    // Range: {param.min_value} to {param.max_value}, step: {param.step_size or '1'}\n"
        return TranslationResult(
            source_id=param.id,
            target_artifact=tmdl,
            target_kind="measure",
            confidence=0.85,
            method="deterministic",
            caveats=caveats,
            needs_review=False,
            review_priority="low",
        )

    # String/date parameter — use single-select disconnected table
    tmdl = (
        f"// Disconnected table parameter for '{param.name}'\n"
        f"table '{param.name} Param'\n"
        f"    column 'Option'\n"
        f"        dataType: string\n"
        f"        summarizeBy: none\n"
    )
    if param.allowable_values:
        tmdl += f"    // Allowable values: {', '.join(param.allowable_values[:10])}\n"
    tmdl += f"    measure 'Selected {param.name}' = SELECTEDVALUE('{param.name} Param'[Option], \"{param.current_value or ''}\") \n"
    caveats.append("Load allowable values as a static table in Power Query.")
    return TranslationResult(
        source_id=param.id,
        target_artifact=tmdl,
        target_kind="measure",
        confidence=0.75,
        method="deterministic",
        caveats=caveats,
        needs_review=True,
        review_priority="medium",
    )
