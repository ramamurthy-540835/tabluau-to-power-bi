"""
fix_all_outputs.py
------------------
Fixes the systemic "Missing required artifact model.bin" error across
every output directory in this project.

Root cause: definition.pbism version "1.0" tells Power BI Desktop to
look for model.bim (JSON) or model.bin (binary).  The generator only
produces TMDL text files, which PBI Desktop only reads when a different
mechanism exists.  Without model.bim, PBI Desktop falls through to
model.bin and shows "Missing required artifact model.bin".

Fix: build model.bim from the TMDL table files already present in each
SemanticModel folder.  model.bim is identical in structure to the
DataModelSchema in a .pbit — standard tabular model JSON written to a
UTF-8 file.  PBI Desktop reads it without any preview features enabled.

Run once from the project root:
    python fix_all_outputs.py
"""
from __future__ import annotations
import json
import re
import uuid
from pathlib import Path

# ── TMDL → BIM helpers ───────────────────────────────────────────────────────

_TMDL_DTYPE_TO_BIM = {
    "int64":    "int64",
    "decimal":  "decimal",
    "string":   "string",
    "boolean":  "boolean",
    "dateTime": "dateTime",
    "date":     "dateTime",
}

_TMDL_AGG_TO_BIM = {
    "none": "none", "sum": "sum", "average": "average",
    "count": "count", "distinctCount": "distinctCount",
    "min": "min", "max": "max",
}


def _parse_tmdl_table(tmdl_text: str) -> dict:
    """
    Parse a table .tmdl file into a BIM-compatible table dict.
    Handles columns, measures, and the partition M query.
    """
    lines = tmdl_text.splitlines()

    # Table name (first non-empty line)
    table_name = ""
    for ln in lines:
        m = re.match(r"^table '([^']+)'", ln.strip())
        if m:
            table_name = m.group(1)
            break
    if not table_name:
        return {}

    columns = []
    measures = []
    partition_m = []
    in_source = False
    source_indent = 0

    i = 0
    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()

        # Column block
        m = re.match(r"\s+column '([^']+)'", ln)
        if m:
            col_name = m.group(1)
            col = {
                "name": col_name,
                "dataType": "string",
                "summarizeBy": "none",
                "sourceColumn": col_name,
                "annotations": [{"name": "SummarizationSetBy", "value": "Automatic"}],
            }
            i += 1
            while i < len(lines):
                sub = lines[i].strip()
                if sub.startswith("dataType:"):
                    raw_dt = sub.split(":", 1)[1].strip()
                    col["dataType"] = _TMDL_DTYPE_TO_BIM.get(raw_dt, "string")
                elif sub.startswith("summarizeBy:"):
                    raw_ag = sub.split(":", 1)[1].strip()
                    col["summarizeBy"] = _TMDL_AGG_TO_BIM.get(raw_ag, "none")
                elif sub.startswith("sourceColumn:"):
                    col["sourceColumn"] = sub.split(":", 1)[1].strip()
                elif sub.startswith("displayName:"):
                    col["displayName"] = sub.split(":", 1)[1].strip().strip("'\"")
                elif sub.startswith("isHidden:"):
                    col["isHidden"] = sub.split(":", 1)[1].strip() == "true"
                elif sub == "" or sub.startswith("column ") or sub.startswith("measure ") \
                        or sub.startswith("partition ") or sub.startswith("annotation "):
                    break
                i += 1
            columns.append(col)
            continue

        # Measure block
        m = re.match(r"\s+measure '([^']+)'\s*=\s*(.+)", ln)
        if m:
            mname = m.group(1)
            expr_start = m.group(2).strip()
            # Collect multi-line expression
            expr_lines = [expr_start]
            i += 1
            while i < len(lines):
                sub = lines[i]
                stripped2 = sub.strip()
                if stripped2.startswith("formatString:") or \
                   stripped2.startswith("displayFolder:") or \
                   stripped2.startswith("annotation ") or \
                   stripped2 == "" and (i + 1 >= len(lines) or
                       re.match(r"\s+(measure|column|partition|annotation)\s", lines[i+1] if i+1 < len(lines) else "")):
                    break
                # Stop if we hit another TMDL block (non-indented under measure)
                if sub and not sub.startswith("  "):
                    break
                # Lines that are TMDL properties (formatString, displayFolder) - skip
                if stripped2.startswith("formatString:") or stripped2.startswith("displayFolder:"):
                    break
                expr_lines.append(sub.rstrip())
                i += 1
            # Collect formatString and displayFolder
            fmt = "#,0.00"
            folder = "Calculated Fields"
            while i < len(lines):
                sub2 = lines[i].strip()
                if sub2.startswith("formatString:"):
                    fmt = sub2.split(":", 1)[1].strip().strip('"')
                    i += 1
                elif sub2.startswith("displayFolder:"):
                    folder = sub2.split(":", 1)[1].strip().strip('"')
                    i += 1
                elif sub2.startswith("annotation ") or sub2 == "":
                    i += 1
                else:
                    break
            full_expr = "\n".join(expr_lines).strip()
            measures.append({
                "name": mname,
                "expression": full_expr,
                "formatString": fmt,
                "displayFolder": folder,
                "annotations": [{"name": "PBI_Id", "value": str(uuid.uuid4())}],
            })
            continue

        # Partition source block
        if stripped.startswith("source") and re.match(r"\s+source\s*$", ln):
            in_source = True
            source_indent = len(ln) - len(ln.lstrip()) + 4  # 12 spaces in TMDL
            i += 1
            continue

        if in_source:
            # Collect M source lines until we hit a non-source block
            if ln.strip() == "" or len(ln) - len(ln.lstrip()) >= source_indent:
                # Strip the fixed 12-space TMDL indent
                content = ln[source_indent:] if len(ln) >= source_indent else ln.lstrip()
                partition_m.append(content)
                i += 1
                continue
            else:
                in_source = False

        i += 1

    m_expr = "\n".join(partition_m).strip()

    table = {
        "name": table_name,
        "columns": columns,
        "partitions": [{
            "name": table_name,
            "mode": "import",
            "source": {"type": "m", "expression": m_expr},
        }],
        "annotations": [{"name": "PBI_Id", "value": str(uuid.uuid4())}],
    }
    if measures:
        table["measures"] = measures
    return table


