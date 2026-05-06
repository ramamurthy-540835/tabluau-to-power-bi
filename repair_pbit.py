"""
repair_pbit.py
--------------
Fixes the corrupted MigratedReport.pbit for "Market Basket Analysis & Cross Selling".

Three bugs to fix:
  1. DataModelSchema / Settings / DiagramLayout / Report/Layout are stored as UTF-8
     instead of UTF-16 LE with BOM — the format Power BI Desktop requires.
  2. M syntax error: `Source = null,` (trailing comma before `in`) in the
     federated copy table makes the entire data model unparseable.
  3. Unfilled `<server>` placeholder in the Orders table M query.  Replaced
     with an ExcelFilePath template parameter so PBI Desktop prompts for the
     file path when the template is opened.
"""
import json
import zipfile
import shutil
from pathlib import Path

PBIT_PATH = Path(
    r"pbi_output\Market Basket Analysis _ Cross Selling\MigratedReport.pbit"
)
BACKUP_PATH = PBIT_PATH.with_suffix(".pbit.bak")

EXCEL_PARAM_EXPR = (
    '"" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'
)

EXCEL_PARAM_QUERY = (
    "// Power Query M for: Orders\n"
    "// NOTE: Enter the full path to your Excel / CSV file when Power BI Desktop prompts.\n"
    "let\n"
    "    Source = Excel.Workbook(File.Contents(ExcelFilePath), null, true),\n"
    "    FirstSheet = Source{0}[Data],\n"
    "    PromotedHeaders = Table.PromoteHeaders(FirstSheet, [PromoteAllScalars=true])\n"
    "in\n"
    "    PromotedHeaders"
)

NO_CONNECTION_QUERY = (
    "// Power Query M for data source: federated.1bsfma201593491fwux0e0f9o0l6 (copy)\n"
    "// [BLOCKER] No connection found in Tableau workbook.\n"
    "// Replace Source below with your real data connection.\n"
    "let\n"
    "    Source = #table(type table [], {})\n"
    "in\n"
    "    Source"
)


def _decode(raw: bytes) -> dict:
    for enc in ("utf-16", "utf-8"):
        try:
            return json.loads(raw.decode(enc))
        except Exception:
            pass
    raise ValueError("Cannot decode JSON from entry")


def _utf16(obj: dict) -> bytes:
    return json.dumps(obj, indent=2, ensure_ascii=True).encode("utf-16")


def _fix_data_model(schema: dict) -> dict:
    """Repair broken M queries and add ExcelFilePath parameter."""
    needs_excel_param = False

    for table in schema.get("model", {}).get("tables", []):
        tname = table.get("name", "")
        for part in table.get("partitions", []):
            src = part.get("source", {})
            expr = src.get("expression", "")

            # Bug 2: null M expression with trailing comma (syntax error)
            if "Source = null" in expr or "Source = null," in expr:
                print(f"  [FIX] Table '{tname}': replaced null M with valid empty table")
                src["expression"] = NO_CONNECTION_QUERY

            # Bug 3: unfilled <server> placeholder
            elif "<server>" in expr or "<file_path>" in expr:
                print(f"  [FIX] Table '{tname}': replaced <server> placeholder with ExcelFilePath param query")
                src["expression"] = EXCEL_PARAM_QUERY
                needs_excel_param = True

    # Add ExcelFilePath template parameter to the model when needed
    if needs_excel_param:
        existing_exprs = schema.get("model", {}).get("expressions", [])
        param_names = [e.get("name") for e in existing_exprs]
        if "ExcelFilePath" not in param_names:
            print("  [FIX] Adding ExcelFilePath template parameter to model")
            schema["model"]["expressions"] = existing_exprs + [{
                "name": "ExcelFilePath",
                "kind": "m",
                "expression": EXCEL_PARAM_EXPR,
                "annotations": [{"name": "PBI_ResultType", "value": "Text"}],
            }]

    return schema


def _fix_report_layout(layout: dict) -> dict:
    layout.pop("pods", None)
    return layout


