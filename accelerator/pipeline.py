"""
MigrationPipeline: end-to-end orchestrator.
Chains ingestion → parser → analyzer → translators → generators → packager.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List

from accelerator.ingestion import ingest, RawArtifact
from accelerator.parser import parse
from accelerator.analyzer import analyze
from accelerator.ir.schema import (
    IRMigrationUnit, IRAnalysisResults, TranslationResult
)
from accelerator.translators import (
    translate_calculated_field, translate_visual,
    translate_filter, translate_parameter, translate_dashboard, translate_schema
)
from accelerator.generators import generate_tmdl, generate_m, generate_report, package, build_pbit, write_model_bim
from accelerator.generators.packager import clean_output

# Valid output formats
OUTPUT_FORMAT_PBIP = "pbip"   # folder-based project (default, works with git)
OUTPUT_FORMAT_PBIT = "pbit"   # single-file template (easier to share/email)
OUTPUT_FORMAT_BOTH = "both"   # write both


@dataclass
class MigrationResult:
    unit: IRMigrationUnit
    analysis: IRAnalysisResults
    translations: List[TranslationResult]
    output_dir: Path
    report: dict
    pbit_path: Optional[Path] = None   # set when output_format includes "pbit"


class MigrationPipeline:
    def __init__(
        self,
        output_base: str | Path = "./pbi_output",
        project_name: str = "MigratedReport",
        output_format: str = OUTPUT_FORMAT_PBIP,
        clean: bool = False,
    ):
        self.output_base   = Path(output_base)
        self.project_name  = project_name
        self.output_format = output_format  # "pbip" | "pbit" | "both"
        self.clean         = clean

    def run(self, input_path: str | Path) -> MigrationResult:
        print(f"[1/6] Ingesting {input_path}...")
        artifact = ingest(input_path)

        print("[2/6] Parsing Tableau XML...")
        unit = self.parse(artifact)

        print("[3/6] Analyzing IR...")
        analysis = self.analyze(unit)

        print("[4/6] Translating artifacts...")
        translations = self.translate(unit)

        output_dir = self.output_base / Path(input_path).stem
        if self.clean:
            clean_output(output_dir)
        else:
            output_dir.mkdir(parents=True, exist_ok=True)

        print("[5/6] Generating Power BI artifacts...")
        translations += generate_tmdl(unit, output_dir)
        translations += generate_m(unit, output_dir)
        translations += generate_report(unit, output_dir)
        write_model_bim(self.project_name, unit, output_dir)

        from accelerator.generators.packager import build_migration_report
        report = build_migration_report(unit, translations, output_dir)

        pbit_path = None
        fmt = self.output_format.lower()

        if fmt in (OUTPUT_FORMAT_PBIP, OUTPUT_FORMAT_BOTH):
            print(f"[6/6] Packaging PBIP...")
            package(self.project_name, unit, translations, output_dir)

        if fmt in (OUTPUT_FORMAT_PBIT, OUTPUT_FORMAT_BOTH):
            print(f"[6/6] Building .pbit template...")
            # packager renames Report/ → <name>.Report/ — run it first so pbit_generator
            # can find the files at the right paths
            if fmt == OUTPUT_FORMAT_PBIT:
                package(self.project_name, unit, translations, output_dir)
            pbit_path = build_pbit(self.project_name, unit, translations, output_dir)

        print(f"\nDone. Output: {output_dir}")
        return MigrationResult(
            unit=unit,
            analysis=analysis,
            translations=translations,
            output_dir=output_dir,
            report=report,
            pbit_path=pbit_path,
        )

    def parse(self, artifact_or_path) -> IRMigrationUnit:
        if isinstance(artifact_or_path, RawArtifact):
            return parse(artifact_or_path)
        artifact = ingest(artifact_or_path)
        return parse(artifact)

    def analyze(self, unit: IRMigrationUnit) -> IRAnalysisResults:
        return analyze(unit)

    def translate(self, unit: IRMigrationUnit) -> List[TranslationResult]:
        results: List[TranslationResult] = []

        from accelerator.generators.tmdl_generator import _safe_table_name
        for ds in unit.data_sources:
            table_name = _safe_table_name(ds.tables[0].name) if ds.tables else "Table"
            for col in ds.columns:
                if col.is_calculated:
                    results.append(translate_calculated_field(col, table_name))
            results.extend(translate_schema(ds))

        for ws in unit.worksheets:
            results.append(translate_visual(ws))
            for f in ws.filters:
                results.append(translate_filter(f))

        for dash in unit.dashboards:
            results.append(translate_dashboard(dash))

        for param in unit.parameters:
            results.append(translate_parameter(param))

        return results
