"""
Generates TMDL (Tabular Model Definition Language) files for the Power BI semantic model.
Emits model.tmdl, tables/*.tmdl, and relationships.tmdl.
"""
from __future__ import annotations
import re
import os
from pathlib import Path
from uuid import uuid4
from typing import List

from accelerator.ir.schema import IRMigrationUnit, IRDataSource, IRColumn, TranslationResult

# Chars illegal in TMDL identifiers / Windows filenames.
# '#' is the TMDL escape character so it must be removed from table names.
# '$', '(', ')', spaces are also problematic in filenames and TMDL identifiers.
_INVALID_PATH_CHARS = re.compile(r'[\\/:*?"<>|\[\]#$() \x00-\x1f]')


def _safe_table_name(name: str) -> str:
    """Strip SQL brackets and illegal characters from a table name.

    The result is safe to use as both a Windows filename and a TMDL identifier.
    """
    clean = name.replace("].[", "_").replace("[", "").replace("]", "")
    clean = _INVALID_PATH_CHARS.sub("_", clean)
    clean = re.sub(r"_+", "_", clean).strip("_. ")
    return clean[:60] or "Table"


_DTYPE_TO_TMDL = {
    "int": "int64", "decimal": "decimal", "string": "string",
    "boolean": "boolean", "date": "dateTime", "datetime": "dateTime", "unknown": "string",
}

_AGG_TO_TMDL = {
    "sum": "sum", "avg": "average", "count": "count",
    "countd": "distinctCount", "min": "min", "max": "max", "median": "none",
}

_STRING_RESULT_PATTERN = re.compile(
    r"then\s*['\"]|else\s*['\"]|'[↑↓↔]'|\"[↑↓↔]\"|DATENAME|STR\s*\(|STRING\s*\(",
    re.IGNORECASE,
)

_AGG_FUNC_PATTERN = re.compile(
    r'\b(SUM|AVG|AVERAGE|COUNT|COUNTD|DISTINCTCOUNT|MIN|MAX|MEDIAN'
    r'|RUNNING_SUM|WINDOW_SUM|WINDOW_AVG|TOTAL|CALCULATE|SUMX|AVERAGEX|COUNTX)\s*\(',
    re.IGNORECASE,
)


def _is_row_context(col: IRColumn) -> bool:
    """True → emit as calculatedColumn (row-level); False → emit as measure."""
    if not col.is_calculated:
        return False
    if col.role == "measure" or col.aggregation:
        return False
    formula = col.formula or ""
    if _AGG_FUNC_PATTERN.search(formula):
        return False
    # AST-level check when available
    if col.formula_ast:
        from accelerator.parser.formula_parser import has_lod, has_table_calc
        if has_lod(col.formula_ast) or has_table_calc(col.formula_ast):
            return False
    return True


def _infer_format_string(col: IRColumn) -> str:
    formula = col.formula or ""
    if _STRING_RESULT_PATTERN.search(formula):
        return ""
    if col.datatype in ("date", "datetime"):
        return "Short Date"
    if col.aggregation in ("count", "countd"):
        return "#,0"
    return "#,0.00"


def _column_tmdl(col: IRColumn, table_name: str) -> str:
    dtype = _DTYPE_TO_TMDL.get(col.datatype, "string")
    display_name = col.business_name or col.name

    if col.is_calculated:
        if col.dax_expression:
            dax = _strip_measure_prefix(col.dax_expression, display_name)
            dax = _fix_tableau_dax(dax)
        else:
            dax = f"// TODO: translate from Tableau\n    // {col.formula}"

        if _is_row_context(col):
            # Row-level formula → Power BI calculated column
            lines = [
                f"    calculatedColumn '{display_name}'",
                f"        dataType: {dtype}",
                f"        expression: {dax}",
                f'        displayFolder: "Calculated Columns"',
            ]
        else:
            # Aggregated / LOD / table-calc → Power BI measure
            fmt = _infer_format_string(col)
            lines = [f"    measure '{display_name}' = {dax}"]
            if fmt:
                lines.append(f'        formatString: "{fmt}"')
            lines.append(f'        displayFolder: "Calculated Fields"')
        return "\n".join(lines)
    else:
        agg = _AGG_TO_TMDL.get(col.aggregation or "", "none") if col.role == "measure" else "none"
        lines = [
            f"    column '{col.name}'",
            f"        dataType: {dtype}",
            f"        summarizeBy: {agg}",
            f"        sourceColumn: {col.name}",
        ]
        if col.business_name and col.business_name != col.name:
            lines.append(f"        displayName: '{display_name}'")
        return "\n".join(lines)


