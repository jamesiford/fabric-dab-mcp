"""Thin connection layer over the Fabric Lakehouse SQL analytics endpoint.

Auth is Entra-only: we mint an access token for the SQL resource and hand it to
ODBC via SQL_COPT_SS_ACCESS_TOKEN (1256). There is no SQL-auth path on Fabric.
"""

from __future__ import annotations

import os
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pyodbc
from azure.identity import AzureCliCredential, DefaultAzureCredential

SQL_COPT_SS_ACCESS_TOKEN = 1256
_TOKEN_SCOPE = "https://database.windows.net/.default"

_DRIVER_PREFERENCE = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
)


def _pick_driver() -> str:
    available = set(pyodbc.drivers())
    for candidate in _DRIVER_PREFERENCE:
        if candidate in available:
            return candidate
    raise RuntimeError(
        f"No supported SQL Server ODBC driver found. Installed: {sorted(available)}"
    )


def _token_struct(token: str) -> bytes:
    raw = token.encode("utf-16-le")
    return struct.pack("<i", len(raw)) + raw


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    elapsed_ms: float
    sql: str

    def to_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


@dataclass
class FabricSqlClient:
    """Connects to a Fabric SQL analytics endpoint and runs read-only queries.

    Connections are thread-local. The side-by-side app runs two lanes against
    this client concurrently, and a single shared pyodbc connection cannot
    service overlapping commands - it raises "Connection is busy with results
    for another command", which would penalise whichever lane arrived second.
    """

    server: str
    database: str
    timeout_s: int = 60
    use_cli_credential: bool = True
    _local: threading.local = field(default_factory=threading.local, init=False, repr=False)
    _cred: Any = field(default=None, init=False, repr=False)
    _cred_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def from_env(cls) -> "FabricSqlClient":
        server = os.environ.get("FABRIC_SQL_SERVER", "").strip()
        database = os.environ.get("FABRIC_SQL_DATABASE", "").strip()
        if not server or not database:
            raise RuntimeError(
                "FABRIC_SQL_SERVER and FABRIC_SQL_DATABASE must be set (see .env.example)"
            )
        return cls(
            server=server,
            database=database,
        )

    def _credential(self):
        with self._cred_lock:
            if self._cred is None:
                self._cred = (
                    AzureCliCredential() if self.use_cli_credential else DefaultAzureCredential()
                )
            return self._cred

    def connect(self) -> pyodbc.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        token = self._credential().get_token(_TOKEN_SCOPE).token
        conn_str = (
            f"Driver={{{_pick_driver()}}};"
            f"Server={self.server},1433;"
            f"Database={self.database};"
            "Encrypt=Yes;TrustServerCertificate=No;"
            f"Connection Timeout={self.timeout_s};"
        )
        conn = pyodbc.connect(
            conn_str,
            attrs_before={SQL_COPT_SS_ACCESS_TOKEN: _token_struct(token)},
            timeout=self.timeout_s,
        )
        self._local.conn = conn
        return conn

    def query(self, sql: str, params: tuple = ()) -> QueryResult:
        """Run a SELECT and return every row it produced, with wall-clock timing."""
        conn = self.connect()
        started = time.perf_counter()
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params) if params else cursor.execute(sql)
            columns = [c[0] for c in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
        finally:
            cursor.close()
        elapsed_ms = (time.perf_counter() - started) * 1000
        return QueryResult(
            columns=columns,
            rows=[tuple(r) for r in rows],
            elapsed_ms=elapsed_ms,
            sql=sql,
        )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
