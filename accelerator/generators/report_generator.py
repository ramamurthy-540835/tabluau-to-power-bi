"""
Generates Power BI report JSON (report.json) from translated dashboards and visuals.
Uses the correct PBIP enhanced-metadata format:
  - page.json: name/displayName/ordinal/visualContainers with flat x,y,width,height
  - each visual container: x,y,z,width,height at top level; visual definition in "config" JSON string
"""
from __future__ import annotations
import json
import re
import os
from pathlib import Path
from typing import List
from uuid import uuid4

from accelerator.ir.schema import IRMigrationUnit, TranslationResult
from accelerator.translators.visual_translator import translate_visual
from accelerator.translators.dashboard_translator import translate_dashboard


def _build_theme(unit: IRMigrationUnit) -> dict:
    from pathlib import Path
    import yaml
    theme_cfg = Path(__file__).parent.parent.parent / "config" / "theme_palette.yaml"
    colors = ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F",
               "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#BAB0AC"]
    if theme_cfg.exists():
        cfg = yaml.safe_load(theme_cfg.read_text(encoding="utf-8"))
        palette_name = cfg.get("default_palette", "tableau10")
        colors = cfg.get("tableau_palettes", {}).get(palette_name, {}).get("colors", colors)
    return {
        "name": "Tableau Migration Theme",
        "dataColors": colors,
        "background": "#FFFFFF",
        "foreground": "#252423",
        "tableAccent": colors[0] if colors else "#4E79A7",
    }


def _safe_folder_name(name: str, max_len: int = 60) -> str:
    """Sanitize display name for use as a Windows folder name."""
    safe = name.replace("\\", "_").replace("/", "_")
    safe = re.sub(r'[:*?"<>|\x00-\x1f]', "_", safe)
    safe = re.sub(r"_+", "_", safe).strip("_. ")
    return safe[:max_len] or "page"


def _page_folder_name() -> str:
    """Return a ReportSection-style unique page folder name matching PBI convention."""
    return "ReportSection" + str(uuid4()).replace("-", "")[:8]


def _make_slicer_container(col_name: str, table_name: str,
                           x: int, y: int, tab_order: int = 20000) -> dict:
    """Build a slicer visual container for a single field."""
    slicer_name = str(uuid4()).replace("-", "")[:16]
    safe_col = col_name.replace("'", "\\'")
    visual_config = {
        "name": slicer_name,
        "layouts": [{"id": 0, "position": {
            "x": x, "y": y, "z": 100,
            "width": 200, "height": 260, "tabOrder": tab_order,
        }}],
        "singleVisual": {
            "visualType": "slicer",
            "projections": {},
            "objects": {
                "title": [{"properties": {
                    "text": {"expr": {"Literal": {"Value": f"'{safe_col}'"}}},
                    "show": {"type": "Boolean", "fixed": {"value": True}},
                }}],
            },
            "drillFilterOtherVisuals": True,
            "vcObjects": {},
        },
    }
    return {
        "x": x, "y": y, "z": 100,
        "width": 200, "height": 260,
        "config": json.dumps(visual_config, separators=(",", ":")),
        "filters": "[]",
    }


def _make_container(visual_data: dict, x: int, y: int, width: int, height: int,
                    table_name: str, tab_order: int = 10000) -> dict:
    """
    Build a PBIP Enhanced Metadata Format visual container.

    PBIP EMF (Power BI Desktop 2024+):
      - Top-level: x, y, z, width, height, config (JSON string), filters
      - config.singleVisual.visualType is a plain string — this is what PBI
        Desktop renders.  The old vcObjects-at-config-level approach (used in
        some early PBIP samples) is NOT supported by current PBI Desktop and
        causes visuals to be silently ignored.
    """
    vis = visual_data.get("visual", {})
    visual_type = vis.get("visualType", "clusteredBarChart")
    title_text  = vis.get("title", {}).get("text", "")
    raw_projections = vis.get("projections", {})  # from visual_translator

    # Convert shelf projections to PBIP queryRef format
    pbi_projections: dict = {}
    for slot, fields in raw_projections.items():
        refs = []
        for f_info in fields:
            field_name = f_info.get("field", "") if isinstance(f_info, dict) else str(f_info)
            if field_name and table_name:
                refs.append({"queryRef": f"{table_name}.{field_name}", "active": True})
            elif field_name:
                refs.append({"queryRef": field_name, "active": True})
        if refs:
            pbi_projections[slot] = refs

    visual_name = str(uuid4()).replace("-", "")[:16]

    objects: dict = {}
    if title_text:
        safe_title = title_text.replace("'", "\\'")
        objects["title"] = [{
            "properties": {
                "text": {"expr": {"Literal": {"Value": f"'{safe_title}'"}}},
                "show": {"type": "Boolean", "fixed": {"value": True}},
            }
        }]

    visual_config = {
        "name": visual_name,
        "layouts": [{
            "id": 0,
            "position": {
                "x": x, "y": y, "z": 0,
                "width": width, "height": height,
                "tabOrder": tab_order,
            },
        }],
        "singleVisual": {
            "visualType": visual_type,
            "projections": pbi_projections,
            "objects": objects,
            "drillFilterOtherVisuals": True,
            "vcObjects": {},
        },
    }
    return {
        "x": x, "y": y, "z": 0,
        "width": width, "height": height,
        "config": json.dumps(visual_config, separators=(",", ":")),
        "filters": "[]",
    }


