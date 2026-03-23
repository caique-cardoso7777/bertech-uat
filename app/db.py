from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).parent.parent / "data" / "bertech_uat.db"


# ── connection ───────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    # Allow override via env var (useful for CI or network-mounted filesystems)
    db_path_str = os.environ.get("BERTECH_DB_PATH", str(DB_PATH))
    Path(db_path_str).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path_str, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL mode improves concurrency but may not work on all network filesystems.
    # Falls back to DELETE mode transparently if WAL is unsupported.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS suites (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        description TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS scenarios (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        suite_id    INTEGER NOT NULL REFERENCES suites(id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        description TEXT,
        script_key  TEXT NOT NULL,
        order_idx   INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        suite_id        INTEGER NOT NULL REFERENCES suites(id) ON DELETE CASCADE,
        env             TEXT NOT NULL,
        num_users       INTEGER NOT NULL DEFAULT 1,
        status          TEXT NOT NULL DEFAULT 'queued',
        notes           TEXT,
        started_at      TEXT,
        finished_at     TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS run_results (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        scenario_id     INTEGER REFERENCES scenarios(id) ON DELETE SET NULL,
        scenario_name   TEXT NOT NULL,
        user_idx        INTEGER NOT NULL,
        status          TEXT NOT NULL DEFAULT 'pending',
        steps_json      TEXT,
        errors_json     TEXT,
        screenshot_path TEXT,
        duration_ms     INTEGER,
        email_used      TEXT,
        started_at      TEXT,
        finished_at     TEXT
    );
    """)
    conn.commit()


# ── helpers ──────────────────────────────────────────────────────────

def fetch_one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()

def fetch_all(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()

def now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── suites ───────────────────────────────────────────────────────────

def list_suites(conn) -> List[Dict]:
    rows = fetch_all(conn, """
        SELECT s.id, s.name, s.description, s.created_at,
               COUNT(sc.id) as scenario_count,
               COUNT(r.id)  as run_count,
               MAX(r.started_at) as last_run_at,
               (SELECT status FROM runs WHERE suite_id=s.id ORDER BY id DESC LIMIT 1) as last_run_status
        FROM suites s
        LEFT JOIN scenarios sc ON sc.suite_id = s.id
        LEFT JOIN runs r ON r.suite_id = s.id
        GROUP BY s.id ORDER BY s.id
    """)
    return [dict(r) for r in rows]

def get_suite(conn, suite_id: int) -> Optional[Dict]:
    row = fetch_one(conn, "SELECT * FROM suites WHERE id=?", (suite_id,))
    return dict(row) if row else None

def create_suite(conn, name: str, description: str = "") -> int:
    conn.execute(
        "INSERT INTO suites(name, description) VALUES(?,?)",
        (name, description)
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

def delete_suite(conn, suite_id: int) -> None:
    conn.execute("DELETE FROM suites WHERE id=?", (suite_id,))
    conn.commit()


def ensure_default_suite(conn) -> int:
    """Guarantee the Buoy-Champ suite exists. Returns the suite id (always 1)."""
    row = fetch_one(conn, "SELECT id FROM suites WHERE id=1")
    if row:
        return row[0]
    conn.execute(
        "INSERT INTO suites(id, name, description) VALUES(1,?,?)",
        (
            "Business Owner Onboarding",
            "Automated UAT — Buoy Champ full onboarding + enrollment flow",
        ),
    )
    conn.execute(
        "INSERT INTO scenarios(suite_id, name, script_key, order_idx) VALUES(1,?,?,0)",
        ("Buoy Champ Onboarding", "buoy_champ_onboarding"),
    )
    conn.commit()
    return 1


# ── scenarios ────────────────────────────────────────────────────────

def list_scenarios(conn, suite_id: int) -> List[Dict]:
    rows = fetch_all(conn,
        "SELECT * FROM scenarios WHERE suite_id=? ORDER BY order_idx, id",
        (suite_id,))
    return [dict(r) for r in rows]

def add_scenario(conn, suite_id: int, name: str, script_key: str, description: str = "") -> int:
    conn.execute(
        "INSERT INTO scenarios(suite_id, name, description, script_key) VALUES(?,?,?,?)",
        (suite_id, name, description, script_key)
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

def delete_scenario(conn, scenario_id: int) -> None:
    conn.execute("DELETE FROM scenarios WHERE id=?", (scenario_id,))
    conn.commit()


# ── runs ─────────────────────────────────────────────────────────────

def create_run(conn, suite_id: int, env: str, num_users: int, notes: str = "") -> int:
    conn.execute(
        """INSERT INTO runs(suite_id, env, num_users, status, notes, started_at)
           VALUES(?,?,?,'running',?,?)""",
        (suite_id, env, num_users, notes, now_iso())
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

def update_run_status(conn, run_id: int, status: str) -> None:
    finished = now_iso() if status in ("complete", "error", "canceled") else None
    conn.execute(
        "UPDATE runs SET status=?, finished_at=? WHERE id=?",
        (status, finished, run_id)
    )
    conn.commit()

def get_run(conn, run_id: int) -> Optional[Dict]:
    row = fetch_one(conn, "SELECT * FROM runs WHERE id=?", (run_id,))
    return dict(row) if row else None

def list_runs(conn, suite_id: int | None = None) -> List[Dict]:
    if suite_id:
        rows = fetch_all(conn,
            "SELECT r.*, s.name as suite_name FROM runs r JOIN suites s ON s.id=r.suite_id WHERE r.suite_id=? ORDER BY r.id DESC",
            (suite_id,))
    else:
        rows = fetch_all(conn,
            "SELECT r.*, s.name as suite_name FROM runs r JOIN suites s ON s.id=r.suite_id ORDER BY r.id DESC LIMIT 100")
    return [dict(r) for r in rows]


# ── run results ──────────────────────────────────────────────────────

def save_result(conn, run_id: int, scenario_id: int | None, scenario_name: str,
                user_idx: int, status: str, steps: list, errors: list,
                duration_ms: int, email_used: str = "", screenshot_path: str = "") -> None:
    conn.execute("""
        INSERT INTO run_results(
            run_id, scenario_id, scenario_name, user_idx, status,
            steps_json, errors_json, screenshot_path, duration_ms,
            email_used, started_at, finished_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        run_id, scenario_id, scenario_name, user_idx, status,
        json.dumps(steps), json.dumps(errors), screenshot_path, duration_ms,
        email_used, now_iso(), now_iso()
    ))
    conn.commit()

def get_run_results(conn, run_id: int) -> List[Dict]:
    rows = fetch_all(conn,
        "SELECT * FROM run_results WHERE run_id=? ORDER BY scenario_name, user_idx",
        (run_id,))
    results = []
    for r in rows:
        d = dict(r)
        d["steps"] = json.loads(d.get("steps_json") or "[]")
        d["errors"] = json.loads(d.get("errors_json") or "[]")
        results.append(d)
    return results

def get_run_stats(conn, run_id: int) -> Dict:
    results = get_run_results(conn, run_id)
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "PASS")
    warned = sum(1 for r in results if r["status"] == "WARN")
    failed = sum(1 for r in results if r["status"] in ("FAIL", "error"))
    return {"total": total, "passed": passed, "warned": warned, "failed": failed}
