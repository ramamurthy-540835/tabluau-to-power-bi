"""
LLM-assisted DAX translator for complex Tableau calculated fields.
Uses the Anthropic API with prompt caching and retry logic.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional
import yaml

from accelerator.ir.schema import IRColumn, TranslationResult
from accelerator.parser.formula_parser import ast_depth, has_lod, has_table_calc

_CONFIG = Path(__file__).parent.parent.parent.parent / "config" / "llm_config.yaml"


def _load_llm_config() -> dict:
    if _CONFIG.exists():
        return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    return {"model": "claude-sonnet-4-6", "temperature": 0.1, "max_tokens": 2048, "max_retries": 2}


SYSTEM_PROMPT = """\
You are an expert in both Tableau calculated fields and Microsoft DAX (Data Analysis Expressions).
Your job is to translate Tableau formulas into DAX measures or calculated columns for Power BI.

Rules:
1. Default to DAX MEASURES unless the formula requires row-level context (then use calculated column).
2. For LOD expressions: FIXED → CALCULATE with ALLEXCEPT, INCLUDE → CALCULATE with VALUES, EXCLUDE → CALCULATE with ALL.
3. For table calcs: RUNNING_SUM → CALCULATE with cumulative date filter, INDEX → RANKX.
4. Preserve semantic intent, not just syntax.
5. If the translation is ambiguous, explain the most likely intent and caveat the rest.

Output ONLY valid JSON in this exact structure:
{
  "dax_expression": "...",
  "measure_or_column": "measure",
  "confidence": 0.9,
  "rationale": "...",
  "caveats": ["..."]
}
"""

FEW_SHOT_EXAMPLES = """\
Example 1 — FIXED LOD:
Tableau: { FIXED [Region] : SUM([Sales]) }
DAX: CALCULATE(SUM(Orders[Sales]), ALLEXCEPT(Orders, Orders[Region]))

Example 2 — INCLUDE LOD:
Tableau: { INCLUDE [Customer ID] : AVG([Order Amount]) }
DAX: CALCULATE(AVERAGEX(VALUES(Orders[Customer ID]), CALCULATE(SUM(Orders[Order Amount]))), VALUES(Orders[Customer ID]))

Example 3 — RUNNING SUM:
Tableau: RUNNING_SUM(SUM([Sales]))
DAX: CALCULATE(SUM(Orders[Sales]), FILTER(ALLSELECTED(Date[Date]), Date[Date] <= MAX(Date[Date])))

Example 4 — IF with NULL:
Tableau: IF ISNULL([Discount]) THEN 0 ELSE [Discount] END
DAX: IF(ISBLANK(Orders[Discount]), 0, Orders[Discount])

Example 5 — COUNTD:
Tableau: COUNTD([Customer ID])
DAX: DISTINCTCOUNT(Orders[Customer ID])