def build_model_bim(sm_dir: Path) -> dict:
    """Build a BIM-format model dict from the TMDL files in a SemanticModel directory."""
    tables = []
    tables_dir = sm_dir / "tables"
    if tables_dir.exists():
        for tmdl_file in sorted(tables_dir.glob("*.tmdl")):
            text = tmdl_file.read_text(encoding="utf-8")
            table = _parse_tmdl_table(text)
            if table:
                tables.append(table)

    return {
        "name": "Model",
        "compatibilityLevel": 1550,
        "model": {
            "culture": "en-US",
            "tables": tables,
            "relationships": [],
            "annotations": [
                {"name": "PBIDesktopVersion", "value": "2.130.0.0"},
            ],
        },
    }


# ── Main fix ─────────────────────────────────────────────────────────────────

def fix_semantic_model_dir(sm_dir: Path) -> bool:
    """Write model.bim into a SemanticModel directory. Returns True if written."""
    if not sm_dir.exists():
        return False

    bim_path = sm_dir / "model.bim"

    # If model.bim already exists and is non-empty, skip
    if bim_path.exists() and bim_path.stat().st_size > 100:
        print(f"  SKIP  (model.bim already exists): {sm_dir}")
        return False

    model_bim = build_model_bim(sm_dir)
    if not model_bim["model"]["tables"]:
        print(f"  WARN  (no tables found in TMDL): {sm_dir}")
        # Still write an empty-but-valid bim so PBI Desktop doesn't error
    bim_path.write_text(json.dumps(model_bim, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  WROTE model.bim  ({bim_path.stat().st_size} bytes, "
          f"{len(model_bim['model']['tables'])} tables): {sm_dir}")
    return True


def fix_all(root: Path) -> None:
    fixed = 0
    for pbism in sorted(root.rglob("definition.pbism")):
        sm_dir = pbism.parent
        if fix_semantic_model_dir(sm_dir):
            fixed += 1

    print(f"\nDone. model.bim written to {fixed} SemanticModel directory/directories.")


if __name__ == "__main__":
    project_root = Path(__file__).parent
    print(f"Scanning: {project_root}\n")
    fix_all(project_root)
