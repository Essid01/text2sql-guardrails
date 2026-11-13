"""Typed, serializable description of a database schema."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ColumnInfo(BaseModel):
    """One column of a table."""

    name: str
    type: str
    nullable: bool
    is_primary_key: bool = False
    comment: str | None = None
    # Distinct values for low-cardinality text columns (e.g. order status), else None.
    sample_values: list[str] | None = None


class ForeignKeyInfo(BaseModel):
    """A foreign key: `table.columns` -> `referred_table.referred_columns`."""

    columns: list[str]
    referred_table: str
    referred_columns: list[str]


class TableInfo(BaseModel):
    """One table with its columns, keys and approximate size."""

    name: str
    comment: str | None = None
    columns: list[ColumnInfo]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list)
    row_count_estimate: int | None = None

    def column(self, name: str) -> ColumnInfo:
        """Return the column called `name` (KeyError if absent)."""
        for col in self.columns:
            if col.name == name:
                return col
        raise KeyError(f"{self.name}.{name}")


class DatabaseSchema(BaseModel):
    """All tables of the database."""

    tables: list[TableInfo]

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    def table(self, name: str) -> TableInfo:
        """Return the table called `name` (KeyError if absent)."""
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(name)

    def neighbors(self, name: str) -> set[str]:
        """Tables directly linked to `name` by a foreign key, in either direction."""
        linked = {fk.referred_table for fk in self.table(name).foreign_keys}
        linked |= {
            t.name for t in self.tables if any(fk.referred_table == name for fk in t.foreign_keys)
        }
        linked.discard(name)
        return linked

    def subset(self, names: set[str]) -> DatabaseSchema:
        """Schema restricted to `names`, keeping the original table order."""
        return DatabaseSchema(tables=[t for t in self.tables if t.name in names])
