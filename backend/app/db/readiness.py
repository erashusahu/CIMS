"""Read-only checks for the foundation schema and the runtime database role."""

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.base import Base
from app.models import entities  # noqa: F401 - register foundation metadata


@dataclass(frozen=True)
class ReadinessIssue:
    code: str
    message: str


@dataclass
class ReadinessReport:
    migration: str | None = None
    issues: list[ReadinessIssue] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.issues


@lru_cache
def expected_revision() -> str:
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "alembic"))
    return scripts.get_current_head()


def inspect_foundation(connection: Connection) -> tuple[list[str], list[str], bool]:
    inspector = inspect(connection)
    present = set(inspector.get_table_names())
    required = {table.name: set(table.columns.keys()) for table in Base.metadata.tables.values()}
    # Refresh-token history is used by auth.py through SQL rather than an ORM model.
    required["auth_refresh_tokens"] = {"id", "session_id", "token_hash", "used_at"}
    missing = sorted(set(required) - present)
    incompatible = []
    for name in sorted(set(required) & present):
        columns = {column["name"] for column in inspector.get_columns(name)}
        if not required[name].issubset(columns):
            incompatible.append(name)
    return missing, incompatible, "alembic_version" in present


async def check_database_readiness(database_engine: AsyncEngine) -> ReadinessReport:
    report = ReadinessReport()
    async with database_engine.connect() as connection, connection.begin():
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        role = (
            await connection.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        if role.rolsuper or role.rolbypassrls:
            report.issues.append(
                ReadinessIssue(
                    "runtime_role_unsafe",
                    "The runtime login must have NOSUPERUSER and NOBYPASSRLS.",
                )
            )

        missing, incompatible, has_migrations = await connection.run_sync(inspect_foundation)
        if missing:
            report.issues.append(
                ReadinessIssue("foundation_tables_missing", "Missing tables: " + ", ".join(missing))
            )
        if incompatible:
            report.issues.append(
                ReadinessIssue(
                    "foundation_columns_missing", "Incompatible tables: " + ", ".join(incompatible)
                )
            )

        owned = await connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_class c "
                "WHERE c.oid IN (SELECT to_regclass(name) "
                "FROM unnest(CAST(:names AS text[])) AS required(name)) "
                "AND pg_has_role(c.relowner, 'USAGE'))"
            ),
            {
                "names": [table.name for table in Base.metadata.tables.values()]
                + ["auth_refresh_tokens"]
            },
        )
        if owned:
            report.issues.append(
                ReadinessIssue(
                    "runtime_role_owns_tables", "The runtime login must not own foundation tables."
                )
            )

        versions = (
            (await connection.execute(text("SELECT version_num FROM alembic_version")))
            .scalars()
            .all()
            if has_migrations
            else []
        )
        if len(versions) == 1:
            report.migration = versions[0]
        if versions != [expected_revision()]:
            report.issues.append(
                ReadinessIssue(
                    "migration_not_current", "The foundation migration is missing or not current."
                )
            )
    return report
