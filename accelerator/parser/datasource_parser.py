"""Parses Tableau <datasource> XML elements into IRDataSource objects."""
from __future__ import annotations
from uuid import uuid4
from lxml import etree

from accelerator.ir.schema import (
    IRDataSource, IRConnection, IRTable, IRJoin, IRColumn, IRFilter
)
from accelerator.parser.formula_parser import parse_formula

NS = "http://www.tableausoftware.com/xml/user"

_DTYPE_MAP = {
    "string": "string", "str": "string",
    "integer": "int", "int": "int",
    "real": "decimal", "float": "decimal", "double": "decimal",
    "boolean": "boolean", "bool": "boolean",
    "date": "date",
    "datetime": "datetime", "timestamp": "datetime",
}

_ROLE_MAP = {
    "dimension": "dimension",
    "measure": "measure",
}


def _attr(el, name: str, default: str = "") -> str:
    return el.get(name, default) or default


def _parse_connection(conn_el) -> IRConnection:
    return IRConnection(
        id=uuid4(),
        connection_class=_attr(conn_el, "class", "unknown"),
        server=_attr(conn_el, "server") or None,
        port=int(_attr(conn_el, "port")) if _attr(conn_el, "port").isdigit() else None,
        database=_attr(conn_el, "dbname") or _attr(conn_el, "database") or None,
        schema_name=_attr(conn_el, "schema") or None,
        username=_attr(conn_el, "username") or None,
        authentication=_attr(conn_el, "authentication") or None,
        raw_attributes=dict(conn_el.attrib),
    )


def _parse_table(rel_el, connection_id) -> IRTable:
    tbl_name = _attr(rel_el, "table") or _attr(rel_el, "name")
    tbl_name = tbl_name.strip("[]\"'")
    return IRTable(
        id=uuid4(),
        name=tbl_name,
        alias=_attr(rel_el, "name") or None,
        schema_name=_attr(rel_el, "schema") or None,
        connection_id=connection_id,
    )


def _parse_join(rel_el, left_id, right_id) -> IRJoin:
    join_type = _attr(rel_el, "join", "inner").lower()
    conditions = []
    for clause in rel_el.findall(".//clause"):
        expr = _attr(clause, "expression")
        if expr:
            conditions.append({"expression": expr})
    for expr_el in rel_el.findall(".//expression"):
        op = _attr(expr_el, "op")
        if op in ("=", "=="):
            cols = expr_el.findall("expression")
            if len(cols) == 2:
                conditions.append({
                    "left": _attr(cols[0], "op") or cols[0].text or "",
                    "right": _attr(cols[1], "op") or cols[1].text or "",
                })
    return IRJoin(
        id=uuid4(),
        join_type=join_type if join_type in ("inner", "left", "right", "full", "cross") else "inner",
        left_table_id=left_id,
        right_table_id=right_id,
        conditions=conditions,
    )


def _parse_column(col_el) -> IRColumn:
    formula_str = _attr(col_el, "formula") or None
    calc_el = col_el.find("calculation")
    if calc_el is not None:
        formula_str = _attr(calc_el, "formula") or formula_str

    ast = parse_formula(formula_str) if formula_str else None
    raw_type = _attr(col_el, "datatype", "string").lower()
    raw_role = _attr(col_el, "role", "dimension").lower()
    raw_agg = _attr(col_el, "default-role", "").lower() or _attr(col_el, "aggregation", "").lower()

    agg_map = {"sum": "sum", "avg": "avg", "average": "avg", "count": "count",
               "countd": "countd", "min": "min", "max": "max", "median": "median"}

    return IRColumn(
        id=uuid4(),
        name=_attr(col_el, "name", "unknown").strip("[]"),
        business_name=_attr(col_el, "caption") or None,
        datatype=_DTYPE_MAP.get(raw_type, "unknown"),
        role=_ROLE_MAP.get(raw_role, "unknown"),
        aggregation=agg_map.get(raw_agg) if raw_agg else None,
        is_calculated=formula_str is not None,
        formula=formula_str,
        formula_ast=ast,
    )


def parse_datasource(ds_el) -> IRDataSource:
    ds = IRDataSource(
        id=uuid4(),
        name=_attr(ds_el, "name", "unknown"),
        caption=_attr(ds_el, "caption") or None,
    )

    # Connections — search broadly: direct children first, then nested
    # Federated datasources wrap the real connection inside <connection class='federated'>
    connection_id = uuid4()
    all_conn_els = ds_el.findall(".//connection")
    # Prefer non-federated connections (the actual database connection)
    sorted_conns = sorted(
        all_conn_els,
        key=lambda el: 0 if _attr(el, "class") not in ("federated", "") else 1
    )
    for conn_el in sorted_conns:
        cls = _attr(conn_el, "class")
        if cls and cls != "federated":
            c = _parse_connection(conn_el)
            connection_id = c.id
            ds.connections.append(c)
            break
    # Fallback to any connection if none found above
    if not ds.connections:
        for conn_el in all_conn_els:
            if _attr(conn_el, "class"):
                c = _parse_connection(conn_el)
                connection_id = c.id
                ds.connections.append(c)
                break

    # Tables and joins from relation elements
    table_map: dict[str, object] = {}
    for rel_el in ds_el.findall(".//relation"):
        rel_type = _attr(rel_el, "type", "")
        if rel_type == "table":
            tbl = _parse_table(rel_el, connection_id)
            table_map[_attr(rel_el, "name")] = tbl
            ds.tables.append(tbl)
        elif rel_type in ("join", "union"):
            rels = rel_el.findall("relation[@type='table']")
            if len(rels) >= 2:
                left_name = _attr(rels[0], "name")
                right_name = _attr(rels[1], "name")
                if left_name not in table_map:
                    t = _parse_table(rels[0], connection_id)
                    table_map[left_name] = t
                    ds.tables.append(t)
                if right_name not in table_map:
                    t = _parse_table(rels[1], connection_id)
                    table_map[right_name] = t
                    ds.tables.append(t)
                j = _parse_join(
                    rel_el,
                    table_map[left_name].id,
                    table_map[right_name].id,
                )
                ds.joins.append(j)

    # Columns
    for col_el in ds_el.findall(".//column"):
        if _attr(col_el, "name"):
            ds.columns.append(_parse_column(col_el))

    return ds
