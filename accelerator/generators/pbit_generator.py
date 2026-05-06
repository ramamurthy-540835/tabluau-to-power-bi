"""
pbit_generator.py
Builds a Power BI Template (.pbit) file from the already-generated
PBIP output folder (Report/ + SemanticModel/).

A .pbit is a ZIP archive with this internal structure:
    [Content_Types].xml
    DataModelSchema          ← JSON string of the tabular model
    DiagramLayout            ← JSON string (can be empty object)
    Report/
        Layout               ← JSON string of the report layout
        StaticResources/
            RegisteredResources/
    SecurityBindings         ← empty JSON array "[]"
    Settings                 ← JSON string of report settings
    Version                  ← plain text, e.g. "2.0"

Key differences from PBIP:
  - Single file instead of folder tree
  - DataModelSchema is the full model as one big JSON (not split .tmdl files)
  - Report Layout is the full report.json content as one JSON string
  - No TMDL — uses the older JSON-based tabular model format

Usage:
    from accelerator.generators.pbit_generator import build_pbit

    build_pbit(
        project_name="MigratedReport",
        output_dir=Path("./pbi_output/MyWorkbook"),
        unit=unit,
        translations=translations,
    )
    # Writes: ./pbi_output/MyWorkbook/MigratedReport.pbit
"""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from uuid import uuid4

from accelerator.ir.schema import IRMigrationUnit, IRDataSource, IRColumn, TranslationResult

# ── Internal helpers ──────────────────────────────────────────────────────────

_DTYPE_TO_TABULAR = {
    "int":      "int64",
    "decimal":  "decimal",
    "string":   "string",
    "boolean":  "boolean",
    "date":     "dateTime",
    "datetime": "dateTime",
    "unknown":  "string",
}

_AGG_TO_TABULAR = {
    "sum":    "sum",
    "avg":    "average",
    "count":  "count",
    "countd": "distinctCount",
    "min":    "min",
    "max":    "max",
    "median": "none",
}