def repair(pbit_path: Path) -> None:
    if not pbit_path.exists():
        raise FileNotFoundError(f"Not found: {pbit_path}")

    # Backup before modifying
    shutil.copy2(pbit_path, BACKUP_PATH)
    print(f"Backup written: {BACKUP_PATH}")

    with zipfile.ZipFile(pbit_path) as zf:
        raw_schema   = zf.read("DataModelSchema")
        raw_layout   = zf.read("Report/Layout")
        raw_settings = zf.read("Settings")
        raw_diagram  = zf.read("DiagramLayout")
        raw_version  = zf.read("Version")
        raw_security = zf.read("SecurityBindings")
        raw_ct       = zf.read("[Content_Types].xml")

        # Collect static resource files
        static_files = {
            name: zf.read(name)
            for name in zf.namelist()
            if name.startswith("Report/StaticResources/")
        }

    # --- Parse current content ---
    schema   = _decode(raw_schema)
    layout   = _decode(raw_layout)
    settings = _decode(raw_settings)
    diagram  = _decode(raw_diagram)

    # --- Apply fixes ---
    print("\nApplying fixes to DataModelSchema:")
    schema = _fix_data_model(schema)

    print("\nApplying fixes to Report/Layout:")
    before_keys = set(layout.keys())
    layout = _fix_report_layout(layout)
    removed = before_keys - set(layout.keys())
    if removed:
        print(f"  [FIX] Removed PBIP-only keys: {removed}")
    else:
        print("  [OK] No PBIP-only keys present")

    # --- Rebuild ZIP with correct UTF-16 LE encoding (Bug 1) ---
    tmp_path = pbit_path.with_suffix(".pbit.tmp")
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", raw_ct)           # UTF-8 — keep as-is
        zf.writestr("Version",             raw_version)      # plain text — keep
        zf.writestr("SecurityBindings",    raw_security)     # plain bytes — keep

        # Re-encode as UTF-16 LE with BOM (required by Power BI Desktop)
        zf.writestr("DataModelSchema", _utf16(schema))
        zf.writestr("DiagramLayout",   _utf16(diagram))
        zf.writestr("Report/Layout",   _utf16(layout))
        zf.writestr("Settings",        _utf16(settings))

        for name, data in static_files.items():
            zf.writestr(name, data)

    # Replace original with repaired file
    pbit_path.unlink()
    tmp_path.rename(pbit_path)

    print(f"\n[DONE] Repaired .pbit written: {pbit_path}")
    print("       Original backed up at:  ", BACKUP_PATH)

    # Validate
    _validate(pbit_path)


def _validate(pbit_path: Path) -> None:
    import re
    print("\nValidation:")
    with zipfile.ZipFile(pbit_path) as zf:
        # Check encoding
        raw = zf.read("DataModelSchema")
        bom = raw[:2].hex()
        if bom == "fffe":
            print("  [OK] DataModelSchema is UTF-16 LE with BOM")
        else:
            print(f"  [WARN] DataModelSchema BOM unexpected: {bom}")

        schema = json.loads(raw.decode("utf-16"))
        for table in schema.get("model", {}).get("tables", []):
            for part in table.get("partitions", []):
                expr = part.get("source", {}).get("expression", "")
                if "Source = null" in expr:
                    print(f"  [FAIL] Table '{table['name']}': still has null M")
                elif re.search(r'"<[^>"]+>"', expr):
                    print(f"  [FAIL] Table '{table['name']}': still has placeholder")
                else:
                    print(f"  [OK] Table '{table['name']}': M query is valid")

        raw_layout = zf.read("Report/Layout")
        bom2 = raw_layout[:2].hex()
        if bom2 == "fffe":
            print("  [OK] Report/Layout is UTF-16 LE with BOM")
        else:
            print(f"  [WARN] Report/Layout BOM unexpected: {bom2}")

        layout = json.loads(raw_layout.decode("utf-16"))
        if "pods" in layout:
            print("  [WARN] Report/Layout still has 'pods' key")
        sections = layout.get("sections", [])
        print(f"  [OK] Report has {len(sections)} page(s)")


if __name__ == "__main__":
    repair(PBIT_PATH)
