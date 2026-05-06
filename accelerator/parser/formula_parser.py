"""
Tableau formula language parser.
Produces a typed IRFormulaAST from a Tableau calculated field formula string.
Uses Lark EBNF grammar for correct handling of nested expressions.
"""
from __future__ import annotations
from typing import Optional

try:
    from lark import Lark, Transformer, Tree, Token
    LARK_AVAILABLE = True
except ImportError:
    LARK_AVAILABLE = False

from accelerator.ir.schema import IRFormulaAST

TABLEAU_GRAMMAR = r"""
    ?start: expr

    ?expr: or_expr

    ?or_expr: and_expr
            | or_expr "OR"i and_expr   -> binary_op

    ?and_expr: not_expr
             | and_expr "AND"i not_expr -> binary_op

    ?not_expr: compare_expr
             | "NOT"i not_expr         -> unary_op

    ?compare_expr: add_expr
                 | compare_expr OP add_expr -> binary_op

    ?add_expr: mul_expr
             | add_expr ADDOP mul_expr -> binary_op

    ?mul_expr: unary_arith
             | mul_expr MULOP unary_arith -> binary_op

    ?unary_arith: primary
                | "-" primary -> unary_neg

    ?primary: lod_expr
            | if_expr
            | case_expr
            | func_call
            | column_ref
            | param_ref
            | literal
            | "(" expr ")"

    lod_expr: "{" LOD_TYPE dims_clause ":" expr "}"
            | "{" ":" expr "}"          -> empty_fixed

    LOD_TYPE: "FIXED"i | "INCLUDE"i | "EXCLUDE"i

    dims_clause: "[" NAME "]" ("," "[" NAME "]")*

    if_expr: "IF"i expr "THEN"i expr ("ELSEIF"i expr "THEN"i expr)* ["ELSE"i expr] "END"i

    case_expr: "CASE"i expr ("WHEN"i expr "THEN"i expr)+ ["ELSE"i expr] "END"i

    func_call: NAME "(" [expr ("," expr)*] ")"

    column_ref: "[" NAME "]"
              | "[" NAME "]" "." "[" NAME "]"

    param_ref: "[" "Parameters" "." NAME "]"
             | "[" NAME "]"    -> column_ref

    literal: ESCAPED_STRING  -> string_literal
           | NUMBER           -> number_literal
           | "NULL"i          -> null_literal
           | "TRUE"i          -> bool_literal
           | "FALSE"i         -> bool_literal

    OP: "==" | "=" | "!=" | "<>" | ">=" | "<=" | ">" | "<"
    ADDOP: "+" | "-" | "+"
    MULOP: "*" | "/" | "%"

    NAME: /[A-Za-z_À-ɏ][A-Za-z0-9_À-ɏ ]*/

    %import common.ESCAPED_STRING
    %import common.NUMBER
    %import common.WS
    %ignore WS
"""