def _extract_func_args(text: str, start: int) -> tuple[list[str], int]:
    """Extract comma-separated args of a function call starting at '(' position.

    Returns (args_list, end_index_after_closing_paren).
    Handles nested parentheses correctly.
    """
    assert text[start] == "("
    depth = 1
    buf = ""
    current: list[str] = []
    i = start + 1  # skip the opening '('
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
            buf += ch
        elif ch == ")":
            depth -= 1
            if depth == 0:
                current.append(buf)
                return current, i + 1
            buf += ch
        elif ch == "," and depth == 1:
            current.append(buf)
            buf = ""
        else:
            buf += ch
        i += 1
    return current, i


def _fix_tableau_dax(dax: str) -> str:
    """Convert residual Tableau-syntax fragments to valid DAX."""

    def _replace_func(text: str, func_name: str, replacer) -> str:
        """Find all occurrences of func_name( and replace using replacer(args) → str."""
        pattern = re.compile(re.escape(func_name) + r"\s*\(", re.IGNORECASE)
        result = []
        pos = 0
        for m in pattern.finditer(text):
            result.append(text[pos:m.start()])
            paren_start = m.end() - 1  # index of '('
            args, end = _extract_func_args(text, paren_start)
            result.append(replacer([a.strip() for a in args]))
            pos = end
        result.append(text[pos:])
        return "".join(result)

    # DATENAME('month', x) → FORMAT(x, "MMMM")
    fmt_map = {"month": "MMMM", "year": "YYYY", "day": "D",
               "quarter": "Q", "weekday": "dddd"}

    def _datename(args: list[str]) -> str:
        part = args[0].strip("'\"").lower() if args else ""
        expr = args[1] if len(args) > 1 else ""
        fmt = fmt_map.get(part, "MMMM")
        return f'FORMAT({expr}, "{fmt}")'

    dax = _replace_func(dax, "DATENAME", _datename)

    # DATEADD('month', n, x) → EDATE(x, n)  (must come before DATETRUNC)
    def _dateadd(args: list[str]) -> str:
        part = args[0].strip("'\"").lower() if args else ""
        n    = args[1] if len(args) > 1 else "0"
        expr = args[2] if len(args) > 2 else ""
        if part == "year":
            try:
                months = int(n) * 12
                return f"EDATE({expr}, {months})"
            except ValueError:
                return f"EDATE({expr}, ({n})*12)"
        if part == "day":
            return f"({expr} + {n})"
        return f"EDATE({expr}, {n})"

    dax = _replace_func(dax, "DATEADD", _dateadd)

    # DATETRUNC('month', x) → DATE(YEAR(x), MONTH(x), 1)
    def _datetrunc(args: list[str]) -> str:
        part = args[0].strip("'\"").lower() if args else ""
        expr = args[1] if len(args) > 1 else ""
        if part == "year":
            return f"DATE(YEAR({expr}), 1, 1)"
        if part == "quarter":
            return f"DATE(YEAR({expr}), (INT((MONTH({expr})-1)/3)*3)+1, 1)"
        return f"DATE(YEAR({expr}), MONTH({expr}), 1)"

    dax = _replace_func(dax, "DATETRUNC", _datetrunc)

    # Remove redundant DATE(single_date_expr) wrappers left by DATE(DATETRUNC(...))
    # In DAX, DATE() always takes 3 args; a single-arg call is a Tableau cast idiom.
    def _unwrap_single_date(text: str) -> str:
        pattern = re.compile(r'DATE\s*\(', re.IGNORECASE)
        result = []
        pos = 0
        for m in pattern.finditer(text):
            if m.start() < pos:  # already consumed by a larger match
                continue
            result.append(text[pos:m.start()])
            paren_start = m.end() - 1
            args, end = _extract_func_args(text, paren_start)
            if len(args) == 1:
                result.append(args[0].strip())
            else:
                result.append("DATE(" + ", ".join(a.strip() for a in args) + ")")
            pos = end
        result.append(text[pos:])
        return "".join(result)

    dax = _unwrap_single_date(dax)

    # Tableau CASE … WHEN … THEN … END → DAX SWITCH(SELECTEDVALUE(…), …)
    # Pattern: case [Table].[Column] when 'val' then expr … END
    case_block = re.compile(
        r'case\s+(\[[^\]]+\](?:\.\[[^\]]+\])?)\s*((?:when\s+.+?\s+then\s+.+?\s*)+)end',
        re.IGNORECASE | re.DOTALL,
    )
    def _case(m: re.Match) -> str:
        selector = m.group(1)
        # extract table.column for SELECTEDVALUE
        parts = selector.replace("[", "").replace("]", "").split(".")
        sv = f"SELECTEDVALUE({selector})" if len(parts) < 2 else f"SELECTEDVALUE({selector})"
        branches = re.findall(r"when\s+(.+?)\s+then\s+(.+?)(?=\s+when|\s*$)", m.group(2), re.IGNORECASE | re.DOTALL)
        args = ", ".join(f"{w.strip()}, {t.strip()}" for w, t in branches)
        return f"SWITCH({sv}, {args})"

    dax = case_block.sub(_case, dax)

    # Tableau logical operators: lowercase and/or → DAX &&/||
    # Only replace standalone keywords (not inside strings or identifiers)
    dax = re.sub(r'\band\b', '&&', dax)
    dax = re.sub(r'\bor\b',  '||', dax)

    # Tableau IF … THEN … ELSE … END → DAX IF(…, …, …)
    # Only handle simple single-branch pattern not already in DAX form
    tableau_if = re.compile(
        r'\bif\b\s+(.+?)\s+then\b\s+(.+?)(?:\s+else\b\s+(.+?))?\s+end\b',
        re.IGNORECASE | re.DOTALL,
    )
    def _if(m: re.Match) -> str:
        cond  = m.group(1).strip()
        then_ = m.group(2).strip()
        else_ = (m.group(3) or "BLANK()").strip()
        return f"IF({cond}, {then_}, {else_})"

    dax = tableau_if.sub(_if, dax)

    return dax


