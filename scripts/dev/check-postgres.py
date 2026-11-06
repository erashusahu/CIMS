"""Verify the configured CIMS connection using a read-only transaction."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.db.base import engine
from app.db.readiness import check_database_readiness
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


async def main(*, readiness: bool = False) -> int:
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            result = await connection.execute(
                text("SELECT current_database(), current_user")
            )
            database, user = result.one()
            print(f"Connected to {database} as {user}")
            schemas = await connection.execute(
                text(
                    "SELECT schema_name FROM information_schema.schemata "
                    "ORDER BY schema_name"
                )
            )
            print("Schemas: " + ", ".join(schemas.scalars()))
            tables = await connection.execute(
                text(
                    "SELECT schemaname, count(*) FROM pg_tables "
                    "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
                    "GROUP BY schemaname ORDER BY schemaname"
                )
            )
            for schema, count in tables:
                print(f"Tables in {schema}: {count}")
            print("Read-only verification passed; no database changes made.")
        if readiness:
            async with asyncio.timeout(5):
                report = await check_database_readiness(engine)
            if not report.ready:
                print("Application readiness: BLOCKED")
                for issue in report.issues:
                    print(f"- {issue.code}: {issue.message}")
                return 1
            print(f"Application readiness: READY (migration {report.migration})")
        return 0
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        # Exception messages can contain credentials or SQL parameter values.
        print(
            f"Database verification failed ({type(exc).__name__}). Check local configuration."
        )
        return 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--readiness",
        action="store_true",
        help="Also check foundation schema and runtime role",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(readiness=args.readiness)))
