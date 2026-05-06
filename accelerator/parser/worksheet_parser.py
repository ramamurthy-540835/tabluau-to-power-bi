"""Parses Tableau <worksheet> XML elements into IRWorksheet objects."""
from __future__ import annotations
from uuid import uuid4
from lxml import etree

from accelerator.ir.schema import IRWorksheet, IRShelves, IRFilter, IRSort, IRStyle

_MARK_MAP = {
    "bar": "bar", "line": "line", "area": "area", "pie": "pie",
    "circle": "circle", "square": "square", "shape": "shape",
    "text": "text", "gantt": "gantt", "polygon": "polygon",
    "map": "map", "density": "density", "automatic": "auto",
}


def _attr(el, name: str, default: str = "") -> str:
    return el.get(name, default) or default


def _strip_brackets(s: str) -> str:
    return s.strip("[]")


def _parse_shelves(table_el) -> IRShelves:
    shelves = IRShelves()
    if table_el is None:
        return shelves

    for pane_el in table_el.findall(".//pane"):
        for enc_el in pane_el.findall("encodings"):
            for child in enc_el:
                tag = child.tag.lower()
                field_name = _strip_brackets(_attr(child, "field", ""))
                if not field_name:
                    continue
                if tag == "rows":
                    if field_name not in shelves.rows:
                        shelves.rows.append(field_name)
                elif tag == "columns":
                    if field_name not in shelves.cols:
                        shelves.cols.append(field_name)
                elif tag in ("color", "colour"):
                    shelves.color = field_name
                elif tag == "size":
                    shelves.size = field_name
                elif tag == "label":
                    shelves.label = field_name
                elif tag == "detail":
                    if field_name not in shelves.detail:
                        shelves.detail.append(field_name)
                elif tag == "tooltip":
                    if field_name not in shelves.tooltip:
                        shelves.tooltip.append(field_name)
                elif tag == "shape":
                    shelves.shape = field_name
                elif tag == "path":
                    shelves.path = field_name

    # Fallback: read from rows/cols elements directly
    for rows_el in table_el.findall(".//rows"):
        text = (rows_el.text or "").strip()
        for part in text.split(","):
            part = _strip_brackets(part.strip())
            if part and part not in shelves.rows:
                shelves.rows.append(part)

    for cols_el in table_el.findall(".//cols"):
        text = (cols_el.text or "").strip()
        for part in text.split(","):
            part = _strip_brackets(part.strip())
            if part and part not in shelves.cols:
                shelves.cols.append(part)

    return shelves


def _parse_filters(table_el) -> list[IRFilter]:
    filters = []
    if table_el is None:
        return filters
    for f_el in table_el.findall(".//filter"):
        col_name = _strip_brackets(_attr(f_el, "column", ""))
        ftype = _attr(f_el, "class", "categorical")
        include_vals = [_attr(m, "value") for m in f_el.findall(".//member[@member='true']")]
        exclude_vals = [_attr(m, "value") for m in f_el.findall(".//member[@member='false']")]
        min_v = _attr(f_el.find("min") or etree.Element("x"), "value") or None
        max_v = _attr(f_el.find("max") or etree.Element("x"), "value") or None
        is_context = _attr(f_el, "filter-group", "") == "context"
        filters.append(IRFilter(
            id=uuid4(),
            filter_type=ftype if ftype in ("categorical","range","relative_date","context","set","top_n","custom") else "custom",
            column_name=col_name or None,
            include_values=include_vals,
            exclude_values=exclude_vals,
            min_value=min_v,
            max_value=max_v,
            is_context_filter=is_context,
        ))
    return filters


def _parse_mark_type(table_el) -> str:
    if table_el is None:
        return "auto"
    mark_el = table_el.find(".//mark")
    if mark_el is not None:
        raw = _attr(mark_el, "class", "automatic").lower()
        return _MARK_MAP.get(raw, "auto")
    return "auto"


def parse_worksheet(ws_el) -> IRWorksheet:
    name = _attr(ws_el, "name", "Unnamed")
    table_el = ws_el.find(".//table")

    mark_type = _parse_mark_type(table_el)
    shelves = _parse_shelves(table_el)
    filters = _parse_filters(table_el)

    ds_dep = ws_el.find(".//datasource-dependencies")
    ds_name = _attr(ds_dep, "datasource") if ds_dep is not None else None

    style = IRStyle()

    return IRWorksheet(
        id=uuid4(),
        name=name,
        mark_type=mark_type,
        shelves=shelves,
        filters=filters,
        format=style,
    )