def _strip_measure_prefix(dax: str, measure_name: str) -> str:
    """Remove the 'Measure Name = ' prefix that schema_translator writes.

    schema_translator stores the full statement so the TMDL generator and
    pbit_generator both need to strip it before embedding the expression.
    """
    for prefix in (f"Measure '{measure_name}' = ", f'Measure "{measure_name}" = '):
        if dax.startswith(prefix):
            return dax[len(prefix):]
    return dax


def _indent_m_for_tmdl(m_query: str) -> str:
    """Indent M query lines so they nest correctly inside a TMDL partition source block.

    TMDL indentation for a source block inside a partition inside a table:
        table (col 0) → partition (4 sp) → source (8 sp) → M code (12 sp)
    """
    return "\n".join(
        "            " + line if line.strip() else ""
        for line in m_query.splitlines()
    )


def generate_table_tmdl(ds: IRDataSource) -> dict[str, str]:
    """Returns filename → TMDL content for one physical table and optionally a Measures table.

    One table is created per IRDataSource (using the first physical table name).
    All non-calculated columns from the data source go into that single table.
    A partition section with the inline M query is included — required by Power BI.
    """
    from accelerator.generators.m_generator import generate_query

    files: dict[str, str] = {}

    # ── Physical table (one per data source) ─────────────────────────────────
    raw_name = ds.tables[0].name if ds.tables else ds.name
    safe = _safe_table_name(raw_name)

    # Physical columns + row-context calculated columns (calculatedColumn) go in the table.
    # Aggregated calculated columns (measures) go in the separate _Measures table.
    physical_cols = [c for c in ds.columns if not c.is_calculated]
    calc_col_cols = [c for c in ds.columns if c.is_calculated and _is_row_context(c)]
    measure_cols  = [c for c in ds.columns if c.is_calculated and not _is_row_context(c)]

    seen_cols: set[str] = set()
    unique_cols = []
    for c in physical_cols + calc_col_cols:
        if c.name not in seen_cols:
            seen_cols.add(c.name)
            unique_cols.append(c)

    # M query for this data source (handles hyper, SQL, CSV, etc.)
    m_query = generate_query(ds)
    m_indented = _indent_m_for_tmdl(m_query)

    lines = [
        f"table '{safe}'",
        f"    annotation PBI_Id = \"{uuid4()}\"",
        "",
    ]
    for col in unique_cols:
        lines.append(_column_tmdl(col, safe))
        lines.append("")

    # Partition — required by Power BI to associate the table with a data source
    lines += [
        f"    partition '{safe}' = m",
        f"        mode: import",
        f"        source",
        m_indented,
        "",
    ]
    files[f"tables/{safe}.tmdl"] = "\n".join(lines)

    # ── Measures table (aggregated / LOD / table-calc fields only) ───────────────
    calc_cols = measure_cols
    if calc_cols:
        measures_m = 'let\n    Source = #table(\n        type table [_placeholder = Int64.Type],\n        {}\n    )\nin\n    Source'
        measures_m_indented = _indent_m_for_tmdl(measures_m)
        m_lines = [
            "table '_Measures'",
            f"    annotation PBI_Id = \"{uuid4()}\"",
            "",
        ]
        for col in calc_cols:
            m_lines.append(_column_tmdl(col, "_Measures"))
            m_lines.append("")
        m_lines += [
            "    partition 'Measures-Partition' = m",
            "        mode: import",
            "        source",
            measures_m_indented,
            "",
        ]
        files["tables/_Measures.tmdl"] = "\n".join(m_lines)

    return files


