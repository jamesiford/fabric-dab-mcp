"""Fabric-native row-level security keyed on SESSION_CONTEXT.

This is the experiment that decides whether per-user data security is achievable
at the Fabric layer for a Lakehouse SQL analytics endpoint. It is undocumented:
Microsoft documents `set-session-context` for the SQL Server / Azure SQL family
only, and the Lakehouse SQL analytics endpoint is in the `dwsql` family. Fabric
*does* document RLS via CREATE SECURITY POLICY for the endpoint, but its examples
use the connected principal, not session context.

What this proves or disproves:
  1. sp_set_session_context / SESSION_CONTEXT() execute on the endpoint
  2. an inline TVF predicate can read session context
  3. CREATE SECURITY POLICY attaches to a Lakehouse Delta table
  4. setting session context actually changes which rows come back

Predicate design note - fail-open vs fail-closed:
  The predicate below is permissive when no session context is set, so an
  unattributed service connection sees the whole book. That is what makes the
  existing demo keep working, and it mirrors how a service identity is usually
  treated. It is NOT what a bank should ship. The fail-closed variant is included
  and can be applied with --strict: no session context means no rows, and a
  service identity must present an explicit 'svc' context to see everything.

    python setup_rls.py --enable      # permissive predicate (default)
    python setup_rls.py --enable --strict
    python setup_rls.py --test
    python setup_rls.py --disable
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from fabric_sql import FabricSqlClient  # noqa: E402

POLICY = "dbo.rls_rm_book"
PREDICATE = "dbo.fn_rm_book_predicate"
TARGET = "dbo.vw_deposits"

# The claim key DAB actually writes into SESSION_CONTEXT. Verified empirically
# against Fabric's queryinsights.exec_requests_history - DAB emits the full
# WS-Federation claim URI, not a short name like 'roles'.
ROLE_CLAIM = "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"

PREDICATE_PERMISSIVE = f"""
CREATE FUNCTION {PREDICATE}(@rm AS VARCHAR(100))
RETURNS TABLE
WITH SCHEMABINDING
AS RETURN
    SELECT 1 AS is_visible
    WHERE CAST(SESSION_CONTEXT(N'rm') AS VARCHAR(100)) IS NULL
       OR CAST(SESSION_CONTEXT(N'rm') AS VARCHAR(100)) = @rm
"""

PREDICATE_STRICT = f"""
CREATE FUNCTION {PREDICATE}(@rm AS VARCHAR(100))
RETURNS TABLE
WITH SCHEMABINDING
AS RETURN
    SELECT 1 AS is_visible
    WHERE CAST(SESSION_CONTEXT(N'rm') AS VARCHAR(100)) = @rm
       OR CAST(SESSION_CONTEXT(N'svc') AS VARCHAR(10)) = 'all'
"""

# The production-shaped predicate: reads the role claim DAB propagates from the
# validated token, so the filter is driven by the caller's identity rather than
# by anything the caller can set. 'svc-all' is an explicit break-glass role for
# unattended service access; without it an unauthenticated caller sees nothing.
PREDICATE_CLAIM = f"""
CREATE FUNCTION {PREDICATE}(@rm AS VARCHAR(100))
RETURNS TABLE
WITH SCHEMABINDING
AS RETURN
    SELECT 1 AS is_visible
    WHERE CAST(SESSION_CONTEXT(N'{ROLE_CLAIM}') AS VARCHAR(200)) = @rm
       OR CAST(SESSION_CONTEXT(N'{ROLE_CLAIM}') AS VARCHAR(200)) = 'svc-all'
"""


def drop_all(cursor, conn) -> None:
    for stmt in (
        f"DROP SECURITY POLICY IF EXISTS {POLICY}",
        f"DROP FUNCTION IF EXISTS {PREDICATE}",
    ):
        try:
            cursor.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            print(f"  (ignored) {stmt}: {str(exc)[:110]}")


def enable(cursor, conn, strict: bool, claim: bool = False) -> None:
    drop_all(cursor, conn)
    if claim:
        body, label = PREDICATE_CLAIM, "claim-driven"
    elif strict:
        body, label = PREDICATE_STRICT, "strict"
    else:
        body, label = PREDICATE_PERMISSIVE, "permissive"
    cursor.execute(body)
    print(f"  created predicate {PREDICATE} ({label})")

    cursor.execute(
        f"CREATE SECURITY POLICY {POLICY} "
        f"ADD FILTER PREDICATE {PREDICATE}(relationship_manager) ON {TARGET} "
        "WITH (STATE = ON)"
    )
    print(f"  created security policy {POLICY} on {TARGET}")


def test(cursor) -> None:
    def count(label: str) -> int:
        cursor.execute(f"SELECT COUNT(*) FROM {TARGET}")
        n = cursor.fetchone()[0]
        print(f"  {label:44s} {n:>7,} rows")
        return n

    print("\n--- with no session context ---")
    cursor.execute("EXEC sp_set_session_context 'rm', NULL")
    cursor.execute("EXEC sp_set_session_context 'svc', NULL")
    baseline = count("no context")

    cursor.execute(f"SELECT TOP 1 relationship_manager FROM {TARGET}")
    row = cursor.fetchone()
    rm = row[0] if row else None

    if rm:
        print(f"\n--- with session context rm = {rm!r} ---")
        cursor.execute("EXEC sp_set_session_context 'rm', ?", (rm,))
        scoped = count(f"scoped to {rm}")

        cursor.execute("EXEC sp_set_session_context 'rm', ?", ("Nobody At All",))
        none_ = count("scoped to a non-existent RM")

        cursor.execute("EXEC sp_set_session_context 'rm', NULL")
        cursor.execute("EXEC sp_set_session_context 'svc', 'all'")
        svc = count("service context svc='all'")

        print()
        if scoped < baseline and none_ == 0:
            print("  RESULT: Fabric-native RLS via SESSION_CONTEXT WORKS on this endpoint.")
            print(f"          unfiltered {baseline:,} -> scoped {scoped:,} -> unknown RM {none_}")
        elif scoped == baseline:
            print("  RESULT: predicate did NOT filter - session context is not reaching it.")
        else:
            print(f"  RESULT: inconclusive (baseline {baseline}, scoped {scoped}, none {none_}, svc {svc})")

    cursor.execute("EXEC sp_set_session_context 'rm', NULL")
    cursor.execute("EXEC sp_set_session_context 'svc', NULL")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enable", action="store_true")
    ap.add_argument("--disable", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="fail-closed predicate: no context means no rows")
    ap.add_argument("--claim", action="store_true",
                    help="production-shaped: filter on the role claim DAB propagates")
    args = ap.parse_args()

    client = FabricSqlClient.from_env()
    conn = client.connect()
    # Fabric rejects DDL inside the implicit snapshot-isolation transaction that
    # pyodbc opens by default: "this DDL statement is not allowed inside a
    # snapshot isolation transaction". Autocommit avoids the implicit txn.
    conn.autocommit = True
    cursor = conn.cursor()
    try:
        if args.disable:
            drop_all(cursor, conn)
            print("  policy and predicate removed")
        if args.enable:
            enable(cursor, conn, args.strict, args.claim)
        if args.test or (args.enable and not args.claim):
            test(cursor)
        if args.enable and args.claim:
            print("\n  claim-driven policy is live.")
            print("  Rows are now filtered by the role claim DAB writes into")
            print("  SESSION_CONTEXT, so test it through DAB, not from here.")
    finally:
        cursor.close()
        client.close()


if __name__ == "__main__":
    main()