def _build_data_model_schema(unit: IRMigrationUnit) -> dict:
    """
    Build the DataModelSchema JSON that goes inside the .pbit ZIP.
    This is the tabular model in the older Power BI JSON format
    (not TMDL).  Power BI Desktop reads this when opening the .pbit.

    One Power BI table is created per IRDataSource (using the first physical
    table name, or the data source name).  All non-calculated columns from
    the data source are assigned to that single table.  This avoids the
    duplicate-column problem that occurs when a data source has multiple
    joined tables but the IR stores columns at the data source level.
    """
    from accelerator.generators.tmdl_generator import _safe_table_name
    from accelerator.generators.m_generator import generate_query

    tables = []
    seen_table_names: set[str] = set()
    measures_added: bool = False

    for ds in unit.data_sources:
        # ── One physical table per data source ────────────────────────────
        # Use the first table name when available; fall back to the data
        # source name.  Columns from joined tables are all exposed as a
        # flat list in the IR so they all belong to this single PBI table.
        raw_name = ds.tables[0].name if ds.tables else ds.name
        safe = _safe_table_name(raw_name)
        if safe not in seen_table_names:
            seen_table_names.add(safe)

            # Deduplicate physical columns by name
            seen_cols: set[str] = set()
            columns = []
            for col in ds.columns:
                if col.is_calculated:
                    continue
                if col.name in seen_cols:
                    continue
                seen_cols.add(col.name)
                dtype = _DTYPE_TO_TABULAR.get(col.datatype, "string")
                agg   = _AGG_TO_TABULAR.get(col.aggregation or "", "none") \
                        if col.role == "measure" else "none"
                # Use business_name as the display name (the 'name' field IS the
                # display name in BIM JSON — 'displayName' is NOT a valid BIM
                # column property and causes "Unrecognized JSON property" on open).
                display = col.business_name if col.business_name else col.name
                col_obj: dict = {
                    "name":         display,
                    "dataType":     dtype,
                    "summarizeBy":  agg,
                    "sourceColumn": col.name,
                    "annotations":  [{"name": "SummarizationSetBy", "value": "Automatic"}],
                }
                columns.append(col_obj)

            # Always use the m_generator query; it handles Hyper extracts with a
            # proper #table() query that does not require any user input.
            m_expr = generate_query(ds)

            tables.append({
                "name":       safe,
                "columns":    columns,
                "partitions": [{
                    "name":   safe,
                    "mode":   "import",
                    "source": {"type": "m", "expression": m_expr},
                }],
                "annotations": [{"name": "PBI_Id", "value": str(uuid4())}],
            })

        # ── Measures table (one shared table for all calculated fields) ───
        calc_cols = [c for c in ds.columns if c.is_calculated]
        if calc_cols and not measures_added:
            measures_added = True
            seen_table_names.add("_Measures")

            measures = []
            from accelerator.generators.tmdl_generator import _fix_tableau_dax
            for col in calc_cols:
                dax = col.dax_expression or f"// TODO: translate\n// {col.formula}"
                dax_clean = _fix_tableau_dax(_strip_measure_prefix(dax, col.business_name or col.name))
                measures.append({
                    "name":          col.business_name or col.name,
                    "expression":    dax_clean,
                    "displayFolder": "Calculated Fields",
                    "annotations":   [{"name": "PBI_Id", "value": str(uuid4())}],
                })

            tables.append({
                "name":    "_Measures",
                "columns": [{
                    "name":         "_placeholder",
                    "dataType":     "int64",
                    "isHidden":     True,
                    "summarizeBy":  "none",
                    "sourceColumn": "_placeholder",
                }],
                "measures":   measures,
                "partitions": [{
                    "name":   "_Measures-Partition",
                    "mode":   "import",
                    "source": {"type": "m", "expression": "let\n    Source = #table(\n        type table [_placeholder = Int64.Type],\n        {}\n    )\nin\n    Source"},
                }],
                "annotations": [{"name": "PBI_Id", "value": str(uuid4())}],
            })

    # ── Relationships ─────────────────────────────────────────────────────
    relationships = []
    for ds in unit.data_sources:
        for rel in ds.relationships:
            from_tbl = next((t for t in ds.tables if t.id == rel.from_table), None)
            to_tbl   = next((t for t in ds.tables if t.id == rel.to_table), None)
            if not from_tbl or not to_tbl:
                continue
            relationships.append({
                "name":             f"{from_tbl.name}_{to_tbl.name}",
                "fromTable":        from_tbl.name,
                "fromColumn":       "<resolve_join_column>",
                "toTable":          to_tbl.name,
                "toColumn":         "<resolve_join_column>",
                "crossFilteringBehavior": "OneDirection",
                "isActive":         rel.is_active,
            })

    model_obj: dict = {
        "culture":       "en-US",
        "tables":        tables,
        "relationships": relationships,
        "annotations": [
            {"name": "PBI_QueryOrder",  "value": json.dumps([ds.name for ds in unit.data_sources])},
            {"name": "PBIDesktopVersion", "value": "2.130.0.0"},
        ],
    }

    return {
        "name":               "Model",
        "compatibilityLevel": 1550,
        "model":              model_obj,
    }



def _strip_measure_prefix(dax: str, measure_name: str) -> str:
    """
    The deterministic translator writes "Measure 'Name' = <expr>".
    Extract just the <expr> part for the DataModelSchema measures array.
    """
    prefix = f"Measure '{measure_name}' = "
    prefix2 = f"Measure \"{measure_name}\" = "
    if dax.startswith(prefix):
        return dax[len(prefix):]
    if dax.startswith(prefix2):
        return dax[len(prefix2):]
    return dax


