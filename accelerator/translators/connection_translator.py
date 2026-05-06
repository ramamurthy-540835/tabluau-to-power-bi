"""Translates IRConnection objects into Power Query M Source steps."""
from __future__ import annotations
import re
from pathlib import Path
import yaml

from accelerator.ir.schema import IRConnection, TranslationResult

_CONFIG = Path(__file__).parent.parent.parent / "config" / "connection_map.yaml"
_PLACEHOLDER_RE = re.compile(r'<[^>]+>')

_EMPTY_TABLE_M = "let\n    Source = #table(type table [], {{}})\nin\n    Source"


def _load_map() -> dict:
    if _CONFIG.exists():
        return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    return {}


def _fallback_m(conn: IRConnection, reason: str, original_m: str = "") -> str:
    """Return a valid M query (typed empty table) with a blocker comment."""
    lines = [
        f"// [BLOCKER] {reason}",
        f"// Class: {conn.connection_class}  "
        f"Server: {conn.server or '(none)'}  "
        f"Database: {conn.database or '(none)'}",
    ]
    if original_m:
        lines.append("// When resolved, replace Source with:")
        for ln in original_m.splitlines():
            lines.append(f"// {ln}")
    lines += ["let", "    Source = #table(type table [], {})", "in", "    Source"]
    return "\n".join(lines)


def translate_connection(conn: IRConnection) -> TranslationResult:
    conn_map = _load_map()
    entry = conn_map.get(conn.connection_class.lower())

    if entry is None:
        full_m = _fallback_m(
            conn,
            reason=f'No Power BI connector mapping for Tableau class "{conn.connection_class}"',
        )
        return TranslationResult(
            source_id=conn.id,
            target_artifact=full_m,
            target_kind="query",
            confidence=0.0,
            method="deterministic",
            caveats=[f'No connector mapping for class "{conn.connection_class}" — replace Source with the correct Power Query M.'],
            needs_review=True,
            review_priority="blocker",
        )

    template = entry.get("m_template", "")
    server   = conn.server   or ""
    database = conn.database or ""
    schema   = conn.schema_name or "dbo"
    port     = str(conn.port) if conn.port else "5432"

    m_code = (template
              .replace("{server}",   server)
              .replace("{database}", database)
              .replace("{schema}",   schema)
              .replace("{port}",     port)
              .replace("{project}",  database)
              .replace("{dataset}",  schema)
              .replace("{catalog}",  database)
              .replace("{site_url}", server)
              .replace("{url}",      server)
              .replace("{file_path}", server))

    # If required connection details were absent, placeholders remain — produce a
    # fallback typed empty table so Power BI Desktop can open the file, and mark
    # as a blocker so the user knows to fill it in.
    remaining = _PLACEHOLDER_RE.findall(m_code)
    if remaining:
        unique = sorted(set(remaining))
        intended_m = f"let\n    Source = {m_code}\nin\n    Source"
        full_m = _fallback_m(
            conn,
            reason=f"Connection details missing — fill in: {', '.join(unique)}",
            original_m=intended_m,
        )
        return TranslationResult(
            source_id=conn.id,
            target_artifact=full_m,
            target_kind="query",
            confidence=0.0,
            method="deterministic",
            caveats=[
                f"Connection details missing ({', '.join(unique)}). "
                "The server/file path was not found in the Tableau workbook. "
                "Open the query in Power BI Desktop and replace Source with the real connection."
            ],
            needs_review=True,
            review_priority="blocker",
        )

    full_m = f"let\n    Source = {m_code}\nin\n    Source"

    caveats = []
    if conn.username:
        caveats.append(
            f"Credentials stripped. Re-enter username '{conn.username}' "
            "in Power BI Service data gateway settings."
        )

    return TranslationResult(
        source_id=conn.id,
        target_artifact=full_m,
        target_kind="query",
        confidence=0.9,
        method="deterministic",
        caveats=caveats,
        needs_review=bool(caveats),
        review_priority="medium" if caveats else "low",
    )
