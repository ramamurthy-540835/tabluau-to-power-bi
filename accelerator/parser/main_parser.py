"""
Top-level parser: converts a RawArtifact into an IRMigrationUnit
by dispatching to sub-parsers.
"""
from __future__ import annotations
from datetime import datetime, timezone
from lxml import etree

from accelerator.ingestion.ingestor import RawArtifact
from accelerator.ir.schema import IRMigrationUnit
from accelerator.parser.datasource_parser import parse_datasource
from accelerator.parser.worksheet_parser import parse_worksheet
from accelerator.parser.dashboard_parser import parse_dashboard


def parse(artifact: RawArtifact) -> IRMigrationUnit:
    root = etree.fromstring(artifact.raw_xml.encode("utf-8"))
    unit = IRMigrationUnit(
        source_file=artifact.source_path,
        source_hash=artifact.source_hash,
        source_version=root.get("source-build") or root.get("version") or "unknown",
        parsed_at=datetime.now(timezone.utc).isoformat(),
    )

    # Parse data sources
    for ds_el in root.findall(".//datasource"):
        if ds_el.get("name") in ("Parameters", None):
            continue
        ds = parse_datasource(ds_el)
        unit.data_sources.append(ds)

    # Parse worksheets
    ds_by_name = {ds.name: ds for ds in unit.data_sources}
    for ws_el in root.findall(".//worksheet"):
        ws = parse_worksheet(ws_el)
        # Link worksheet to data source
        ds_dep = ws_el.find(".//datasource-dependencies")
        if ds_dep is not None:
            dep_name = ds_dep.get("datasource", "")
            if dep_name in ds_by_name:
                ws.data_source_id = ds_by_name[dep_name].id
        unit.worksheets.append(ws)

    # Parse dashboards
    ws_by_name = {ws.name: ws for ws in unit.worksheets}
    for dash_el in root.findall(".//dashboard"):
        dash = parse_dashboard(dash_el)
        # Back-link worksheets to this dashboard
        for zone in dash.zones:
            if zone.worksheet_name and zone.worksheet_name in ws_by_name:
                ws = ws_by_name[zone.worksheet_name]
                if dash.id not in ws.used_in_dashboards:
                    ws.used_in_dashboards.append(dash.id)
        unit.dashboards.append(dash)

    return unit
