"""Load strategies (Strategy pattern): how mapped rows are written to the target table.

All writers run inside ONE transaction opened by the runner, so a failure
anywhere rolls the whole load back. Values are always bound parameters;
identifiers come from the reflected table and are quoted by the dialect.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Table, and_, delete, insert, text, update
from sqlalchemy.engine import Connection


class Writer:
    def __init__(self, conn: Connection, table: Table, keys: list[str]):
        self.conn, self.table, self.keys = conn, table, keys
        self.counts = {"inserted": 0, "updated": 0, "upserted": 0, "deleted": 0}

    def start(self) -> None:
        pass

    def write(self, rows: list[dict[str, Any]]) -> None:
        raise NotImplementedError


class InsertWriter(Writer):
    def write(self, rows):
        self.conn.execute(insert(self.table), rows)        # executemany: one round trip per batch
        self.counts["inserted"] += len(rows)


class ReplaceWriter(InsertWriter):
    """DELETE + INSERT in the same transaction (not TRUNCATE, which auto-commits on Oracle),
    so readers never see an empty table and a failure restores the old rows."""

    def start(self):
        res = self.conn.execute(delete(self.table))
        self.counts["deleted"] = max(res.rowcount or 0, 0)


class GenericUpsertWriter(Writer):
    """Portable fallback: UPDATE by key, INSERT when no row matched."""

    def write(self, rows):
        t = self.table
        for r in rows:
            cond = and_(*[t.c[k] == r[k] for k in self.keys])
            vals = {k: v for k, v in r.items() if k not in self.keys}
            if vals:
                if self.conn.execute(update(t).where(cond).values(**vals)).rowcount:
                    self.counts["updated"] += 1
                    continue
            elif self.conn.execute(t.select().where(cond)).first() is not None:
                continue                                    # key-only row already present
            self.conn.execute(insert(t).values(**r))
            self.counts["inserted"] += 1


class OnConflictUpsertWriter(Writer):
    """PostgreSQL / SQLite: INSERT … ON CONFLICT (keys) DO UPDATE (needs a PK/unique on keys)."""

    def write(self, rows):
        name = self.conn.dialect.name
        if name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        stmt = dialect_insert(self.table)
        others = [c for c in rows[0] if c not in self.keys]
        stmt = (stmt.on_conflict_do_update(index_elements=self.keys,
                                           set_={c: stmt.excluded[c] for c in others})
                if others else stmt.on_conflict_do_nothing(index_elements=self.keys))
        self.conn.execute(stmt, rows)
        self.counts["upserted"] += len(rows)


class MySqlUpsertWriter(Writer):
    def write(self, rows):
        from sqlalchemy.dialects.mysql import insert as mysql_insert
        stmt = mysql_insert(self.table)
        others = [c for c in rows[0] if c not in self.keys] or self.keys[:1]
        stmt = stmt.on_duplicate_key_update({c: stmt.inserted[c] for c in others})
        self.conn.execute(stmt, rows)
        self.counts["upserted"] += len(rows)


class MergeUpsertWriter(Writer):
    """Oracle and SQL Server: MERGE with positional binds (:p0, :p1 …), executed as executemany."""

    def _statement(self, cols: list[str]) -> str:
        prep = self.conn.dialect.identifier_preparer
        q = prep.quote
        tbl = prep.format_table(self.table)
        oracle = self.conn.dialect.name == "oracle"
        select_list = ", ".join(f":p{i} AS {q(c)}" for i, c in enumerate(cols))
        src = f"(SELECT {select_list} FROM dual)" if oracle else f"(SELECT {select_list})"
        on = " AND ".join(f"t.{q(k)} = s.{q(k)}" for k in self.keys)
        others = [c for c in cols if c not in self.keys]
        matched = (f" WHEN MATCHED THEN UPDATE SET " + ", ".join(f"t.{q(c)} = s.{q(c)}" for c in others)
                   if others else "")
        ins_cols = ", ".join(q(c) for c in cols)
        ins_vals = ", ".join(f"s.{q(c)}" for c in cols)
        alias = "t" if oracle else "AS t"
        s_alias = "s" if oracle else "AS s"
        sql = (f"MERGE INTO {tbl} {alias} USING {src} {s_alias} ON ({on}){matched} "
               f"WHEN NOT MATCHED THEN INSERT ({ins_cols}) VALUES ({ins_vals})")
        return sql if oracle else sql + ";"

    def write(self, rows):
        cols = list(rows[0])
        stmt = text(self._statement(cols))
        self.conn.execute(stmt, [{f"p{i}": r[c] for i, c in enumerate(cols)} for r in rows])
        self.counts["upserted"] += len(rows)


def make_writer(mode: str, conn: Connection, table: Table, keys: list[str]) -> Writer:
    if mode == "insert":
        return InsertWriter(conn, table, keys)
    if mode == "replace":
        return ReplaceWriter(conn, table, keys)
    dialect = conn.dialect.name
    if dialect in ("postgresql", "sqlite"):
        return OnConflictUpsertWriter(conn, table, keys)
    if dialect in ("mysql", "mariadb"):
        return MySqlUpsertWriter(conn, table, keys)
    if dialect in ("oracle", "mssql"):
        return MergeUpsertWriter(conn, table, keys)
    return GenericUpsertWriter(conn, table, keys)