class FormulaTransformer(Transformer):
    def start(self, items):
        return items[0]

    def binary_op(self, items):
        left, op, right = items[0], str(items[1]), items[2]
        return IRFormulaAST(node_type="BinaryOp", operator=op, children=[left, right])

    def unary_op(self, items):
        return IRFormulaAST(node_type="UnaryOp", operator="NOT", children=[items[0]])

    def unary_neg(self, items):
        return IRFormulaAST(node_type="UnaryOp", operator="-", children=[items[0]])

    def lod_expr(self, items):
        lod_type = str(items[0]).upper()
        dims_node = items[1] if len(items) > 2 else None
        expr = items[-1]
        dims = []
        if dims_node and hasattr(dims_node, "children"):
            dims = [str(t) for t in dims_node.children]
        return IRFormulaAST(
            node_type="LODExpression",
            lod_type=lod_type,
            lod_dimensions=dims,
            children=[expr],
        )

    def empty_fixed(self, items):
        return IRFormulaAST(
            node_type="LODExpression",
            lod_type="FIXED",
            lod_dimensions=[],
            children=[items[0]],
        )

    def dims_clause(self, items):
        return Tree("dims_clause", items)

    def if_expr(self, items):
        return IRFormulaAST(node_type="IfExpr", children=list(items))

    def case_expr(self, items):
        return IRFormulaAST(node_type="CaseExpr", children=list(items))

    def func_call(self, items):
        name = str(items[0]).upper()
        args = [i for i in items[1:] if isinstance(i, IRFormulaAST)]
        TABLE_CALCS = {
            "RUNNING_SUM", "RUNNING_AVG", "RUNNING_MAX", "RUNNING_MIN", "RUNNING_COUNT",
            "WINDOW_SUM", "WINDOW_AVG", "WINDOW_MAX", "WINDOW_MIN", "WINDOW_COUNT",
            "INDEX", "RANK", "FIRST", "LAST", "LOOKUP", "TOTAL", "SIZE", "PREVIOUS_VALUE"
        }
        AGGREGATES = {"SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "MEDIAN", "STDEV",
                      "STDEVP", "VAR", "VARP", "ATTR", "PERCENTILE"}
        if name in TABLE_CALCS:
            return IRFormulaAST(node_type="TableCalcCall", value=name, children=args)
        if name in AGGREGATES:
            return IRFormulaAST(node_type="AggregateCall", value=name, children=args)
        return IRFormulaAST(node_type="FunctionCall", value=name, children=args)

    def column_ref(self, items):
        name = ".".join(str(t) for t in items)
        return IRFormulaAST(node_type="ColumnRef", value=name)

    def string_literal(self, items):
        return IRFormulaAST(node_type="Literal", value=str(items[0]), datatype="string")

    def number_literal(self, items):
        v = str(items[0])
        dtype = "int" if "." not in v else "decimal"
        return IRFormulaAST(node_type="Literal", value=v, datatype=dtype)

    def null_literal(self, items):
        return IRFormulaAST(node_type="Literal", value="NULL")

    def bool_literal(self, items):
        return IRFormulaAST(node_type="Literal", value=str(items[0]).upper(), datatype="boolean")


_parser: Optional[Lark] = None


def _get_parser() -> Optional[Lark]:
    global _parser
    if not LARK_AVAILABLE:
        return None
    if _parser is None:
        _parser = Lark(TABLEAU_GRAMMAR, parser="earley", ambiguity="resolve")
    return _parser


def parse_formula(formula: str) -> IRFormulaAST:
    """Parse a Tableau formula string into an IRFormulaAST. Falls back to a simple wrapper on error."""
    if not formula or not formula.strip():
        return IRFormulaAST(node_type="Literal", value="")

    lark = _get_parser()
    if lark is None:
        return IRFormulaAST(node_type="FunctionCall", value="_UNPARSED", children=[
            IRFormulaAST(node_type="Literal", value=formula)
        ])

    try:
        tree = lark.parse(formula.strip())
        return FormulaTransformer().transform(tree)
    except Exception:
        return IRFormulaAST(node_type="FunctionCall", value="_UNPARSED", children=[
            IRFormulaAST(node_type="Literal", value=formula)
        ])


def ast_depth(node: IRFormulaAST) -> int:
    if not node.children:
        return 1
    return 1 + max(ast_depth(c) for c in node.children)


def has_lod(node: IRFormulaAST) -> bool:
    if node.node_type == "LODExpression":
        return True
    return any(has_lod(c) for c in node.children)


def has_table_calc(node: IRFormulaAST) -> bool:
    if node.node_type == "TableCalcCall":
        return True
    return any(has_table_calc(c) for c in node.children)


def collect_column_refs(node: IRFormulaAST) -> list[str]:
    refs = []
    if node.node_type == "ColumnRef" and node.value:
        refs.append(node.value)
    for c in node.children:
        refs.extend(collect_column_refs(c))
    return refs