def _build_report_layout(output_dir: Path, project_name: str) -> dict:
    """
    Build the Report/Layout JSON for the .pbit ZIP.

    PBIP stores pages as individual pages/<name>/page.json files; .pbit needs
    all pages as a "sections" array inside Report/Layout.  Read both the
    top-level report.json metadata AND every page.json, then combine them.
    """
    report_dir = output_dir / f"{project_name}.Report"
    if not report_dir.exists():
        report_dir = output_dir / "Report"

    # Top-level report metadata (id, config, filters, resourcePackages)
    report_json_path = report_dir / "report.json"
    if report_json_path.exists():
        layout = json.loads(report_json_path.read_text(encoding="utf-8"))
    else:
        layout = {
            "id":               str(uuid4()),
            "resourcePackages": [],
            "config":           json.dumps({"version": "5.47"}),
            "filters":          "[]",
        }

    # Collect pages from the PBIP pages/ subfolder and add as sections
    pages_dir = report_dir / "pages"
    sections = []
    if pages_dir.exists():
        for page_json_path in sorted(pages_dir.rglob("page.json")):
            try:
                page = json.loads(page_json_path.read_text(encoding="utf-8"))
                sections.append(page)
            except Exception:
                pass

    layout["sections"] = sections
    # Remove PBIP-only keys that are invalid in .pbit layout
    layout.pop("pods", None)
    return layout


_FILE_PARAM_CLASSES = frozenset({"hyper", "extract", "tableau-extract"})
_FILE_BASED_CLASSES = frozenset({"excel-direct", "text-csv"})


def _use_excel_param(ds) -> bool:
    """True when the data source should use an ExcelFilePath template parameter."""
    if not ds.connections:
        return False
    cls = ds.connections[0].connection_class.lower()
    if cls in _FILE_PARAM_CLASSES:
        return True
    return cls in _FILE_BASED_CLASSES and not ds.connections[0].server


def _build_excel_param_query(ds) -> str:
    """M query using ExcelFilePath parameter — prompts user on .pbit open."""
    return (
        f"// Power Query M for: {ds.name}\n"
        "// NOTE: Enter the full path to your Excel file when Power BI Desktop prompts\n"
        "let\n"
        "    Source = Excel.Workbook(File.Contents(ExcelFilePath), null, true),\n"
        "    FirstSheet = Source{0}[Data],\n"
        "    PromotedHeaders = Table.PromoteHeaders(FirstSheet, [PromoteAllScalars=true])\n"
        "in\n"
        "    PromotedHeaders"
    )


def _validate_pbit_issues(pbit_path) -> list:
    """Return a list of fatal issues found in the generated .pbit."""
    import re as _re
    issues = []
    def _read_json(zf, name):
        data = zf.read(name)
        # Files are UTF-16 LE with BOM; fall back to UTF-8 for older outputs
        try:
            return json.loads(data.decode("utf-16"))
        except Exception:
            return json.loads(data.decode("utf-8"))

    try:
        with zipfile.ZipFile(pbit_path) as zf:
            schema = _read_json(zf, "DataModelSchema")
            for table in schema.get("model", {}).get("tables", []):
                for part in table.get("partitions", []):
                    expr = part.get("source", {}).get("expression", "")
                    if "Source = null" in expr:
                        issues.append(f"Table '{table['name']}': Source = null — will crash on load")
                    if _re.search(r'"<[^>"]+>"', expr):
                        issues.append(f"Table '{table['name']}': unfilled placeholder in M query")
            layout = _read_json(zf, "Report/Layout")
            if not layout.get("sections"):
                issues.append("Report has no pages — blank report")
    except Exception as e:
        issues.append(f"Validation error: {e}")
    return issues


def _content_types_xml() -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="json" ContentType="application/json"/>\n'
        '  <Default Extension="xml"  ContentType="application/xml"/>\n'
        '  <Override PartName="/DataModelSchema" ContentType="application/json"/>\n'
        '  <Override PartName="/DiagramLayout"   ContentType="application/json"/>\n'
        '  <Override PartName="/Report/Layout"   ContentType="application/json"/>\n'
        '  <Override PartName="/SecurityBindings" ContentType="application/json"/>\n'
        '  <Override PartName="/Settings"        ContentType="application/json"/>\n'
        '  <Override PartName="/Version"         ContentType="application/octet-stream"/>\n'
        '</Types>'
    )


# ── Public API ────────────────────────────────────────────────────────────────