Example 6 — ZN:
Tableau: ZN([Sales])
DAX: IF(ISBLANK(SUM(Orders[Sales])), 0, SUM(Orders[Sales]))
"""


_TABLE_CALC_PATTERN = re.compile(
    r'\b(WINDOW_MAX|WINDOW_MIN|WINDOW_SUM|WINDOW_AVG|WINDOW_COUNT'
    r'|RUNNING_SUM|RUNNING_AVG|RUNNING_MAX|RUNNING_MIN|RUNNING_COUNT'
    r'|INDEX|RANK|FIRST|LAST|LOOKUP|TOTAL|SIZE|PREVIOUS_VALUE)\s*\(',
    re.IGNORECASE,
)
_LOD_PATTERN = re.compile(r'\{\s*(FIXED|INCLUDE|EXCLUDE)\b', re.IGNORECASE)
# Curly-brace expressions that are NOT LOD (e.g. {COUNTD([col])}) are Tableau
# inline aggregations that have no direct DAX equivalent — route to LLM.
_TABLEAU_CURLY_PATTERN = re.compile(r'\{[^}]+\}')

# Maps Tableau aggregation names to correct DAX function names.
# COUNTD → DISTINCTCOUNT; AVG → AVERAGE; others match case-insensitively.
_TABLEAU_AGG_TO_DAX: dict[str, str] = {
    "sum":    "SUM",
    "avg":    "AVERAGE",
    "count":  "COUNT",
    "countd": "DISTINCTCOUNT",
    "min":    "MIN",
    "max":    "MAX",
    "median": "MEDIAN",
}


def _should_use_llm(col: IRColumn) -> bool:
    formula = col.formula or ""

    # Fast string-level checks — catches formulas that failed AST parsing
    if _LOD_PATTERN.search(formula):
        return True
    if _TABLE_CALC_PATTERN.search(formula):
        return True
    # Tableau curly-brace aggregations like {COUNTD([col])} are not valid DAX
    if _TABLEAU_CURLY_PATTERN.search(formula):
        return True

    # AST-level checks for successfully parsed formulas
    if col.formula_ast is None:
        return False
    if col.formula_ast.node_type == "_UNPARSED" and len(formula) > 60:
        return True   # complex enough that parser gave up — send to LLM
    if has_lod(col.formula_ast):
        return True
    if has_table_calc(col.formula_ast):
        return True
    if ast_depth(col.formula_ast) > 6:
        return True
    if len(col.dependencies) > 3:
        return True
    return False


def _cache_key(formula: str, context: str) -> str:
    return hashlib.sha256(f"{formula}||{context}".encode()).hexdigest()


_translation_cache: dict[str, dict] = {}


# Matches simple aggregations with or without Tableau's curly-brace wrapper:
#   {COUNTD([Customer ID])}  →  DISTINCTCOUNT('t'[Customer ID])
#   SUM([Sales])             →  SUM('t'[Sales])
_SIMPLE_AGG_RE = re.compile(
    r'^\s*\{?\s*(SUM|AVG|COUNT|COUNTD|MIN|MAX|MEDIAN)\s*\(\s*\[([^\]]+)\]\s*\)\s*\}?\s*$',
    re.IGNORECASE,
)


def _try_deterministic_agg(formula: str, table_name: str, measure_name: str) -> str | None:
    """Return a DAX measure string for simple aggregation patterns, or None."""
    m = _SIMPLE_AGG_RE.match(formula)
    if not m:
        return None
    dax_agg = _TABLEAU_AGG_TO_DAX.get(m.group(1).lower(), m.group(1).upper())
    field    = m.group(2)
    return f"Measure '{measure_name}' = {dax_agg}('{table_name}'[{field}])"


def translate_calculated_field(col: IRColumn, table_name: str = "Table") -> TranslationResult:
    """Translate a Tableau calculated field to DAX and store result on col.dax_expression."""
    formula      = col.formula or ""
    measure_name = col.business_name or col.name

    if not formula.strip():
        return TranslationResult(
            source_id=col.id,
            target_artifact=f"// Empty formula for '{col.name}'",
            target_kind="measure",
            confidence=1.0,
            method="deterministic",
        )

    # Special case: Tableau's "Number of Records" (formula = "1") is always
    # a row-count field — the correct DAX equivalent is COUNTROWS.
    if formula.strip() == "1":
        dax = f"Measure '{measure_name}' = COUNTROWS('{table_name}')"
        col.dax_expression = dax
        return TranslationResult(
            source_id=col.id,
            target_artifact=dax,
            target_kind="measure",
            confidence=0.95,
            method="deterministic",
        )

    # Fast deterministic path for simple aggregations (with or without curly braces).
    # This handles patterns like {COUNTD([Customer ID])} without needing the LLM,
    # ensuring they translate correctly even when ANTHROPIC_API_KEY is not set.
    simple = _try_deterministic_agg(formula, table_name, measure_name)
    if simple:
        col.dax_expression = simple
        return TranslationResult(
            source_id=col.id,
            target_artifact=simple,
            target_kind="measure",
            confidence=0.95,
            method="deterministic",
        )

    if not _should_use_llm(col):
        result = _deterministic_translate(col, table_name)
    else:
        result = _llm_translate(col, table_name)

    # Store the translated DAX back on the column for TMDL generator to use
    col.dax_expression = result.target_artifact
    return result


def _deterministic_translate(col: IRColumn, table_name: str) -> TranslationResult:
    """Fast path for simple formulas using function_dictionary.yaml."""
    cfg_path = Path(__file__).parent.parent.parent.parent / "config" / "function_dictionary.yaml"
    func_dict = {}
    if cfg_path.exists():
        func_dict = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    formula = col.formula or ""
    dax = formula
    confidence = 0.7
    caveats: list[str] = []

    # Simple pattern substitutions from function dictionary
    import re
    for func_name, entry in func_dict.items():
        if entry.get("strategy") == "direct" and entry.get("dax"):
            pattern = re.compile(r'\b' + re.escape(func_name) + r'\b', re.IGNORECASE)
            if pattern.search(dax):
                dax_template = entry["dax"]
                args_match = re.search(r'\b' + re.escape(func_name) + r'\s*\(([^)]*)\)', dax, re.IGNORECASE)
                if args_match:
                    args = [a.strip() for a in args_match.group(1).split(",")]
                    repl = dax_template
                    for i, arg in enumerate(args):
                        arg_clean = arg.strip("[]")
                        repl = repl.replace(f"{{arg{i+1}}}", f"{table_name}[{arg_clean}]")
                    dax = pattern.sub("", dax, count=1)
                    confidence = 0.8

    measure_name = col.business_name or col.name
    dax_measure = f"Measure '{measure_name}' = {formula}"
    if col.aggregation:
        dax_agg = _TABLEAU_AGG_TO_DAX.get(col.aggregation.lower(), col.aggregation.upper())
        dax_measure = f"Measure '{measure_name}' = {dax_agg}({table_name}[{col.name}])"
        confidence = 0.95

    return TranslationResult(
        source_id=col.id,
        target_artifact=dax_measure,
        target_kind="measure",
        confidence=confidence,
        method="deterministic",
        caveats=caveats,
        needs_review=confidence < 0.85,
        review_priority="low" if confidence >= 0.85 else "medium",
    )


def _llm_translate(col: IRColumn, table_name: str) -> TranslationResult:
    """Call Claude API for complex formula translation."""
    formula = col.formula or ""
    cache_key = _cache_key(formula, table_name)

    if cache_key in _translation_cache:
        cached = _translation_cache[cache_key]
        return TranslationResult(
            source_id=col.id,
            target_artifact=cached.get("dax_expression", formula),
            target_kind="measure" if cached.get("measure_or_column") == "measure" else "column",
            confidence=cached.get("confidence", 0.8),
            method="llm",
            rationale=cached.get("rationale"),
            caveats=cached.get("caveats", []) + ["[cached result]"],
            needs_review=True,
            review_priority="medium",
        )

    try:
        import anthropic
    except ImportError:
        return TranslationResult(
            source_id=col.id,
            target_artifact=f"// TODO: Translate manually\n// Tableau: {formula}",
            target_kind="measure",
            confidence=0.0,
            method="manual",
            caveats=["anthropic SDK not installed. Run: pip install anthropic"],
            needs_review=True,
            review_priority="blocker",
        )

    llm_cfg = _load_llm_config()
    # lstrip('﻿') removes the UTF-8 BOM that Secret Manager injects when
    # the secret was stored with a BOM prefix (common on Windows tooling).
    # strip() removes trailing newlines that gcloud CLI sometimes appends.
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").lstrip('﻿').strip()
    if not api_key:
        return TranslationResult(
            source_id=col.id,
            target_artifact=f"// TODO: Translate manually\n// Tableau: {formula}",
            target_kind="measure",
            confidence=0.0,
            method="manual",
            caveats=["ANTHROPIC_API_KEY environment variable not set."],
            needs_review=True,
            review_priority="blocker",
        )

    client = anthropic.Anthropic(api_key=api_key)
    user_content = (
        f"{FEW_SHOT_EXAMPLES}\n\n"
        f"Now translate this Tableau calculated field to DAX:\n"
        f"Field name: {col.name}\n"
        f"Data type: {col.datatype}\n"
        f"Role: {col.role}\n"
        f"Table: {table_name}\n"
        f"Formula: {formula}\n"
    )

    max_retries = llm_cfg.get("max_retries", 2)
    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            messages = [{"role": "user", "content": user_content}]
            if attempt > 0 and last_error:
                messages.append({"role": "assistant", "content": "I'll fix the DAX syntax."})
                messages.append({"role": "user", "content": f"The previous attempt had this error: {last_error}\nPlease fix and return corrected JSON."})

            response = client.messages.create(
                model=llm_cfg.get("model", "claude-sonnet-4-6"),
                max_tokens=llm_cfg.get("max_tokens", 2048),
                temperature=llm_cfg.get("temperature", 0.1),
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=messages,
            )
            content = response.content[0].text.strip()

            # Extract JSON from response
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            parsed = json.loads(content)
            _translation_cache[cache_key] = parsed

            return TranslationResult(
                source_id=col.id,
                target_artifact=parsed.get("dax_expression", formula),
                target_kind="measure" if parsed.get("measure_or_column", "measure") == "measure" else "column",
                confidence=float(parsed.get("confidence", 0.8)),
                method="llm",
                rationale=parsed.get("rationale"),
                caveats=parsed.get("caveats", []),
                validation_status="not_validated",
                needs_review=True,
                review_priority="medium",
            )
        except json.JSONDecodeError as e:
            last_error = f"JSON parse error: {e}"
            print(f"[dax_translator] JSON parse error for '{col.name}': {e}")
        except Exception as e:
            last_error = str(e)
            print(f"[dax_translator] API error for '{col.name}': {type(e).__name__}: {e}")
            break

    print(f"[dax_translator] BLOCKER: '{col.name}' failed after {max_retries+1} attempts — {last_error}")
    return TranslationResult(
        source_id=col.id,
        target_artifact=f"// TRANSLATION FAILED\n// Tableau: {formula}\n// Error: {last_error}",
        target_kind="measure",
        confidence=0.0,
        method="llm",
        caveats=[f"LLM translation failed after {max_retries+1} attempts: {last_error}"],
        needs_review=True,
        review_priority="blocker",
    )
