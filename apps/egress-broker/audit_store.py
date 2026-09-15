"""Concurrent, durable PostgreSQL storage for the egress-audit plane.

The broker writes (writer role); the audit-api reads (reader role). Callers never
hold DB work over upstream I/O — every method is one short transaction.
"""
import json
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class AuditStore:
    def __init__(self, dsn):
        if not dsn:
            raise RuntimeError("EGRESS_AUDIT_DATABASE_URL must be configured")
        self.pool = ConnectionPool(
            conninfo=dsn, min_size=1, max_size=8, kwargs={"row_factory": dict_row}
        )

    @contextmanager
    def _connection(self):
        with self.pool.connection() as connection:
            with connection.transaction():
                yield connection

    def health(self):
        with self._connection() as connection:
            connection.execute("SELECT 1")

    def begin_egress(self, tenant_id, request_id, key_id, target_host,
                     capability_id, method, path, headers):
        """Durably claim a request before egress; state = pending."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO egress_requests
                    (id, tenant_id, key_id, target_host, capability_id,
                     request_method, request_path, request_headers, state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'pending')
                """,
                (request_id, tenant_id, key_id, target_host, capability_id,
                 method, path, json.dumps(headers or {})),
            )

    def complete_egress(self, request_id, upstream_status, response_bytes, duration_ms):
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE egress_requests SET
                    state = 'completed', upstream_status = %s, response_bytes = %s,
                    duration_ms = %s, completed_at = now()
                WHERE id = %s AND state = 'pending'
                """,
                (upstream_status, response_bytes, duration_ms, request_id),
            )

    def fail_egress(self, request_id, reason, duration_ms):
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE egress_requests SET
                    state = 'failed', failure_reason = %s, duration_ms = %s,
                    completed_at = now()
                WHERE id = %s AND state = 'pending'
                """,
                (reason, duration_ms, request_id),
            )

    def deny_egress(self, tenant_id, request_id, key_id, target_host, reason, method, path):
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO egress_requests
                    (id, tenant_id, key_id, target_host, request_method, request_path,
                     state, failure_reason, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'denied', %s, now())
                """,
                (request_id, tenant_id, key_id, target_host, method, path, reason),
            )

    def events(self, limit):
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, tenant_id, key_id, target_host, capability_id, request_method,
                       request_path, state, upstream_status, response_bytes, duration_ms,
                       failure_reason, created_at, completed_at
                FROM egress_requests
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            row = dict(row)
            for key in ("created_at", "completed_at"):
                if row.get(key) is not None:
                    row[key] = row[key].isoformat()
            out.append(row)
        return out
