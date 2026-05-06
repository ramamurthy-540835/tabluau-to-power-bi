"""
Assembles the final PBIP package and generates the migration report.
"""
from __future__ import annotations
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from uuid import uuid4

from accelerator.ir.schema import IRMigrationUnit, TranslationResult


def build_pbip_manifest(project_name: str, output_dir: Path) -> None:
    # Only list the report in artifacts[].  The SemanticModel is referenced
    # from within the report's definition.pbir (byPath), so PBI Desktop finds
    # it automatically.  Older PBI Desktop versions reject "semanticModel" as
    # an unknown artifacts property and refuse to open the file.
    manifest = {
        "version": "1.0",
        "artifacts": [
            {"report": {"path": f"{project_name}.Report"}},
        ],
        "settings": {"enableAutoRecovery": True},
    }
    pbip_path = output_dir / f"{project_name}.pbip"
    pbip_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def build_review_queue(
    unit: IRMigrationUnit,
    translations: List[TranslationResult],
    output_dir: Path,
) -> dict:
    queue: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_file": unit.source_file,
        "items": [],
    }

    priority_order = {"blocker": 0, "high": 1, "medium": 2, "low": 3}
    review_items = [t for t in translations if t.needs_review]
    review_items.sort(key=lambda t: priority_order.get(t.review_priority, 4))

    for item in review_items:
        queue["items"].append({
            "source_id": str(item.source_id),
            "kind": item.target_kind,
            "priority": item.review_priority,
            "method": item.method,
            "confidence": item.confidence,
            "rationale": item.rationale,
            "caveats": item.caveats,
            "validation_status": item.validation_status,
            "artifact_preview": item.target_artifact[:500] + ("..." if len(item.target_artifact) > 500 else ""),
        })

    queue["summary"] = {
        "total_items": len(review_items),
        "blockers": sum(1 for i in review_items if i.review_priority == "blocker"),
        "high": sum(1 for i in review_items if i.review_priority == "high"),
        "medium": sum(1 for i in review_items if i.review_priority == "medium"),
        "low": sum(1 for i in review_items if i.review_priority == "low"),
    }

    (output_dir / "review_queue.json").write_text(json.dumps(queue, indent=2), encoding="utf-8")
    return queue


