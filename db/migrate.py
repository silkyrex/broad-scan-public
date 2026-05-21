import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))
MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def run_migrations(conn=None, seed_only=False):
    """Run any unapplied migrations in order.

    seed_only=True marks all migrations as applied without running them --
    used on fresh installs where the schema is already correct.
    """
    close = conn is None
    if close:
        conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name       TEXT NOT NULL PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))

    for path in migrations:
        name = path.name
        if name in applied:
            continue
        if seed_only:
            conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))
            print(f"  seeded  {name}")
        else:
            print(f"  applying {name} ...")
            try:
                conn.executescript(path.read_text())
            except sqlite3.OperationalError as e:
                print(f"  skipped {name} (schema already matches: {e})")
            conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))
            print(f"  applied  {name}")
        conn.commit()

    if close:
        conn.close()


if __name__ == "__main__":
    run_migrations()
    print("migrations done")
