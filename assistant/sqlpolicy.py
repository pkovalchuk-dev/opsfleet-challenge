"""SQL policy: parse the model's proposal, reject anything outside the allowed shape,
and rewrite every table reference into a brand scoped subquery."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.optimizer.scope import traverse_scope

DATASET = "bigquery-public-data.thelook_ecommerce"
CATALOG, DB = DATASET.split(".")

ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "products": ("id", "cost", "category", "name", "brand", "retail_price", "department", "sku", "distribution_center_id"),
    "order_items": ("id", "order_id", "user_id", "product_id", "inventory_item_id", "status", "created_at",
                    "shipped_at", "delivered_at", "returned_at", "sale_price"),
    "orders": ("order_id", "user_id", "status", "gender", "created_at", "returned_at", "shipped_at",
               "delivered_at", "num_of_item"),
    "users": ("id", "age", "gender", "state", "city", "country", "traffic_source", "created_at"),
}
FORBIDDEN_COLUMNS = frozenset({"first_name", "last_name", "email", "street_address", "postal_code",
                               "latitude", "longitude", "user_geom"})
DENIED_FUNCTION_PREFIXES = ("EXTERNAL", "SESSION_USER", "ML.", "ML_", "AI.", "AI_", "NET.", "NET_", "KEYS.", "AEAD.")
BRANDS_PARAM = "brands"
CUTOFF_PARAM = "analysis_cutoff"


class PolicyViolation(Exception):
    """The proposed SQL is outside what the assistant may run. Not repairable by the model."""


class RepairableSqlError(Exception):
    """The SQL is malformed or names a column that does not exist. The model may fix it."""


@dataclass
class ScopedQuery:
    sql: str
    brands: tuple[str, ...] | None  # None for all-brand access (CEO)
    tables: list[str] = field(default_factory=list)
    data_cutoff: dt.datetime | None = None

    @property
    def parameters(self) -> list:
        """BigQuery query parameters for the scoped SQL."""
        from google.cloud import bigquery

        parameters = []
        if self.brands is not None:
            parameters.append(bigquery.ArrayQueryParameter(BRANDS_PARAM, "STRING", list(self.brands)))
        if self.data_cutoff is not None:
            parameters.append(bigquery.ScalarQueryParameter(CUTOFF_PARAM, "TIMESTAMP", self.data_cutoff))
        return parameters


def _qualified(name: str) -> str:
    return f"`{DATASET}.{name}`"


def _scoped_source(table: str, scoped: bool) -> str:
    cols = ", ".join(ALLOWED_COLUMNS[table])
    base = f"SELECT {cols} FROM {_qualified(table)}"
    if not scoped:
        return base
    products_in_scope = f"SELECT id FROM {_qualified('products')} WHERE brand IN UNNEST(@{BRANDS_PARAM})"
    if table == "products":
        return f"{base} WHERE brand IN UNNEST(@{BRANDS_PARAM})"
    if table == "order_items":
        return f"{base} WHERE product_id IN ({products_in_scope})"
    if table == "orders":
        # A mixed-brand basket must expose only the authorized item count.
        order_cols = ", ".join(f"o.{c}" for c in ALLOWED_COLUMNS[table] if c != "num_of_item")
        counts = (f"SELECT order_id, COUNT(*) AS num_of_item FROM {_qualified('order_items')} "
                  f"WHERE product_id IN ({products_in_scope}) GROUP BY order_id")
        return (f"SELECT {order_cols}, scoped_items.num_of_item FROM {_qualified('orders')} o "
                f"JOIN ({counts}) scoped_items ON scoped_items.order_id = o.order_id")
    users_in_scope = f"SELECT user_id FROM {_qualified('order_items')} WHERE product_id IN ({products_in_scope})"
    return f"{base} WHERE id IN ({users_in_scope})"  # users


def _check_statement(sql: str) -> exp.Select:
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except (ParseError, TokenError) as exc:
        raise RepairableSqlError(f"could not parse SQL: {str(exc)[:160]}") from exc
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise PolicyViolation("exactly one statement is allowed")
    tree = statements[0]
    set_op = getattr(exp, "SetOperation", exp.Union)
    if isinstance(tree, set_op) or list(tree.find_all(set_op)):
        raise PolicyViolation("UNION / INTERSECT / EXCEPT are not allowed")
    if not isinstance(tree, exp.Select):
        raise PolicyViolation(f"only SELECT statements are allowed, got {type(tree).__name__}")
    if list(tree.find_all(exp.With)):
        raise PolicyViolation("CTEs (WITH) are not allowed; use a nested SELECT instead")
    return tree


def _check_tables(tree: exp.Select) -> list[str]:
    tables: list[str] = []
    for table in tree.find_all(exp.Table):
        name = table.name
        if name not in ALLOWED_COLUMNS:
            raise PolicyViolation(f"table '{name}' is not available; use orders, order_items, products or users")
        if (table.catalog or table.db) and (table.catalog, table.db) not in ((CATALOG, DB), ("", DB)):
            raise PolicyViolation(f"table '{table.sql()}' is outside the allowed dataset")
        tables.append(name)
    if not tables:
        raise PolicyViolation("the query must read from at least one table")
    return sorted(set(tables))


def _check_columns(tree: exp.Select) -> None:
    for star in tree.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            raise PolicyViolation("SELECT * is not allowed; list the columns you need")
    for column in tree.find_all(exp.Column):
        name = column.name.lower()
        if name in FORBIDDEN_COLUMNS:
            raise PolicyViolation(f"column '{column.name}' is personal data and is not available")
    # Resolve aliases per SELECT. Correlated columns also appear in their parent scope.
    for scope in traverse_scope(tree):
        for column in scope.columns:
            source = scope.sources.get(column.table)
            if isinstance(source, exp.Table) and column.name.lower() not in ALLOWED_COLUMNS[source.name]:
                raise RepairableSqlError(f"column '{column.table}.{column.name}' does not exist on {source.name}")


def _check_functions(tree: exp.Select, *, allow_cutoff: bool = False) -> None:
    for node in tree.find_all(exp.Func):
        # SQLGlot has dedicated AST types for SESSION_USER, AI.GENERATE and NET.*;
        # checking Anonymous nodes alone misses these functions.
        name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper()
        if name.startswith(DENIED_FUNCTION_PREFIXES) or "." in name:
            raise PolicyViolation(f"function '{name}' is not allowed")
    for node in tree.find_all(exp.Dot):
        if isinstance(node.expression, exp.Func):
            raise PolicyViolation("qualified functions and user-defined functions are not allowed")
    if list(tree.find_all(exp.Unnest)):
        raise PolicyViolation("UNNEST is not allowed in your query")
    for parameter in tree.find_all(exp.Parameter, exp.Placeholder, exp.SessionParameter):
        if allow_cutoff and type(parameter) is exp.Parameter and parameter.name == CUTOFF_PARAM:
            continue
        raise PolicyViolation("only the supplied @analysis_cutoff parameter is allowed; session variables are not")


def _apply_cutoff(tree: exp.Select) -> exp.Select:
    """Use one fixed clock throughout the action; metric correctness is checked separately."""
    def freeze(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.CurrentTimestamp) and not node.args:
            return exp.Parameter(this=exp.Var(this=CUTOFF_PARAM))
        if isinstance(node, exp.CurrentDate) and (not node.args or (
                isinstance(node.this, exp.Literal) and node.this.this.upper() == "UTC")):
            return exp.Date(this=exp.Parameter(this=exp.Var(this=CUTOFF_PARAM)))
        return node

    return tree.transform(freeze)


def validate_and_scope(sql: str, *, role: str, brands: tuple[str, ...] | list[str],
                       analysis_cutoff: dt.datetime | None = None) -> ScopedQuery:
    """Validate the model's SQL and return the scoped query without adding a SQL row limit.

    Raises PolicyViolation for anything that must not run; those are never sent
    back to the model. Raises RepairableSqlError for malformed SQL or unknown
    columns, which the model may correct within the repair limit."""
    tree = _check_statement(sql)
    tables = _check_tables(tree)
    _check_columns(tree)
    _check_functions(tree, allow_cutoff=analysis_cutoff is not None)
    if analysis_cutoff is not None:
        if analysis_cutoff.tzinfo is None or analysis_cutoff.utcoffset() is None:
            raise ValueError("analysis_cutoff must include a timezone")
        analysis_cutoff = analysis_cutoff.astimezone(dt.timezone.utc)
        tree = _apply_cutoff(tree)

    scoped = role != "ceo"
    if scoped and not brands:
        raise PolicyViolation("no brands are assigned to this user")

    def rewrite(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table) and node.name in ALLOWED_COLUMNS:
            inner = sqlglot.parse_one(_scoped_source(node.name, scoped), read="bigquery")
            alias = exp.TableAlias(this=exp.to_identifier(node.alias or node.name))
            return exp.Subquery(this=inner, alias=alias)
        return node

    tree = tree.transform(rewrite)
    return ScopedQuery(sql=tree.sql(dialect="bigquery"), brands=None if not scoped else tuple(brands),
                       tables=tables, data_cutoff=analysis_cutoff)
