"""
Command-line interface for the Tableau to Power BI Migration Accelerator.

Usage:
  python cli.py analyze --input ./workbooks/ --output ./inventory.json
  python cli.py migrate --input sales.twbx --output ./pbi_output/
  python cli.py migrate --input ./workbooks/ --output ./pbi_output/
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()


@click.group()
def cli():
    """Tableau to Power BI Migration Accelerator — Mastech Digital."""
    pass


@cli.command()
@click.option("--input", "-i", "input_path", required=True, help="Path to .twb/.twbx file or folder of workbooks.")
@click.option("--output", "-o", "output_path", default="./inventory.json", help="Path for inventory JSON output.")
def analyze(input_path: str, output_path: str):
    """Inventory and analyze Tableau artifacts without translating."""
    from accelerator.ingestion import ingest
    from accelerator.parser import parse
    from accelerator.analyzer import analyze as run_analyze

    paths = _collect_paths(input_path)
    if not paths:
        console.print(f"[red]No Tableau files found at {input_path}[/red]")
        sys.exit(1)

    inventory = []
    for p in paths:
        try:
            console.print(f"  Analyzing {p.name}...")
            artifact = ingest(p)
            unit = parse(artifact)
            analysis = run_analyze(unit)
            inventory.append({
                "file": str(p),
                "source_hash": unit.source_hash,
                "data_sources": len(unit.data_sources),
                "worksheets": len(unit.worksheets),
                "dashboards": len(unit.dashboards),
                "calculated_fields": sum(len([c for c in ds.columns if c.is_calculated]) for ds in unit.data_sources),
                "anti_patterns": len(analysis.anti_patterns),
                "risk_scores": analysis.migration_risk_scores,
            })
        except Exception as e:
            console.print(f"  [red]Error: {p.name}: {e}[/red]")
            inventory.append({"file": str(p), "error": str(e)})

    Path(output_path).write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    console.print(Panel(f"[green]Inventory complete. {len(inventory)} files analyzed.\nOutput: {output_path}[/green]"))

    # Print summary table
    t = Table(title="Inventory Summary")
    t.add_column("File", style="cyan")
    t.add_column("Data Sources", justify="right")
    t.add_column("Worksheets", justify="right")
    t.add_column("Dashboards", justify="right")
    t.add_column("Calc Fields", justify="right")
    t.add_column("Anti-patterns", justify="right")
    for row in inventory:
        if "error" not in row:
            t.add_row(
                Path(row["file"]).name,
                str(row["data_sources"]),
                str(row["worksheets"]),
                str(row["dashboards"]),
                str(row["calculated_fields"]),
                str(row["anti_patterns"]),
            )
    console.print(t)


@cli.command()
@click.option("--input", "-i", "input_path", required=True, help="Path to .twb/.twbx file or folder.")
@click.option("--output", "-o", "output_path", default="./pbi_output", help="Output folder for PBIP packages.")
@click.option("--project-name", "-n", default="MigratedReport", help="Power BI project name.")
@click.option("--format", "-f", "output_format", default="both",
              type=click.Choice(["pbip", "pbit", "both"], case_sensitive=False),
              help="Output format: pbip (folder), pbit (single file), or both (default).")
@click.option("--llm-budget", default=5.0, type=float, help="Max LLM cost in USD per workbook.")
@click.option("--clean", is_flag=True, default=False, help="Wipe output directory before each run for a guaranteed fresh slate.")
def migrate(input_path: str, output_path: str, project_name: str, output_format: str, llm_budget: float, clean: bool):
    """Migrate Tableau workbooks to Power BI. Produces a .pbit file (openable in Power BI Desktop) and/or a PBIP folder."""
    from accelerator.pipeline import MigrationPipeline

    paths = _collect_paths(input_path)
    if not paths:
        console.print(f"[red]No Tableau files found at {input_path}[/red]")
        sys.exit(1)

    pipeline = MigrationPipeline(output_base=output_path, project_name=project_name, output_format=output_format, clean=clean)

    for p in paths:
        console.rule(f"Migrating: {p.name}")
        try:
            result = pipeline.run(p)
            _print_result_summary(result)
        except Exception as e:
            console.print(f"[red]Migration failed for {p.name}: {e}[/red]")
            import traceback
            traceback.print_exc()


def _print_result_summary(result):
    from rich.table import Table
    report = result.report
    stats = report.get("translation_stats", {})
    rq = report.get("review_queue_summary", {})

    pbit_line = f"\nPBIT (single file):  {result.pbit_path}" if result.pbit_path else ""
    pbip_line = f"PBIP (folder):       {result.output_dir}" if result.pbit_path else f"Output: {result.output_dir}"

    # Detect PBIP ZIP if packager produced one
    from pathlib import Path as _Path
    project_name = _Path(result.output_dir).name if result.output_dir else "MigratedReport"
    pbip_zip = _Path(result.output_dir) / f"{project_name}_PBIP.zip" if result.output_dir else None
    zip_line = f"\nPBIP ZIP (shareable): {pbip_zip}" if pbip_zip and pbip_zip.exists() else ""

    console.print(Panel(
        f"[green]Migration complete![/green]\n"
        f"{pbip_line}{pbit_line}{zip_line}\n"
        f"Coverage: {stats.get('coverage_pct')}%  |  Avg confidence: {stats.get('average_confidence')}\n"
        f"Review queue: {rq.get('needs_review')} items  |  Blockers: {rq.get('blockers')}\n\n"
        f"[yellow]To open: use the .pbit file, or extract the _PBIP.zip and open the .pbip inside[/yellow]",
        title="Result"
    ))


def _collect_paths(input_path: str) -> list[Path]:
    p = Path(input_path)
    if p.is_file():
        return [p]
    if p.is_dir():
        paths = []
        for ext in ("*.twb", "*.twbx", "*.tds", "*.tdsx"):
            paths.extend(p.glob(ext))
        return sorted(paths)
    return []


if __name__ == "__main__":
    cli()