def build_migration_report(
    unit: IRMigrationUnit,
    translations: List[TranslationResult],
    output_dir: Path,
) -> dict:
    total = len(translations)
    deterministic = sum(1 for t in translations if t.method == "deterministic")
    llm_assisted = sum(1 for t in translations if t.method == "llm")
    manual = sum(1 for t in translations if t.method == "manual")
    avg_confidence = (sum(t.confidence for t in translations) / total) if total else 0

    ds_count = len(unit.data_sources)
    ws_count = len(unit.worksheets)
    dash_count = len(unit.dashboards)
    calc_count = sum(len([c for c in ds.columns if c.is_calculated]) for ds in unit.data_sources)

    risk_scores = {}
    if unit.analysis:
        risk_scores = unit.analysis.migration_risk_scores

    report = {
        "title": "Tableau to Power BI Migration Report",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_file": unit.source_file,
        "source_hash": unit.source_hash,
        "source_version": unit.source_version,
        "inventory": {
            "data_sources": ds_count,
            "worksheets": ws_count,
            "dashboards": dash_count,
            "calculated_fields": calc_count,
            "parameters": len(unit.parameters),
        },
        "translation_stats": {
            "total_artifacts": total,
            "deterministic": deterministic,
            "llm_assisted": llm_assisted,
            "manual_required": manual,
            "average_confidence": round(avg_confidence, 3),
            "coverage_pct": round((deterministic + llm_assisted) / max(total, 1) * 100, 1),
        },
        "migration_risk_scores": risk_scores,
        "anti_patterns": unit.analysis.anti_patterns if unit.analysis else [],
        "proposed_star_schema": unit.analysis.proposed_star_schema if unit.analysis else None,
        "unused_worksheets": [
            ws.name for ws in unit.worksheets
            if unit.analysis and ws.id in unit.analysis.unused_worksheets
        ],
        "review_queue_summary": {
            "needs_review": sum(1 for t in translations if t.needs_review),
            "blockers": sum(1 for t in translations if t.review_priority == "blocker"),
        },
    }

    (output_dir / "migration_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Human-readable summary
    summary_lines = [
        "=" * 60,
        "TABLEAU → POWER BI MIGRATION REPORT",
        "=" * 60,
        f"Source:       {unit.source_file}",
        f"Generated:    {report['generated_at']}",
        "",
        "INVENTORY",
        f"  Data sources:      {ds_count}",
        f"  Worksheets:        {ws_count}",
        f"  Dashboards:        {dash_count}",
        f"  Calculated fields: {calc_count}",
        "",
        "TRANSLATION",
        f"  Deterministic:     {deterministic} ({round(deterministic/max(total,1)*100)}%)",
        f"  LLM-assisted:      {llm_assisted} ({round(llm_assisted/max(total,1)*100)}%)",
        f"  Manual required:   {manual}",
        f"  Coverage:          {report['translation_stats']['coverage_pct']}%",
        f"  Avg confidence:    {report['translation_stats']['average_confidence']}",
        "",
        "REVIEW QUEUE",
        f"  Items needing review: {report['review_queue_summary']['needs_review']}",
        f"  Blockers:            {report['review_queue_summary']['blockers']}",
        "=" * 60,
    ]
    (output_dir / "migration_report.txt").write_text("\n".join(summary_lines), encoding="utf-8")
    return report


import hashlib as _hashlib
import zipfile as _zipfile


def _zip_pbip_folder(project_name: str, output_dir: Path) -> Path:
    """ZIP the entire PBIP output folder so it can be shared as a single file."""
    zip_path = output_dir / f"{project_name}_PBIP.zip"
    skip = {".lastrun_hash", f"{project_name}_PBIP.zip"}
    with _zipfile.ZipFile(zip_path, "w", _zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(output_dir.rglob("*")):
            if f.is_file() and f.name not in skip:
                zf.write(f, f.relative_to(output_dir))
    return zip_path


def _output_fingerprint(output_dir: Path) -> str:
    """SHA-256 of all generated artifact file contents (excluding hash file itself)."""
    h = _hashlib.sha256()
    skip = {".lastrun_hash"}
    for f in sorted(output_dir.rglob("*")):
        if f.is_file() and f.name not in skip:
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def clean_output(output_dir: Path) -> None:
    """Wipe the entire output directory so each run starts from a clean slate."""
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"[packager] Cleaned output directory: {output_dir}")


def package(
    project_name: str,
    unit: IRMigrationUnit,
    translations: List[TranslationResult],
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Move Report and SemanticModel dirs to project-named subdirs.
    # On re-runs the destination already exists with stale files, so remove it
    # first so the fresh output from this run completely replaces it.
    src_report = output_dir / "Report"
    dst_report = output_dir / f"{project_name}.Report"
    if src_report.exists():
        if dst_report.exists():
            shutil.rmtree(dst_report)
        src_report.rename(dst_report)

    src_model = output_dir / "SemanticModel"
    dst_model = output_dir / f"{project_name}.SemanticModel"
    if src_model.exists():
        if dst_model.exists():
            shutil.rmtree(dst_model)
        src_model.rename(dst_model)

    # Fix definition.pbir: after renaming, the relative path must match the new
    # folder name.  report_generator writes "../SemanticModel" which is wrong
    # once the folder is renamed to "{project_name}.SemanticModel".
    pbir_path = dst_report / "definition.pbir"
    if dst_report.exists():
        pbir_content = {
            "version": "1.0",
            "datasetReference": {
                "byPath": {"path": f"../{project_name}.SemanticModel"}
            },
        }
        pbir_path.write_text(json.dumps(pbir_content, indent=2), encoding="utf-8")

    # Create definition.pbism — Power BI Desktop requires this file to recognize
    # the folder as a valid Semantic Model.  Without it the PBIP will not open.
    dst_model.mkdir(parents=True, exist_ok=True)
    pbism_path = dst_model / "definition.pbism"
    pbism_content = {"version": "1.0", "settings": {}}
    pbism_path.write_text(json.dumps(pbism_content, indent=2), encoding="utf-8")

    # Write model.bim — Power BI Desktop reads this (JSON tabular model) when
    # definition.pbism version is "1.0".  Without it PBI Desktop falls through to
    # model.bin (binary) which we cannot generate, and shows
    # "Missing required artifact model.bin".
    from accelerator.generators.pbit_generator import write_model_bim
    write_model_bim(project_name, unit, output_dir)

    # Generate RLS security roles into SemanticModel/roles/
    from accelerator.generators.security_generator import generate_security
    sec_results = generate_security(unit, output_dir)
    translations = list(translations) + sec_results

    build_pbip_manifest(project_name, output_dir)
    build_review_queue(unit, translations, output_dir)
    build_migration_report(unit, translations, output_dir)

    # Duplicate output detection — warn if this run produced no changes
    hash_file = output_dir / ".lastrun_hash"
    current_fp = _output_fingerprint(output_dir)
    if hash_file.exists() and hash_file.read_text().strip() == current_fp:
        print("[packager] WARNING: Output is identical to the previous run - no changes were made.")
    hash_file.write_text(current_fp, encoding="utf-8")

    zip_path = _zip_pbip_folder(project_name, output_dir)
    print(f"[packager] PBIP ZIP: {zip_path}")
    print("[packager] If sharing the PBIP folder, use this ZIP -- extract all files before opening in Power BI Desktop.")

    return output_dir
