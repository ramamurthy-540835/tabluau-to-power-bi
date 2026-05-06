"""
repair_pbit2.py
---------------
Fixes casing errors in DataModelSchema that cause Power BI Desktop to reject
the .pbit as "corrupted":
  - dataType values: "Int64" → "int64", "String" → "string",
                     "DateTime" → "dateTime", "Decimal" → "decimal", etc.
  - summarizeBy values: "None" → "none", "Sum" → "sum", etc.
  - partition mode: "Import" → "import"
  - Measures partition M: #table({},{}) → #table(type table [], {})

TMSL (the tabular model schema language) enum values are case-sensitive.
Power BI Desktop rejects unknown enum values and shows "encrypted or corrupted".
"""
import json, zipfile, shutil
from pathlib import Path

PBIT_PATH = Path(
    r"pbi_output\Market Basket Analysis _ Cross Selling\MigratedReport.pbit"
)

# TMSL-correct enum mappings
_DTYPE_FIX = {
    "Int64":    "int64",
    "Decimal":  "decimal",
    "String":   "string",
    "Boolean":  "boolean",
    "DateTime": "dateTime",
}
_AGG_FIX = {
    "None":          "none",
    "Sum":           "sum",
    "Average":       "average",
    "Count":         "count",
    "DistinctCount": "distinctCount",
    "Min":           "min",
    "Max":           "max",
}


def _fix_schema(schema: dict) -> dict:
    for table in schema.get("model", {}).get("tables", []):
        # Fix columns
        for col in table.get("columns", []):
            if col.get("dataType") in _DTYPE_FIX:
                col["dataType"] = _DTYPE_FIX[col["dataType"]]
            if col.get("summarizeBy") in _AGG_FIX:
                col["summarizeBy"] = _AGG_FIX[col["summarizeBy"]]

        # Fix partitions
        for part in table.get("partitions", []):
            if part.get("mode") == "Import":
                part["mode"] = "import"
            # Fix #table({},{}) → typed empty table
            src = part.get("source", {})
            if src.get("expression") == "let Source = #table({}, {}) in Source":
                src["expression"] = "let Source = #table(type table [], {}) in Source"
    return schema


def _decode(raw: bytes) -> dict:
    for enc in ("utf-16", "utf-8"):
        try:
            return json.loads(raw.decode(enc))
        except Exception:
            pass
    raise ValueError("Cannot decode JSON")


def _utf16(obj: dict) -> bytes:
    return json.dumps(obj, indent=2, ensure_ascii=True).encode("utf-16")


def repair(pbit_path: Path) -> None:
    if not pbit_path.exists():
        raise FileNotFoundError(pbit_path)

    shutil.copy2(pbit_path, pbit_path.with_suffix(".pbit.bak2"))
    print(f"Backup: {pbit_path.with_suffix('.pbit.bak2')}")

    with zipfile.ZipFile(pbit_path) as zf:
        entries = {name: zf.read(name) for name in zf.namelist()}

    schema   = _decode(entries["DataModelSchema"])
    layout   = _decode(entries["Report/Layout"])
    settings = _decode(entries["Settings"])
    diagram  = _decode(entries["DiagramLayout"])

    print("\nFixing DataModelSchema enum casing...")
    schema = _fix_schema(schema)

    tmp = pbit_path.with_suffix(".pbit.tmp2")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", entries["[Content_Types].xml"])
        zf.writestr("Version",             entries["Version"])
        zf.writestr("SecurityBindings",    entries["SecurityBindings"])
        zf.writestr("DataModelSchema",     _utf16(schema))
        zf.writestr("DiagramLayout",       _utf16(diagram))
        zf.writestr("Report/Layout",       _utf16(layout))
        zf.writestr("Settings",            _utf16(settings))
        for name, data in entries.items():
            if name.startswith("Report/StaticResources/"):
                zf.writestr(name, data)

    pbit_path.unlink()
    tmp.rename(pbit_path)
    print(f"[DONE] Written: {pbit_path}")

    # Validate
    print("\nValidation:")
    with zipfile.ZipFile(pbit_path) as zf:
        raw = zf.read("DataModelSchema")
        print(f"  BOM: {raw[:2].hex()} (expect fffe)")
        s = json.loads(raw.decode("utf-16"))
        for table in s["model"]["tables"]:
            for col in table.get("columns", []):
                dt = col.get("dataType", "")
                sb = col.get("summarizeBy", "")
                if dt[0].isupper() or sb[0].isupper():
                    print(f"  [FAIL] table '{table['name']}' col '{col['name']}': dataType={dt} summarizeBy={sb}")
            for part in table.get("partitions", []):
                mode = part.get("mode", "")
                if mode[0].isupper():
                    print(f"  [FAIL] table '{table['name']}' partition mode={mode}")
                else:
                    print(f"  [OK] table '{table['name']}': mode={mode}, col count={len(table.get('columns',[]))}")


if __name__ == "__main__":
    repair(PBIT_PATH)
