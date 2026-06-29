"""Thin Postgres helpers (psycopg 3). Works against Supabase or local Postgres."""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row


# Disable server-side prepared statements: Supabase's transaction-mode pooler
# (PgBouncer, port 6543) does not support them, and psycopg3 would otherwise use
# them automatically. This keeps the pipeline/agent working through any Supabase
# pooler mode (or a direct connection) with negligible cost at this scale.
def connect(database_url: str) -> psycopg.Connection:
    """Open a connection. Caller manages the transaction / closing."""
    return psycopg.connect(database_url, prepare_threshold=None)


def connect_dict(database_url: str) -> psycopg.Connection:
    """Connection whose cursors return dict rows (used by the agent tools)."""
    return psycopg.connect(database_url, row_factory=dict_row, prepare_threshold=None)


def apply_schema(conn: psycopg.Connection, schema_path: str) -> None:
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
