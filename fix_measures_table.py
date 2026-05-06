"""
fix_measures_table.py
---------------------
"Measures" is a reserved TMSL keyword (it is the measures-collection
property on every Table object).  Using it as a table NAME causes:

    "Unsupported Table name 'Measures' has been found in data model schema."

Two bugs are fixed together:
  1. Reserved name  → rename to "_Measures"
  2. Empty columns  → add hidden _placeholder column (BIM requires ≥1 col)

Fixes every affected file under pbi_output/ and output_*/ in one pass,
then patches the generator source files so future runs are clean.
"""
import json, re
from pathlib import Path

ROOT = Path(__file__).parent
OLD_NAME = "Measures"
NEW_NAME = "_Measures"

PLACEHOLDER_COL = {
    "name": "_placeholder",
    "dataType": "int64",
    "isHidden": True,
    "summarizeBy": "none",
    "sourceColumn": "_placeholder",
}

MEASURES_M = (
    "let\n"
    "    Source = #table(\n"
    "        type table [_placeholder = Int64.Type],\n"
    "        {}\n"
    "    )\n"
    "in\n"
    "    Source"
)

# ── model.bim ─────────────────────────────────────────────────────────────────

def fix_bim(path: Path) -> bool:
    data = json.loads(path.read_text("utf-8"))
    changed = False
    for table in data.get("model", {}).get("tables", []):
        if table.get("name") == OLD_NAME:
            table["name"] = NEW_NAME
            # Ensure _placeholder column exists
            cols = table.get("columns", [])
            if not any(c.get("name") == "_placeholder" for c in cols):
                cols.insert(0, PLACEHOLDER_COL)
                table["columns"] = cols
            # Fix partition name and M expression
            for part in table.get("partitions", []):
                if part.get("name") == OLD_NAME:
                    part["name"] = NEW_NAME
                src = part.get("source", {})
                if "#table({}" in src.get("expression", "") or \
                   "#table(type table [], {})" in src.get("expression", ""):
                    src["expression"] = MEASURES_M
            changed = True
    if changed:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  FIXED model.bim: {path}")
    return changed


# ── model.tmdl ────────────────────────────────────────────────────────────────

def fix_model_tmdl(path: Path) -> bool:
    text = path.read_text("utf-8")
    new_text = re.sub(
        r"(ref table ')Measures(')",
        rf"\g<1>{NEW_NAME}\g<2>",
        text,
    )
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        print(f"  FIXED model.tmdl: {path}")
        return True
    return False


# ── Measures.tmdl → _Measures.tmdl ────────────────────────────────────────────

def fix_measures_tmdl_file(path: Path) -> bool:
    text = path.read_text("utf-8")
    # Rename the table declaration
    new_text = re.sub(r"^table 'Measures'", f"table '{NEW_NAME}'", text, flags=re.MULTILINE)

    # Add _placeholder column if missing
    if "_placeholder" not in new_text:
        placeholder_block = (
            f"\n"
            f"    column '_placeholder'\n"
            f"        dataType: int64\n"
            f"        isHidden\n"
            f"        summarizeBy: none\n"
            f"        sourceColumn: _placeholder\n"
        )
        # Insert before the first measure or partition
        insert_re = re.compile(r"(\n    (?:measure|partition) )")
        m = insert_re.search(new_text)
        if m:
            new_text = new_text[:m.start()] + placeholder_block + new_text[m.start():]
        else:
            new_text += placeholder_block

    # Fix the M query in the partition to use typed table with _placeholder
    new_text = re.sub(
        r"Source = #table\(\{\},\s*\{\}\)",
        "Source = #table(\n"
        "                type table [_placeholder = Int64.Type],\n"
        "                {})",
        new_text,
    )
    new_text = re.sub(
        r"Source = #table\(type table \[\],\s*\{\}\)",
        "Source = #table(\n"
        "                type table [_placeholder = Int64.Type],\n"
        "                {})",
        new_text,
    )

    # Rename partition if it's named "Measures-Partition" to keep consistent
    new_text = new_text.replace(
        "partition 'Measures-Partition'",
        f"partition '{NEW_NAME}-Partition'",
    )

    changed = new_text != text
    if changed:
        path.write_text(new_text, encoding="utf-8")

    # Rename the file Measures.tmdl → _Measures.tmdl
    new_path = path.parent / f"{NEW_NAME}.tmdl"
    if path != new_path:
        path.rename(new_path)
        print(f"  FIXED + RENAMED tmdl: {path.name} -> {new_path.name}")
    elif changed:
        print(f"  FIXED tmdl: {path}")
    return changed or path != new_path


# ── sweep pbi_output/ and output_*/ ──────────────────────────────────────────

def sweep(root: Path) -> None:
    fixed = 0
    for bim in root.rglob("model.bim"):
        if fix_bim(bim):
            fixed += 1
    for tmdl in root.rglob("model.tmdl"):
        fix_model_tmdl(tmdl)
    for tmdl in list(root.rglob("Measures.tmdl")):
        fix_measures_tmdl_file(tmdl)
        fixed += 1
    print(f"  >> {fixed} files updated under {root.relative_to(ROOT)}")


# ── patch generator source files ─────────────────────────────────────────────

def patch_file(path: Path, replacements: list[tuple[str, str]]) -> None:
    text = path.read_text("utf-8")
    new_text = text
    for old, new in replacements:
        new_text = new_text.replace(old, new)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        print(f"  PATCHED source: {path.relative_to(ROOT)}")


if __name__ == "__main__":
    print("=== Fixing pbi_output/ ===")
    sweep(ROOT / "pbi_output")

    for d in ROOT.glob("output_*"):
        if d.is_dir():
            print(f"\n=== Fixing {d.name}/ ===")
            sweep(d)

    print("\n=== Patching generator sources ===")

    # pbit_generator.py
    patch_file(
        ROOT / "accelerator" / "generators" / "pbit_generator.py",
        [
            ('"Measures"', '"_Measures"'),
            ("'Measures'", "'_Measures'"),
            ('"Measures-Partition"', '"_Measures-Partition"'),
            ('"name": "_Measures-Partition"', '"name": "_Measures-Partition"'),  # idempotent
            (
                '"expression": "let Source = #table(type table [], {}) in Source"',
                '"expression": ' + json.dumps(MEASURES_M),
            ),
        ],
    )

    # tmdl_generator.py
    patch_file(
        ROOT / "accelerator" / "generators" / "tmdl_generator.py",
        [
            ('"table \'Measures\'"', f'"table \'{NEW_NAME}\'"'),
            ("\"table 'Measures'\"", f"\"table '{NEW_NAME}'\""),
            ("'table Measures'", f"'table {NEW_NAME}'"),
            ('"Measures"', f'"{NEW_NAME}"'),
            ("'Measures'", f"'{NEW_NAME}'"),
            ('"Measures-Partition"', f'"{NEW_NAME}-Partition"'),
            (
                'measures_m = "let\\n    Source = #table(type table [], {})\\nin\\n    Source"',
                'measures_m = ' + repr(MEASURES_M),
            ),
        ],
    )

    print("\nDone. Run: python -m pytest tests/ -q")