def _grid_containers(ws_visuals: dict, ws_to_table: dict) -> list:
    """Lay out all worksheets in a 2-column grid on one page."""
    COLS, PADDING, CELL_H = 2, 20, 300
    cell_w = (1280 - PADDING * (COLS + 1)) // COLS
    result = []
    for idx, (ws_name, visual) in enumerate(ws_visuals.items()):
        col = idx % COLS
        row = idx // COLS
        x = PADDING + col * (cell_w + PADDING)
        y = PADDING + row * (CELL_H + PADDING)
        table_name = ws_to_table.get(ws_name, "")
        result.append(_make_container(visual, x, y, cell_w, CELL_H, table_name,
                                      tab_order=(idx + 1) * 10000))
    return result


def _write_page(pages_dir: Path, display_name: str, ordinal: int,
                containers: list) -> dict:
    """Write one page.json and return the page dict."""
    folder = _page_folder_name()
    page_json = {
        "name": folder,
        "displayName": display_name,
        "ordinal": ordinal,
        "displayOption": 0,
        "config": json.dumps({"version": "5.47", "defaultDrillFilterOtherVisuals": True}),
        "filters": "[]",
        "visualContainers": containers,
    }
    page_path = pages_dir / folder
    page_path.mkdir(parents=True, exist_ok=True)
    (page_path / "page.json").write_text(json.dumps(page_json, indent=2), encoding="utf-8")
    return page_json


_SLICER_X      = 1060   # right panel start (canvas is 1280 wide)
_SLICER_WIDTH  = 200
_SLICER_HEIGHT = 260
_SLICER_GAP    = 10


def _slicers_for_worksheets(ws_names: list[str], unit: IRMigrationUnit,
                             ws_to_table: dict) -> list[dict]:
    """Build slicer containers for filters found on the given worksheets."""
    seen_cols: set[str] = set()
    slicers: list[dict] = []
    for ws_name in ws_names:
        ws = next((w for w in unit.worksheets if w.name == ws_name), None)
        if ws is None:
            continue
        table_name = ws_to_table.get(ws_name, "")
        for f in ws.filters:
            col = f.column_name or ""
            if not col or col in seen_cols or f.is_context_filter:
                continue
            seen_cols.add(col)
            idx = len(slicers)
            y = 20 + idx * (_SLICER_HEIGHT + _SLICER_GAP)
            if y + _SLICER_HEIGHT > 720:
                break  # stay within canvas
            slicers.append(_make_slicer_container(
                col, table_name, _SLICER_X, y, tab_order=20000 + idx * 1000
            ))
    return slicers


