"""
Analyzer module: computes lineage, complexity scores, anti-patterns,
star-schema inference, and migration risk scores from the IR.
"""
from __future__ import annotations
from pathlib import Path
from uuid import UUID
from typing import Dict, List, Set
import yaml

from accelerator.ir.schema import (
    IRMigrationUnit, IRAnalysisResults, IRColumn, IRDataSource
)
from accelerator.parser.formula_parser import ast_depth, has_lod, has_table_calc

_THIS_DIR = Path(__file__).parent.parent.parent / "config"


def _load_thresholds() -> dict:
    cfg = _THIS_DIR / "complexity_thresholds.yaml"
    if cfg.exists():
        return yaml.safe_load(cfg.read_text(encoding="utf-8"))
    return {}


def _calc_complexity(col: IRColumn, thresholds: dict) -> int:
    score = 0
    if col.formula_ast is None:
        return score
    depth = ast_depth(col.formula_ast)
    score += max(0, depth - 3) * thresholds.get("ast_depth_weight", 10)
    if has_lod(col.formula_ast):
        score += thresholds.get("lod_presence", 30)
    if has_table_calc(col.formula_ast):
        score += thresholds.get("table_calc_presence", 25)
    score += len(col.dependencies) * thresholds.get("nested_calc_per_dep", 8)
    return min(score, 100)


def _build_lineage(ds: IRDataSource) -> Dict[str, List[str]]:
    """Map each calculated field name to the names of fields it depends on."""
    name_map = {c.name: c for c in ds.columns}
    lineage: Dict[str, List[str]] = {}
    for col in ds.columns:
        if col.is_calculated and col.formula:
            deps = []
            for dep_id in col.dependencies:
                dep = next((c for c in ds.columns if c.id == dep_id), None)
                if dep:
                    deps.append(dep.name)
            lineage[col.name] = deps
    return lineage


def _detect_anti_patterns(ds: IRDataSource) -> List[dict]:
    patterns = []
    # Blended data sources
    if len(ds.connections) > 1:
        patterns.append({
            "type": "blended_datasource",
            "severity": "high",
            "description": f"Data source '{ds.name}' uses multiple connections (blending). Convert to model relationships.",
            "datasource": ds.name,
        })
    # Flat-table star schema missing
    table_names = {t.name.lower() for t in ds.tables}
    date_tables = [n for n in table_names if "date" in n or "calendar" in n or "time" in n]
    if len(ds.tables) > 1 and not date_tables:
        patterns.append({
            "type": "missing_date_dimension",
            "severity": "medium",
            "description": f"No date dimension table detected in '{ds.name}'. Add a shared date table.",
            "datasource": ds.name,
        })
    # Calculated fields that should be measures (contain aggregation but role=dimension)
    for col in ds.columns:
        if col.is_calculated and col.role == "dimension" and col.aggregation:
            patterns.append({
                "type": "agg_in_dimension",
                "severity": "medium",
                "description": f"Column '{col.name}' has aggregation but is set as dimension. Convert to measure.",
                "column": col.name,
            })
    return patterns


def _infer_star_schema(ds: IRDataSource) -> dict:
    """Simple heuristic: high-fan-in tables are facts, others are dimensions."""
    if not ds.tables:
        return {}
    fan_in: Dict[UUID, int] = {t.id: 0 for t in ds.tables}
    for j in ds.joins:
        fan_in[j.right_table_id] = fan_in.get(j.right_table_id, 0) + 1
    facts = [t.name for t in ds.tables if fan_in.get(t.id, 0) >= 2]
    dims = [t.name for t in ds.tables if fan_in.get(t.id, 0) < 2]
    return {"facts": facts, "dimensions": dims, "inferred": True}


def analyze(unit: IRMigrationUnit) -> IRAnalysisResults:
    thresholds = _load_thresholds()
    results = IRAnalysisResults()

    # Field lineage and complexity
    for ds in unit.data_sources:
        results.field_lineage.update(_build_lineage(ds))
        for col in ds.columns:
            col.complexity_score = _calc_complexity(col, thresholds)
        results.anti_patterns.extend(_detect_anti_patterns(ds))

    # Star schema inference
    if unit.data_sources:
        results.proposed_star_schema = _infer_star_schema(unit.data_sources[0])

    # Unused worksheets
    used_ws_ids: Set[UUID] = set()
    for dash in unit.dashboards:
        for zone in dash.zones:
            if zone.worksheet_name:
                ws = next((w for w in unit.worksheets if w.name == zone.worksheet_name), None)
                if ws:
                    used_ws_ids.add(ws.id)
    for ws in unit.worksheets:
        if ws.id not in used_ws_ids:
            results.unused_worksheets.append(ws.id)

    # Migration risk scores
    for ds in unit.data_sources:
        max_calc = max((c.complexity_score or 0 for c in ds.columns), default=0)
        anti_score = min(len(results.anti_patterns) * 10, 100)
        risk = int(max_calc * 0.5 + anti_score * 0.3)
        results.migration_risk_scores[ds.name] = min(risk, 100)

    for dash in unit.dashboards:
        zone_score = len(dash.zones) * thresholds.get("zone_count_weight", 2)
        action_score = len(dash.actions) * thresholds.get("action_count_weight", 5)
        floating = sum(1 for z in dash.zones if z.is_floating)
        floating_score = floating * thresholds.get("floating_zone_penalty", 8)
        dash.complexity_score = min(zone_score + action_score + floating_score, 100)
        results.migration_risk_scores[f"dashboard:{dash.name}"] = dash.complexity_score

    unit.analysis = results
    return results