def write_model_bim(project_name: str, unit: IRMigrationUnit, output_dir: Path) -> Path:
    """
    Write model.bim to the SemanticModel folder.

    Power BI Desktop requires either model.bim (JSON) or model.bin (binary) to
    open a PBIP semantic model.  Without it PBI Desktop errors with
    'Missing required artifact model.bin'.  model.bim is the JSON equivalent
    and is supported by all modern PBI Desktop versions.

    Must be called BEFORE packager.package() so the file is present when the
    folder is renamed and the PBIP ZIP is created.
    """
    data_model = _build_data_model_schema(unit)
    # Always write to the pre-rename path ("SemanticModel/") so packager carries
    # model.bim along when it renames the folder.  Fall back to the renamed path
    # only if the pre-rename folder no longer exists (e.g. called after packager).
    model_dir = output_dir / "SemanticModel"
    if not model_dir.exists():
        model_dir = output_dir / f"{project_name}.SemanticModel"
    model_dir.mkdir(parents=True, exist_ok=True)
    bim_path = model_dir / "model.bim"
    bim_path.write_text(json.dumps(data_model, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[pbit] model.bim written: {bim_path}")
    return bim_path


def build_pbit(
    project_name: str,
    unit: IRMigrationUnit,
    translations: List[TranslationResult],
    output_dir: Path,
) -> Path:
    """
    Build a .pbit file from the IRMigrationUnit and already-generated output.

    Call this INSTEAD OF (or in addition to) packager.package() depending on
    which output format is required.

    Returns the path to the written .pbit file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    pbit_path = output_dir / f"{project_name}.pbit"

    # 1. Build DataModelSchema (the tabular model JSON)
    data_model = _build_data_model_schema(unit)

    # 2. Build Report Layout
    report_layout = _build_report_layout(output_dir, project_name)

    # 3. Diagram layout — empty is fine, PBI will auto-generate
    diagram_layout = {
        "version": 1,
        "positionedItems": [],
    }

    # 4. Settings
    settings = {
        "version": "1.0",
        "isTemplate": True,
        "createdByVersion": "2.130.0",
        "createdDate": datetime.now(timezone.utc).isoformat(),
    }

    # 5. Write ZIP
    # Encoding rules for .pbit internal files (verified against PBI Desktop output):
    #   DataModelSchema  — UTF-16 LE with BOM  (fffe prefix)
    #   Report/Layout    — UTF-16 LE with BOM
    #   Settings         — UTF-16 LE with BOM
    #   DiagramLayout    — UTF-16 LE with BOM
    #   SecurityBindings — UTF-8 plain  (PBI Desktop writes it as raw "[]")
    #   Version          — UTF-8 plain
    #   [Content_Types].xml — UTF-8
    # ensure_ascii=True on all JSON to avoid any non-ASCII chars that could trip up
    # Power BI Desktop's parser (e.g. unicode warning symbols in M comments).
    def _utf16(s: str) -> bytes:
        return s.encode("utf-16")  # Python utf-16 codec prepends FF FE BOM automatically

    with zipfile.ZipFile(pbit_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",  _content_types_xml().encode("utf-8"))
        zf.writestr("Version",              b"2.0")
        zf.writestr("SecurityBindings",     b"[]")
        zf.writestr("DataModelSchema",      _utf16(json.dumps(data_model,     indent=2, ensure_ascii=True)))
        zf.writestr("DiagramLayout",        _utf16(json.dumps(diagram_layout, indent=2, ensure_ascii=True)))
        zf.writestr("Report/Layout",        _utf16(json.dumps(report_layout,  indent=2, ensure_ascii=True)))
        zf.writestr("Settings",             _utf16(json.dumps(settings,       indent=2, ensure_ascii=True)))

        # Include M queries as static resources for reference
        queries_src = output_dir / f"{project_name}.SemanticModel" / "queries"
        if not queries_src.exists():
            queries_src = output_dir / "SemanticModel" / "queries"
        if queries_src.exists():
            for m_file in queries_src.glob("*.m"):
                zf.writestr(
                    f"Report/StaticResources/RegisteredResources/{m_file.name}",
                    m_file.read_text(encoding="utf-8"),
                )

    print(f"[pbit] Written: {pbit_path}")

    # Post-generation validation — catch fatal issues before the user tries to open
    issues = _validate_pbit_issues(pbit_path)
    if issues:
        print("[pbit] WARNING: Validation issues found:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("[pbit] OK: Validation passed - no fatal issues detected")

    return pbit_path