def generate_report(unit: IRMigrationUnit, output_dir: Path) -> List[TranslationResult]:
    results = []
    report_dir = output_dir / "Report"
    pages_dir = report_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    # Worksheet → PBI table name
    from accelerator.generators.tmdl_generator import _safe_table_name
    ws_to_table: dict[str, str] = {}
    for ds in unit.data_sources:
        raw = ds.tables[0].name if ds.tables else ds.name
        tbl_name = _safe_table_name(raw)
        for ws in unit.worksheets:
            if ws.data_source_id == ds.id:
                ws_to_table[ws.name] = tbl_name

    # Translate all worksheets to visual dicts (old internal format)
    ws_visuals: dict[str, dict] = {}
    ws_name_lower: dict[str, str] = {}
    for ws in unit.worksheets:
        vr = translate_visual(ws)
        ws_visuals[ws.name] = json.loads(vr.target_artifact)
        ws_name_lower[ws.name.strip().lower()] = ws.name
        results.append(vr)

    pages = []

    # ── One full-canvas page per Tableau worksheet (always) ──────────────────
    # Each Tableau worksheet tab becomes its own Power BI page — the visual
    # fills the canvas (leaving 200 px on the right for slicers).
    CANVAS_W  = 1240   # usable visual width (1280 canvas - 40 padding)
    CANVAS_H  = 680    # usable visual height
    PADDING   = 20

    for ordinal, ws in enumerate(unit.worksheets):
        visual     = ws_visuals.get(ws.name, {})
        table_name = ws_to_table.get(ws.name, "")
        # Visual occupies left portion; slicers stack on the right
        vis_w = _SLICER_X - PADDING * 2   # stop before slicer panel
        container = _make_container(
            visual, PADDING, PADDING, vis_w, CANVAS_H, table_name,
            tab_order=10000,
        )
        slicers = _slicers_for_worksheets([ws.name], unit, ws_to_table)
        pages.append(_write_page(pages_dir, ws.name, ordinal, [container] + slicers))
        results.append(TranslationResult(
            source_id=ws.id,
            target_artifact=ws.name,
            target_kind="page",
            confidence=0.95,
            method="deterministic",
        ))

    # ── Dashboard overview pages (one per Tableau dashboard) ─────────────────
    # Tableau dashboards combine multiple worksheets — migrated as an additional
    # summary page so stakeholders see the same composed view they had in Tableau.
    for dash_idx, dash in enumerate(unit.dashboards):
        ordinal = len(unit.worksheets) + dash_idx
        dr = translate_dashboard(dash)
        results.append(dr)

        containers = []
        src_w = dash.width or 1000
        src_h = dash.height or 800
        scale = min(1280 / src_w, 720 / src_h)

        for zone in dash.zones:
            resolved = ws_name_lower.get((zone.worksheet_name or "").strip().lower())
            if not (zone.worksheet_name and resolved in ws_visuals):
                continue

            visual     = ws_visuals[resolved]
            table_name = ws_to_table.get(resolved, "")

            x = int(round(zone.x * scale / 10) * 10)
            y = int(round(zone.y * scale / 10) * 10)
            w = max(int(round(zone.w * scale / 10) * 10), 80)
            h = max(int(round(zone.h * scale / 10) * 10), 60)

            if x > 1280 or y > 720 or w > 1280 or h > 720:
                x = y = w = h = None

            if x is not None:
                containers.append(_make_container(visual, x, y, w, h, table_name))
            else:
                containers.append(None)

        # Fix out-of-bounds placements with grid fallback
        if any(c is None for c in containers):
            COLS, CELL_H = 2, 300
            cell_w = (1280 - PADDING * (COLS + 1)) // COLS
            grid_idx = 0
            fixed = []
            for c in containers:
                if c is None:
                    col_i = grid_idx % COLS
                    row_i = grid_idx // COLS
                    gx = PADDING + col_i * (cell_w + PADDING)
                    gy = PADDING + row_i * (CELL_H + PADDING)
                    resolved_name = list(ws_visuals.keys())[grid_idx] if grid_idx < len(ws_visuals) else None
                    if resolved_name:
                        fixed.append(_make_container(
                            ws_visuals[resolved_name], gx, gy, cell_w, CELL_H,
                            ws_to_table.get(resolved_name, ""),
                            tab_order=(grid_idx + 1) * 10000,
                        ))
                    grid_idx += 1
                else:
                    fixed.append(c)
                    grid_idx += 1
            containers = fixed

        if not containers and ws_visuals:
            containers = _grid_containers(ws_visuals, ws_to_table)

        ws_on_dash = [z.worksheet_name for z in dash.zones if z.worksheet_name]
        containers += _slicers_for_worksheets(ws_on_dash, unit, ws_to_table)

        # Label dashboard pages clearly so users can distinguish them from
        # individual worksheet pages in the Power BI page tab bar.
        dash_page_name = f"{dash.name} (Overview)" if unit.worksheets else dash.name
        pages.append(_write_page(pages_dir, dash_page_name, ordinal, containers))

    # ── report.json ──────────────────────────────────────────────────────────
    # sections must be full page objects (same dicts as page.json) in ordinal
    # order.  PBI Desktop uses this as the authoritative page index — without
    # it the page tab bar stays empty even though page.json files exist on disk.
    sections_ordered = sorted(pages, key=lambda pg: pg["ordinal"])

    theme = _build_theme(unit)
    report_json = {
        "id": str(uuid4()),
        "resourcePackages": [],
        "sections": sections_ordered,  # full page dicts, not folder name strings
        "config": json.dumps({
            "version": "5.47",
            "activeSectionIndex": 0,
            "defaultDrillFilterOtherVisuals": True,
            "themeCollection": {
                "baseTheme": {
                    # Must be a built-in PBI theme name.
                    "name": "CY24SU10",
                    "version": "5.47",
                    "type": 2,
                },
            },
        }),
        "filters": "[]",
    }
    (report_dir / "report.json").write_text(json.dumps(report_json, indent=2), encoding="utf-8")
    (report_dir / "definition.pbir").write_text(
        json.dumps({"version": "1.0", "datasetReference": {"byPath": {"path": "../SemanticModel"}}}, indent=2),
        encoding="utf-8"
    )

    return results