def generate_model_tmdl(unit: IRMigrationUnit) -> str:
    """Generates model.tmdl — one ref table per data source, consistent with generate_table_tmdl."""
    lines = [
        "model Model",
        "    culture: en-US",
        f"    annotation PBI_Id = \"{uuid4()}\"",
        "",
    ]

    seen: set[str] = set()
    for ds in unit.data_sources:
        raw_name = ds.tables[0].name if ds.tables else ds.name
        safe = _safe_table_name(raw_name)
        if safe not in seen:
            seen.add(safe)
            lines.append(f"    ref table '{safe}'")
        has_measures = any(c.is_calculated and not _is_row_context(c) for c in ds.columns)
        if has_measures and "_Measures" not in seen:
            seen.add("_Measures")
            lines.append("    ref table '_Measures'")

    if _needs_date_table(unit) and "Date" not in seen:
        lines.append("    ref table 'Date'")

    return "\n".join(lines)


def _resolve_join_columns(ds: "IRDataSource") -> dict:
    """
    Build a map: (left_table_id, right_table_id) → (left_col_name, right_col_name)
    from the IRJoin.conditions list so relationships can name actual columns.
    """
    result = {}
    for j in ds.joins:
        if j.conditions:
            cond = j.conditions[0]
            left_col  = cond.get("left",  "")
            right_col = cond.get("right", "")
            if left_col and right_col:
                result[(j.left_table_id, j.right_table_id)] = (left_col, right_col)
                result[(j.right_table_id, j.left_table_id)] = (right_col, left_col)
    return result


def generate_relationships_tmdl(unit: IRMigrationUnit) -> str:
    lines = [""]
    seen: set[str] = set()
    for ds in unit.data_sources:
        join_cols = _resolve_join_columns(ds)
        for rel in ds.relationships:
            from_tbl = next((t for t in ds.tables if t.id == rel.from_table), None)
            to_tbl   = next((t for t in ds.tables if t.id == rel.to_table), None)
            if not from_tbl or not to_tbl:
                continue
            from_safe = _safe_table_name(from_tbl.name)
            to_safe   = _safe_table_name(to_tbl.name)
            rel_name  = f"{from_safe}_{to_safe}"
            if rel_name in seen:
                continue
            seen.add(rel_name)

            # Try to resolve column names from join conditions
            resolved = join_cols.get((rel.from_table, rel.to_table))
            if resolved:
                from_col, to_col = resolved
            else:
                # Fall back to looking up column UUIDs in the IR
                from_col_obj = next((c for c in ds.columns if c.id == rel.from_column), None)
                to_col_obj   = next((c for c in ds.columns if c.id == rel.to_column),   None)
                from_col = from_col_obj.name if from_col_obj else "<resolve_column>"
                to_col   = to_col_obj.name   if to_col_obj   else "<resolve_column>"

            needs_review = "<resolve_column>" in (from_col, to_col)
            lines += [
                f"relationship {rel_name}",
                f"    fromTable: '{from_safe}'",
                f"    fromColumn: '{from_col}'" + ("   // TODO: confirm join key" if needs_review else ""),
                f"    toTable: '{to_safe}'",
                f"    toColumn: '{to_col}'" + ("   // TODO: confirm join key" if needs_review else ""),
                f"    crossFilteringBehavior: {rel.cross_filter.capitalize()}Direction",
                f"    isActive: {'true' if rel.is_active else 'false'}",
                "",
            ]
    return "\n".join(lines)


def _needs_date_table(unit: IRMigrationUnit) -> bool:
    """True when the analyzer found no date/calendar table in the workbook."""
    if not unit.analysis or not unit.analysis.proposed_star_schema:
        return False
    dims = unit.analysis.proposed_star_schema.get("dimensions", [])
    return not any(
        any(kw in d.lower() for kw in ("date", "calendar", "time", "period"))
        for d in dims
    )


