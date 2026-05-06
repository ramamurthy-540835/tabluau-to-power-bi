from __future__ import annotations
from uuid import UUID, uuid4
from typing import Optional, List, Literal, Dict, Any
from pydantic import BaseModel, Field


class IRFormulaAST(BaseModel):
    node_type: Literal[
        "Literal", "ColumnRef", "ParameterRef", "FunctionCall",
        "LODExpression", "IfExpr", "CaseExpr", "AggregateCall",
        "TableCalcCall", "BinaryOp", "UnaryOp"
    ]
    value: Optional[str] = None
    children: List["IRFormulaAST"] = Field(default_factory=list)
    lod_type: Optional[Literal["FIXED", "INCLUDE", "EXCLUDE"]] = None
    lod_dimensions: List[str] = Field(default_factory=list)
    operator: Optional[str] = None
    datatype: Optional[str] = None


class IRColumn(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    business_name: Optional[str] = None
    datatype: Literal["int", "decimal", "string", "boolean", "date", "datetime", "unknown"] = "unknown"
    role: Literal["dimension", "measure", "unknown"] = "unknown"
    aggregation: Optional[Literal["sum", "avg", "count", "countd", "min", "max", "median"]] = None
    is_calculated: bool = False
    formula: Optional[str] = None
    formula_ast: Optional[IRFormulaAST] = None
    dependencies: List[UUID] = Field(default_factory=list)
    used_in_worksheets: List[UUID] = Field(default_factory=list)
    complexity_score: Optional[int] = None
    dax_expression: Optional[str] = None   # populated by translator; used by TMDL generator


class IRConnection(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    connection_class: str
    server: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    schema_name: Optional[str] = None
    username: Optional[str] = None
    authentication: Optional[str] = None
    named_connection: Optional[str] = None
    raw_attributes: Dict[str, str] = Field(default_factory=dict)


class IRTable(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    alias: Optional[str] = None
    schema_name: Optional[str] = None
    connection_id: UUID


class IRJoin(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    join_type: Literal["inner", "left", "right", "full", "cross"] = "inner"
    left_table_id: UUID
    right_table_id: UUID
    conditions: List[Dict[str, str]] = Field(default_factory=list)


class IRRelationship(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    from_table: UUID
    from_column: UUID
    to_table: UUID
    to_column: UUID
    cardinality: Literal["1:1", "1:M", "M:1", "M:M"] = "M:1"
    cross_filter: Literal["single", "both"] = "single"
    is_active: bool = True
    inferred: bool = False


class IRFilter(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    filter_type: Literal["categorical", "range", "relative_date", "context", "set", "top_n", "custom"] = "categorical"
    column_id: Optional[UUID] = None
    column_name: Optional[str] = None
    include_values: List[str] = Field(default_factory=list)
    exclude_values: List[str] = Field(default_factory=list)
    min_value: Optional[str] = None
    max_value: Optional[str] = None
    is_context_filter: bool = False
    raw_filter: Optional[str] = None


class IRDataSource(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    caption: Optional[str] = None
    connections: List[IRConnection] = Field(default_factory=list)
    tables: List[IRTable] = Field(default_factory=list)
    joins: List[IRJoin] = Field(default_factory=list)
    columns: List[IRColumn] = Field(default_factory=list)
    relationships: List[IRRelationship] = Field(default_factory=list)
    filters: List[IRFilter] = Field(default_factory=list)
    is_published: bool = False


class IRParameter(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    caption: Optional[str] = None
    datatype: Literal["int", "decimal", "string", "boolean", "date", "datetime"] = "string"
    current_value: Optional[str] = None
    allowable_mode: Literal["all", "list", "range"] = "all"
    allowable_values: List[str] = Field(default_factory=list)
    min_value: Optional[str] = None
    max_value: Optional[str] = None
    step_size: Optional[str] = None


class IRSort(BaseModel):
    column_id: Optional[UUID] = None
    column_name: Optional[str] = None
    direction: Literal["asc", "desc"] = "asc"
    sort_type: Literal["alphabetic", "computed", "datasource", "manual"] = "alphabetic"


class IRStyle(BaseModel):
    font_family: Optional[str] = None
    font_size: Optional[int] = None
    background_color: Optional[str] = None
    text_color: Optional[str] = None
    number_format: Optional[str] = None
    raw_formats: Dict[str, str] = Field(default_factory=dict)


class IRShelves(BaseModel):
    rows: List[str] = Field(default_factory=list)
    cols: List[str] = Field(default_factory=list)
    color: Optional[str] = None
    size: Optional[str] = None
    label: Optional[str] = None
    detail: List[str] = Field(default_factory=list)
    tooltip: List[str] = Field(default_factory=list)
    path: Optional[str] = None
    shape: Optional[str] = None


class IRWorksheet(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    data_source_id: Optional[UUID] = None
    mark_type: Literal["bar", "line", "area", "pie", "scatter", "map", "gantt", "heatmap", "text", "auto", "circle", "square", "shape", "polygon", "density", "unknown"] = "auto"
    shelves: IRShelves = Field(default_factory=IRShelves)
    filters: List[IRFilter] = Field(default_factory=list)
    parameters_used: List[UUID] = Field(default_factory=list)
    sort: Optional[IRSort] = None
    format: IRStyle = Field(default_factory=IRStyle)
    used_in_dashboards: List[UUID] = Field(default_factory=list)
    complexity_score: Optional[int] = None


class IRZone(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    zone_type: Literal["worksheet", "text", "image", "web", "blank", "container", "layout-container"] = "blank"
    worksheet_name: Optional[str] = None
    x: int = 0
    y: int = 0
    w: int = 400
    h: int = 300
    is_floating: bool = False
    children: List["IRZone"] = Field(default_factory=list)


class IRAction(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    action_type: Literal["filter", "highlight", "url", "set", "parameter"] = "filter"
    source_sheets: List[str] = Field(default_factory=list)
    target_sheets: List[str] = Field(default_factory=list)
    url: Optional[str] = None
    run_on: Literal["select", "hover", "menu"] = "select"


class IRDashboard(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    width: int = 1000
    height: int = 800
    sizing_mode: Literal["fixed", "automatic", "range"] = "fixed"
    zones: List[IRZone] = Field(default_factory=list)
    actions: List[IRAction] = Field(default_factory=list)
    parameter_controls: List[UUID] = Field(default_factory=list)
    complexity_score: Optional[int] = None


class IRAnalysisResults(BaseModel):
    unused_worksheets: List[UUID] = Field(default_factory=list)
    unused_columns: List[UUID] = Field(default_factory=list)
    anti_patterns: List[Dict[str, Any]] = Field(default_factory=list)
    data_source_overlaps: List[List[UUID]] = Field(default_factory=list)
    migration_risk_scores: Dict[str, int] = Field(default_factory=dict)
    proposed_star_schema: Optional[Dict[str, Any]] = None
    field_lineage: Dict[str, List[str]] = Field(default_factory=dict)


class IRMigrationUnit(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source_file: str
    source_hash: Optional[str] = None
    source_version: Optional[str] = None
    parsed_at: Optional[str] = None
    data_sources: List[IRDataSource] = Field(default_factory=list)
    parameters: List[IRParameter] = Field(default_factory=list)
    worksheets: List[IRWorksheet] = Field(default_factory=list)
    dashboards: List[IRDashboard] = Field(default_factory=list)
    analysis: Optional[IRAnalysisResults] = None


class TranslationResult(BaseModel):
    source_id: UUID
    target_artifact: str
    target_kind: Literal["measure", "column", "relationship", "visual", "query", "page", "model", "theme"]
    confidence: float = 1.0
    method: Literal["deterministic", "llm", "hybrid", "manual"] = "deterministic"
    rationale: Optional[str] = None
    caveats: List[str] = Field(default_factory=list)
    validation_status: Literal["passed", "syntax_failed", "semantic_warn", "not_validated"] = "not_validated"
    needs_review: bool = False
    review_priority: Literal["low", "medium", "high", "blocker"] = "low"
