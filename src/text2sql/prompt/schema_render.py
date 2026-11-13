"""Render a schema as compact, commented DDL for the LLM.

Why DDL rather than JSON: models have seen huge amounts of CREATE TABLE statements,
so types, keys and REFERENCES are read reliably, and it is token-efficient.
"""

from __future__ import annotations

from text2sql.schema.models import DatabaseSchema, TableInfo


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_table(table: TableInfo) -> str:
    """One CREATE TABLE statement with comments, FKs and categorical values inline."""
    single_fk = {fk.columns[0]: fk for fk in table.foreign_keys if len(fk.columns) == 1}
    lines: list[str] = []
    for col in table.columns:
        line = f"  {col.name} {col.type}"
        if col.is_primary_key and len(table.primary_key) == 1:
            line += " PRIMARY KEY"
        elif not col.nullable:
            line += " NOT NULL"
        if fk := single_fk.get(col.name):
            line += f" REFERENCES {fk.referred_table}({fk.referred_columns[0]})"
        notes = []
        if col.comment:
            notes.append(col.comment)
        if col.sample_values:
            notes.append("Values: " + ", ".join(_quote(v) for v in col.sample_values))
        lines.append((line, " ".join(notes)))

    body = [f"{text}," + (f" -- {note}" if note else "") for text, note in lines]
    if len(table.primary_key) > 1:
        body.append(f"  PRIMARY KEY ({', '.join(table.primary_key)}),")
    for fk in table.foreign_keys:
        if len(fk.columns) > 1:
            body.append(
                f"  FOREIGN KEY ({', '.join(fk.columns)}) REFERENCES "
                f"{fk.referred_table}({', '.join(fk.referred_columns)}),"
            )
    # Drop the comma after the last definition (it sits before any "--" comment).
    last = body[-1]
    head, sep, tail = last.partition(" --")
    body[-1] = head.removesuffix(",") + sep + tail

    header = []
    if table.comment:
        header.append(f"-- {table.comment}")
    if table.row_count_estimate is not None:
        header.append(f"-- ~{table.row_count_estimate:,} rows")
    return "\n".join([*header, f"CREATE TABLE {table.name} (", *body, ");"])


def render_schema(schema: DatabaseSchema) -> str:
    """All tables, separated by blank lines."""
    return "\n\n".join(render_table(t) for t in schema.tables)
