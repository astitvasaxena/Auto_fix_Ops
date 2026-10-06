"""
Idempotent schema migration for NeonDB.

Adds any columns the SQLAlchemy models declare but the live tables are missing.
Safe to re-run — every statement uses IF NOT EXISTS.
"""

import os
import sys
from sqlalchemy import create_engine, text

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


PG_URL = os.getenv("POSTGRES_URL", "")
if not PG_URL:
    print("POSTGRES_URL not set in environment.")
    sys.exit(1)


# (table, column, definition) — definition omits "ADD COLUMN" prefix
MIGRATIONS = [
    # incidents
    ("incidents", "project_id",            'UUID'),
    ("incidents", "resolved_target",       'JSON'),
    ("incidents", "status_timeline",       'JSON'),
    ("incidents", "ai_report",             'JSON'),

    # remediation_audits
    ("remediation_audits", "project_id",          'UUID'),
    ("remediation_audits", "previous_values",     'JSON'),
    ("remediation_audits", "is_shadow_run",       "VARCHAR DEFAULT 'false'"),
    ("remediation_audits", "human_agreed",        'VARCHAR'),
    ("remediation_audits", "failure_reason",      'VARCHAR'),
    ("remediation_audits", "failure_root_cause",  'VARCHAR'),

    # project_config (added in Phase 7+ but not migrated yet)
    ("project_config", "name",                          "VARCHAR DEFAULT 'Default Project'"),
    ("project_config", "github_repo",                   "VARCHAR"),
    ("project_config", "github_token_encrypted",        "TEXT"),
    ("project_config", "prometheus_url",                "VARCHAR"),
    ("project_config", "target_namespace",              "VARCHAR DEFAULT 'autofixops'"),
    ("project_config", "target_manifest_path",          "VARCHAR DEFAULT 'kubernetes_integration/target_app/deployment.yaml'"),
    ("project_config", "shadow_mode",                   "VARCHAR DEFAULT 'true'"),
    ("project_config", "confidence_threshold",          "FLOAT DEFAULT 0.80"),
    ("project_config", "allowed_chaos_namespaces",      "JSON"),
    ("project_config", "max_resource_scale_factor",     "FLOAT DEFAULT 2.0"),
    ("project_config", "created_at",                    "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ("project_config", "updated_at",                    "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),

    # system_config (kill switch)
    ("system_config", "system_mode",      "VARCHAR DEFAULT 'ACTIVE'"),
    ("system_config", "disabled_reason",  "TEXT"),
    ("system_config", "disabled_at",      "TIMESTAMP"),
    ("system_config", "updated_at",       "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
]


def column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column})
    return result.first() is not None


def table_exists(conn, table: str) -> bool:
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
    ), {"t": table})
    return result.first() is not None


def main():
    engine = create_engine(PG_URL)
    with engine.connect() as conn:
        added = 0
        skipped = 0
        for table, column, definition in MIGRATIONS:
            if not table_exists(conn, table):
                print(f"  [skip] table '{table}' does not exist yet (ORM will create it)")
                skipped += 1
                continue
            if column_exists(conn, table, column):
                print(f"  [skip] {table}.{column} already exists")
                skipped += 1
                continue
            sql = f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            print(f"  [add ] {sql}")
            conn.execute(text(sql))
            added += 1
        conn.commit()
        print(f"\nDone. Added {added} columns, skipped {skipped}.")


if __name__ == "__main__":
    main()
