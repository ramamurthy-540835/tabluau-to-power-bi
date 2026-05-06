"""
Generates Power Query M scripts for each data source connection.
"""
from __future__ import annotations
from pathlib import Path
from typing import List

from accelerator.ir.schema import IRMigrationUnit, IRDataSource, TranslationResult
from accelerator.translators.connection_translator import translate_connection


_DTYPE_TO_M = {
    "int": "Int64.Type",
    "decimal": "type number",
    "string": "type text",
    "boolean": "type logical",
    "date": "type date",
    "datetime": "type datetime",
    "unknown": "type text",
}

# Types for use inside #table(type table [...]) schema declarations
_M_SCHEMA_DTYPE = {
    "int":      "(type nullable number)",
    "decimal":  "(type nullable number)",
    "string":   "(type nullable text)",
    "boolean":  "(type nullable logical)",
    "date":     "(type nullable date)",
    "datetime": "(type nullable datetime)",
    "unknown":  "(type nullable text)",
}


def _is_hyper(ds: IRDataSource) -> bool:
    """True when the primary connection is a Tableau Hyper/extract file."""
    return bool(ds.connections) and ds.connections[0].connection_class.lower() in (
        "hyper", "extract", "tableau-extract"
    )


def _build_hyper_query(ds: IRDataSource) -> str:
    """
    Build a valid Power Query M query for a Hyper extract data source.

    There is no Power BI connector for .hyper files, so we generate an empty
    typed table using #table() so that:
      1. The M is syntactically valid — Power BI Desktop opens without errors.
      2. The column names and types are declared — the semantic model schema is correct.
      3. A clear comment tells the user how to swap in a real connection.
    """
    cols = [c for c in ds.columns if not c.is_calculated]

    schema_parts = [
        f'            #"{c.name}" = {_M_SCHEMA_DTYPE.get(c.datatype, "(type nullable text)")}'
        for c in cols[:100]
    ]

    header = (
        f"// Power Query M for: {ds.name}\n"
        "// NOTE: Tableau Hyper extract — no direct Power BI connector available.\n"
        "// Replace Source below with your real connection, for example:\n"
        '//   CSV export:  Csv.Document(File.Contents("C:\\path\\to\\export.csv"),[Delimiter=",",Encoding=65001])\n'
        "//   SQL Server:  Sql.Database(\"server\", \"database\")\n"
    )

    if schema_parts:
        schema = ",\n".join(schema_parts)
        m_body = (
            "let\n"
            "    Source = #table(\n"
            "        type table [\n"
            f"{schema}\n"
            "        ],\n"
            "        {}\n"
            "    )\n"
            "in\n"
            "    Source"
        )
    else:
        m_body = "let\n    Source = #table(type table [], {})\nin\n    Source"

    return header + m_body


def _build_change_type_step(ds: IRDataSource) -> str:
    if not ds.columns:
        return ""
    type_pairs = []
    for col in ds.columns[:50]:  # limit for readability
        m_type = _DTYPE_TO_M.get(col.datatype, "type text")
        type_pairs.append(f"        {{\"{col.name}\", {m_type}}}")
    return (
        "    ChangedType = Table.TransformColumnTypes(Source, {\n"
        + ",\n".join(type_pairs)
        + "\n    }),"
    )


def _build_no_connection_query(ds: IRDataSource) -> str:
    """Valid typed empty table when no connection info exists in the workbook."""
    cols = [c for c in ds.columns if not c.is_calculated]
    schema_parts = [
        f'            #"{c.name}" = {_M_SCHEMA_DTYPE.get(c.datatype, "(type nullable text)")}'
        for c in cols[:100]
    ]
    header = (
        f"// Power Query M for data source: {ds.name}\n"
        "// [BLOCKER] No connection found in Tableau workbook.\n"
        "// Replace Source below with your real data connection.\n"
    )
    if schema_parts:
        schema = ",\n".join(schema_parts)
        return (
            header
            + "let\n"
            + "    Source = #table(\n"
            + "        type table [\n"
            + f"{schema}\n"
            + "        ],\n"
            + "        {}\n"
            + "    )\n"
            + "in\n"
            + "    Source"
        )
    return header + "let\n    Source = #table(type table [], {})\nin\n    Source"


def generate_query(ds: IRDataSource) -> str:
    """Generates a full M query for a data source."""
    # Hyper extracts have no Power BI connector — generate a valid typed empty table
    if _is_hyper(ds):
        return _build_hyper_query(ds)

    # No connections at all — valid typed empty table with blocker comment
    if not ds.connections:
        return _build_no_connection_query(ds)

    conn = ds.connections[0]
    result = translate_connection(conn)
    caveats = list(result.caveats)

    # When the connection translator couldn't resolve the connection (confidence=0),
    # or the result contains an empty File.Contents("") path (which crashes PBI on
    # refresh), fall back to a valid typed empty #table() so the semantic model
    # loads cleanly — the user replaces the source manually.
    artifact = result.target_artifact
    if result.confidence == 0.0 or 'File.Contents("")' in artifact:
        return _build_no_connection_query(ds)

    m_snippet = result.target_artifact
    source_line = (m_snippet
                   .replace("let\n    Source = ", "    Source = ")
                   .replace("\nin\n    Source", ","))

    steps = ["let", source_line]
    change_type = _build_change_type_step(ds)
    if change_type:
        # Strip trailing comma — last step before `in` must not end with `,`
        steps.append(change_type.rstrip().rstrip(","))
        steps.append("in\n    ChangedType")
    else:
        steps[-1] = steps[-1].rstrip(",")
        steps.append("in\n    Source")

    header = f"// Power Query M for data source: {ds.name}\n"
    if caveats:
        header += "\n".join(f"// NOTE: {c}" for c in caveats) + "\n"
    return header + "\n".join(steps)


def generate_all(unit: IRMigrationUnit, output_dir: Path) -> List[TranslationResult]:
    results = []
    queries_dir = output_dir / "SemanticModel" / "queries"
    queries_dir.mkdir(parents=True, exist_ok=True)

    for ds in unit.data_sources:
        m_script = generate_query(ds)
        fname = f"{ds.name.replace(' ', '_')}.m"
        (queries_dir / fname).write_text(m_script, encoding="utf-8")
        results.append(TranslationResult(
            source_id=ds.id,
            target_artifact=m_script,
            target_kind="query",
            confidence=0.85,
            method="deterministic",
            caveats=[f"Review credentials and connection parameters for {ds.name}."],
            needs_review=True,
            review_priority="medium",
        ))

    return results