def generate_date_table_tmdl() -> str:
    """Generate a standard Power BI Date/Calendar table in TMDL format."""
    m_query = (
        "let\n"
        "    StartDate  = #date(2015, 1, 1),\n"
        "    EndDate    = Date.From(DateTime.LocalNow()),\n"
        "    DateList   = List.Dates(StartDate,\n"
        "                   Number.From(EndDate - StartDate) + 1,\n"
        "                   #duration(1,0,0,0)),\n"
        "    DateTable  = Table.FromList(DateList, Splitter.SplitByNothing(),\n"
        "                   type table [Date = date]),\n"
        "    AddYear    = Table.AddColumn(DateTable,   \"Year\",    each Date.Year([Date]),          Int64.Type),\n"
        "    AddQtr     = Table.AddColumn(AddYear,     \"Quarter\", each \"Q\" & Text.From(Date.QuarterOfYear([Date])), type text),\n"
        "    AddMonth   = Table.AddColumn(AddQtr,      \"Month\",   each Date.MonthName([Date]),     type text),\n"
        "    AddMonthNo = Table.AddColumn(AddMonth,    \"MonthNo\", each Date.Month([Date]),          Int64.Type),\n"
        "    AddWeek    = Table.AddColumn(AddMonthNo,  \"Week\",    each Date.WeekOfYear([Date]),     Int64.Type),\n"
        "    AddDay     = Table.AddColumn(AddWeek,     \"Day\",     each Date.Day([Date]),            Int64.Type),\n"
        "    AddWkday   = Table.AddColumn(AddDay,      \"Weekday\", each Date.DayOfWeekName([Date]),  type text)\n"
        "in\n"
        "    AddWkday"
    )
    m_indented = "\n".join("            " + ln for ln in m_query.splitlines())

    return "\n".join([
        f"table 'Date'",
        f"    annotation PBI_Id = \"{uuid4()}\"",
        f"    annotation PBI_IsDateTable = \"True\"",
        "",
        "    column 'Date'",
        "        dataType: dateTime",
        "        isKey: true",
        "        summarizeBy: none",
        "        sourceColumn: Date",
        "        formatString: \"Short Date\"",
        "",
        "    column 'Year'",
        "        dataType: int64",
        "        summarizeBy: none",
        "        sourceColumn: Year",
        "",
        "    column 'Quarter'",
        "        dataType: string",
        "        summarizeBy: none",
        "        sourceColumn: Quarter",
        "",
        "    column 'Month'",
        "        dataType: string",
        "        summarizeBy: none",
        "        sourceColumn: Month",
        "        sortByColumn: 'MonthNo'",
        "",
        "    column 'MonthNo'",
        "        dataType: int64",
        "        isHidden: true",
        "        summarizeBy: none",
        "        sourceColumn: MonthNo",
        "",
        "    column 'Week'",
        "        dataType: int64",
        "        summarizeBy: none",
        "        sourceColumn: Week",
        "",
        "    column 'Day'",
        "        dataType: int64",
        "        summarizeBy: none",
        "        sourceColumn: Day",
        "",
        "    column 'Weekday'",
        "        dataType: string",
        "        summarizeBy: none",
        "        sourceColumn: Weekday",
        "",
        "    partition 'Date' = m",
        "        mode: import",
        "        source",
        m_indented,
        "",
    ])


def generate_all(unit: IRMigrationUnit, output_dir: Path) -> List[TranslationResult]:
    results = []
    model_dir  = output_dir / "SemanticModel"
    tables_dir = model_dir / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)

    model_tmdl = generate_model_tmdl(unit)
    (model_dir / "model.tmdl").write_text(model_tmdl, encoding="utf-8")
    results.append(TranslationResult(
        source_id=unit.id, target_artifact=model_tmdl,
        target_kind="model", confidence=0.95, method="deterministic",
    ))

    seen_table_files: set[str] = set()
    for ds in unit.data_sources:
        for fname, content in generate_table_tmdl(ds).items():
            if fname in seen_table_files:
                continue
            seen_table_files.add(fname)
            dest = model_dir / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

    rel_tmdl = generate_relationships_tmdl(unit)
    if rel_tmdl.strip():
        (model_dir / "relationships.tmdl").write_text(rel_tmdl, encoding="utf-8")

    # Auto-generate a Date table when the workbook has no date dimension
    if _needs_date_table(unit):
        date_tmdl = generate_date_table_tmdl()
        date_path = tables_dir / "Date.tmdl"
        date_path.write_text(date_tmdl, encoding="utf-8")
        results.append(TranslationResult(
            source_id=unit.id,
            target_artifact=date_tmdl,
            target_kind="model",
            confidence=0.90,
            method="deterministic",
            caveats=[
                "Auto-generated Date table (2015–today). Adjust StartDate in Power Query M as needed.",
                "Mark as Date Table: 'Table Tools → Mark as date table → Date' in Power BI Desktop.",
            ],
            needs_review=True,
            review_priority="medium",
            rationale="No date dimension detected in workbook — standard calendar table generated.",
        ))

    return results
