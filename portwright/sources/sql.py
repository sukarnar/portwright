"""SQL adapter (any SQLAlchemy dialect: SQLite, PostgreSQL, MySQL, Oracle, MSSQL …).

Security properties:
* Values are ALWAYS bound parameters – never string-interpolated.
* Table/column identifiers come only from the spec and are verified by
  reflection, so callers can never influence identifiers.
* ``read_only`` sources reject write operations and non-SELECT statements and
  roll back every transaction. (Also use a read-only DB account – defence in depth.)
"""
from __future__ import annotations

import re
import threading
from typing import Any

import anyio
from sqlalchemy import MetaData, Table, create_engine, delete, insert, literal, select, text, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

from ..config import EndpointSpec
from ..credentials.types import build_sql_url
from .base import DataSource, ExecutionContext, NotFound, SourceError, register

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$.]{0,127}$")
_READ_SQL = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_WRITE_OPS = {"create", "update", "delete"}


def _rows(result) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in result]


@register("sql")
class SqlSource(DataSource):
    engine: Engine | None = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._tables: dict[str, Table] = {}
        self._default_schema: str | None = None
        self._cred_version: str | None = None
        self._rotate_lock = threading.Lock()

    def _build_engine(self) -> None:
        cred = self.credential()
        if cred is not None:
            url = build_sql_url(cred)                       # escapes special characters safely
            self._default_schema = cred.fields.get("schema") or None
            self._cred_version = cred.updated
        else:
            url = make_url(self.resolve(self.spec.url))
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if not url.drivername.startswith("sqlite"):
            kwargs["pool_size"] = self.spec.pool_size
        old, self.engine = self.engine, create_engine(url, **kwargs)
        self._tables.clear()
        if old is not None:
            old.dispose()                                    # drain connections using old secret

    async def startup(self) -> None:
        self._build_engine()

    def _refresh_if_rotated(self) -> None:
        """Zero-downtime rotation: if the credential changed, rebuild the pool."""
        if self.spec.credential:
            cred = self.credential()
            if cred.updated != self._cred_version:
                with self._rotate_lock:
                    if cred.updated != self._cred_version:
                        self._build_engine()

    async def shutdown(self) -> None:
        if self.engine:
            self.engine.dispose()

    async def health(self) -> bool:
        def _ping() -> bool:
            with self.engine.connect() as c:
                c.execute(select(literal(1)))  # dialect-neutral (Oracle needs FROM DUAL)
            return True
        try:
            return await anyio.to_thread.run_sync(_ping)
        except Exception:
            return False

    # ------------------------------------------------------------ validation
    def validate_endpoint(self, ep: EndpointSpec) -> None:
        op = ep.operation or ("query" if ep.query else None)
        if op is None:
            raise ValueError(f"[{ep.name}] sql endpoint needs 'query' or 'operation' + 'table'")
        if op == "query":
            if not ep.query:
                raise ValueError(f"[{ep.name}] operation 'query' needs a 'query'")
            if ";" in ep.query.strip().rstrip(";"):
                raise ValueError(f"[{ep.name}] multiple SQL statements are not allowed")
            bound = set(re.findall(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", ep.query))
            declared = {p.name for p in ep.params}
            if bound - declared:
                raise ValueError(f"[{ep.name}] query binds undeclared params {sorted(bound - declared)}")
            if self.spec.read_only and not _READ_SQL.match(ep.query):
                raise ValueError(f"[{ep.name}] source '{self.name}' is read-only; only SELECT/WITH allowed")
        else:
            if not ep.table or not _IDENT.match(ep.table):
                raise ValueError(f"[{ep.name}] invalid or missing table name")
            for c in ep.columns or []:
                if not _IDENT.match(c):
                    raise ValueError(f"[{ep.name}] invalid column name '{c}'")
            if op in {"get", "update", "delete"} and not ep.key:
                raise ValueError(f"[{ep.name}] operation '{op}' needs 'key'")
            if op in _WRITE_OPS and self.spec.read_only:
                raise ValueError(f"[{ep.name}] source '{self.name}' is read-only; '{op}' not allowed")

    # ---------------------------------------------------------------- helpers
    def _table(self, name: str) -> Table:
        if name not in self._tables:
            schema, _, tbl = name.rpartition(".")
            try:
                self._tables[name] = Table(tbl, MetaData(), schema=schema or self._default_schema,
                                           autoload_with=self.engine)
            except NoSuchTableError:
                raise SourceError(f"Configured table '{name}' does not exist", 500) from None
        return self._tables[name]

    @staticmethod
    def _allowed_cols(table: Table, ep: EndpointSpec) -> list:
        names = ep.columns or [c.name for c in table.columns]
        missing = [n for n in names if n not in table.c]
        if missing:
            raise SourceError(f"Configured columns {missing} not in table", 500)
        return [table.c[n] for n in names]

    # ---------------------------------------------------------------- execute
    async def execute(self, ctx: ExecutionContext) -> Any:
        return await anyio.to_thread.run_sync(self._execute_sync, ctx)

    def _execute_sync(self, ctx: ExecutionContext) -> Any:
        self._refresh_if_rotated()
        ep = ctx.endpoint
        op = ep.operation or "query"
        try:
            if op == "query":
                return self._run_query(ctx)
            return self._run_table_op(op, ctx)
        except SourceError:
            raise
        except SQLAlchemyError as exc:
            # Log details server-side; return a generic message to the caller.
            raise SourceError("Database operation failed", 502) from exc

    def _run_query(self, ctx: ExecutionContext) -> Any:
        ep = ctx.endpoint
        binds = {p.name: ctx.params.get(p.name) for p in ep.params}
        is_read = bool(_READ_SQL.match(ep.query))
        with self.engine.connect() as conn:
            if is_read:
                result = conn.execution_options(stream_results=True).execute(text(ep.query), binds)
                # dialect-neutral pagination
                if ctx.offset:
                    result.fetchmany(ctx.offset)
                rows = [dict(r._mapping) for r in result.fetchmany(ctx.limit)]
                result.close()
                conn.rollback()
                return {"items": rows, "limit": ctx.limit, "offset": ctx.offset, "count": len(rows)}
            if self.spec.read_only:
                raise SourceError("Write statements are not allowed on a read-only source", 403)
            result = conn.execute(text(ep.query), binds)
            conn.commit()
            return {"affected": result.rowcount}

    def _run_table_op(self, op: str, ctx: ExecutionContext) -> Any:
        ep = ctx.endpoint
        if op in _WRITE_OPS and self.spec.read_only:
            raise SourceError("Write operations are not allowed on a read-only source", 403)
        table = self._table(ep.table)
        cols = self._allowed_cols(table, ep)
        allowed = {c.name for c in cols}
        p = ctx.params
        with self.engine.connect() as conn:
            if op == "list":
                stmt = select(*cols)
                for name, value in p.items():
                    if name in allowed and value is not None:
                        stmt = stmt.where(table.c[name] == value)
                if ep.key:
                    stmt = stmt.order_by(table.c[ep.key])
                stmt = stmt.limit(ctx.limit).offset(ctx.offset)
                rows = _rows(conn.execute(stmt))
                conn.rollback()
                return {"items": rows, "limit": ctx.limit, "offset": ctx.offset, "count": len(rows)}

            key_col = table.c[ep.key] if ep.key else None
            if op == "get":
                row = conn.execute(select(*cols).where(key_col == p[ep.key])).first()
                conn.rollback()
                if row is None:
                    raise NotFound()
                return dict(row._mapping)

            values = {k: v for k, v in p.items() if k in allowed and k != ep.key and v is not None}
            if op == "create":
                if ep.key and p.get(ep.key) is not None:
                    values[ep.key] = p[ep.key]
                if not values:
                    raise SourceError("No writable fields supplied")
                res = conn.execute(insert(table).values(**values))
                conn.commit()
                pk = list(res.inserted_primary_key or [])
                return {"created": True, "key": pk[0] if len(pk) == 1 else pk}
            if op == "update":
                if not values:
                    raise SourceError("No writable fields supplied")
                res = conn.execute(update(table).where(key_col == p[ep.key]).values(**values))
                conn.commit()
                if res.rowcount == 0:
                    raise NotFound()
                return {"updated": res.rowcount}
            if op == "delete":
                res = conn.execute(delete(table).where(key_col == p[ep.key]))
                conn.commit()
                if res.rowcount == 0:
                    raise NotFound()
                return {"deleted": res.rowcount}
        raise SourceError(f"Unsupported operation '{op}'", 500)
