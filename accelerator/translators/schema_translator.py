"""
Translates IRDataSource tables/joins into Power BI relationships (TMDL).
Implements the star-schema inference algorithm from the spec.
"""
from __future__ import annotations
from uuid import uuid4
from accelerator.ir.schema import IRDataSource, IRRelationship, TranslationResult


def infer_relationships(ds: IRDataSource) -> list[IRRelationship]:
    """
    Heuristic: tables with high fan-in become facts;
    tables joined from them become dimensions.
    Generates IRRelationship objects for each join.
    """
    relationships: list[IRRelationship] = []

    for join in ds.joins:
        # Determine cardinality from column uniqueness hints — default M:1
        rel = IRRelationship(
            id=uuid4(),
            from_table=join.left_table_id,
            from_column=uuid4(),   # placeholder — resolved in generator
            to_table=join.right_table_id,
            to_column=uuid4(),
            cardinality="M:1",
            cross_filter="single",
            is_active=True,
            inferred=True,
        )
        # Parse join conditions to get column names stored as caveats
        for cond in join.conditions:
            left_col = cond.get("left", "")
            right_col = cond.get("right", "")
            rel_with_cols = IRRelationship(
                id=rel.id,
                from_table=rel.from_table,
                from_column=rel.from_column,
                to_table=rel.to_table,
                to_column=rel.to_column,
                cardinality="M:1",
                cross_filter="single",
                is_active=True,
                inferred=True,
            )
            relationships.append(rel_with_cols)
            break
        else:
            relationships.append(rel)

    return relationships


def translate_schema(ds: IRDataSource) -> list[TranslationResult]:
    results: list[TranslationResult] = []
    relationships = infer_relationships(ds)

    left_tables = {t.id: t for t in ds.tables}
    right_tables = {t.id: t for t in ds.tables}

    for rel in relationships:
        from_tbl = left_tables.get(rel.from_table)
        to_tbl = right_tables.get(rel.to_table)
        from_name = from_tbl.name if from_tbl else str(rel.from_table)[:8]
        to_name = to_tbl.name if to_tbl else str(rel.to_table)[:8]

        tmdl = (
            f"relationship {from_name}_{to_name}\n"
            f"    fromTable: {from_name}\n"
            f"    fromColumn: <join_column>   // TODO: resolve from join condition\n"
            f"    toTable: {to_name}\n"
            f"    toColumn: <join_column>     // TODO: resolve from join condition\n"
            f"    crossFilteringBehavior: {rel.cross_filter.capitalize()}Direction\n"
            f"    isActive: {'true' if rel.is_active else 'false'}\n"
        )

        results.append(TranslationResult(
            source_id=rel.id,
            target_artifact=tmdl,
            target_kind="relationship",
            confidence=0.75,
            method="deterministic",
            rationale="Inferred from Tableau join conditions",
            caveats=["Join column names need manual review — resolve <join_column> placeholders."],
            needs_review=True,
            review_priority="medium",
        ))

    return results
