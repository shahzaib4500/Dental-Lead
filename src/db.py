"""Local SQLite database persistence module for review portal and pipeline runs.

Stores:
- Runs: (run_id, campaign, search_scope, target_value, status, created_at, counts, error)
- Review Leads: (lead_id, run_id, practice_name, phone, address, website, campaign, reason, source_url, status, created_at, raw_json)
- Failed Searches / Errors: (error_id, run_id, zip_or_city, keyword, error_message, created_at)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class LeadDatabase:
    """Manages SQLite storage for the Review Portal and execution runs."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            data_dir = Path(__file__).parent.parent / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = data_dir / "leads.db"
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_schema()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        """Create tables if they do not exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    campaign TEXT NOT NULL,
                    search_scope TEXT NOT NULL,
                    target_value TEXT,
                    status TEXT NOT NULL,
                    total_discovered INTEGER DEFAULT 0,
                    qualified INTEGER DEFAULT 0,
                    needs_review INTEGER DEFAULT 0,
                    disqualified INTEGER DEFAULT 0,
                    duplicates INTEGER DEFAULT 0,
                    ingested INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS review_leads (
                    lead_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    practice_name TEXT NOT NULL,
                    phone TEXT,
                    address TEXT,
                    zip_code TEXT,
                    website TEXT,
                    campaign TEXT NOT NULL,
                    qualification_reason TEXT,
                    source_url TEXT,
                    status TEXT NOT NULL, -- 'pending', 'approved', 'rejected'
                    created_at TEXT NOT NULL,
                    raw_json TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS search_errors (
                    error_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    location TEXT,
                    keyword TEXT,
                    error_message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.commit()

    def create_run(
        self,
        campaign: str,
        search_scope: str,
        target_value: str,
        run_id: Optional[str] = None,
    ) -> str:
        rid = run_id or str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, campaign, search_scope, target_value, status, created_at)
                VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (rid, campaign, search_scope, target_value, now),
            )
            conn.commit()
        return rid

    def update_run_summary(
        self,
        run_id: str,
        status: str,
        counts: Dict[str, int],
        error_message: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    total_discovered = ?,
                    qualified = ?,
                    needs_review = ?,
                    disqualified = ?,
                    duplicates = ?,
                    ingested = ?,
                    error_message = ?,
                    completed_at = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    counts.get("total_discovered", 0),
                    counts.get("qualified", 0),
                    counts.get("needs_review", 0),
                    counts.get("disqualified", 0),
                    counts.get("duplicates", 0),
                    counts.get("ingested", 0),
                    error_message,
                    now,
                    run_id,
                ),
            )
            conn.commit()

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row:
                return dict(row)
        return None

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def add_review_lead(self, lead_dict: Dict[str, Any], run_id: Optional[str] = None) -> str:
        lid = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO review_leads (
                    lead_id, run_id, practice_name, phone, address, zip_code,
                    website, campaign, qualification_reason, source_url, status, created_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    lid,
                    run_id,
                    lead_dict.get("practice_name", ""),
                    lead_dict.get("phone"),
                    lead_dict.get("address"),
                    lead_dict.get("zip_code"),
                    lead_dict.get("website"),
                    lead_dict.get("campaign_type", "PRM"),
                    lead_dict.get("qualification_reason", ""),
                    lead_dict.get("source_url") or lead_dict.get("website"),
                    now,
                    json.dumps(lead_dict),
                ),
            )
            conn.commit()
        return lid

    def list_review_leads(self, status: str = "pending", limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM review_leads WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
            result = []
            for r in rows:
                item = dict(r)
                if item.get("raw_json"):
                    try:
                        item["raw_data"] = json.loads(item["raw_json"])
                    except Exception:
                        item["raw_data"] = {}
                result.append(item)
            return result

    def get_review_lead(self, lead_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM review_leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if row:
                item = dict(row)
                if item.get("raw_json"):
                    item["raw_data"] = json.loads(item["raw_json"])
                return item
        return None

    def update_review_lead_status(self, lead_id: str, new_status: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE review_leads SET status = ? WHERE lead_id = ?",
                (new_status, lead_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def record_search_error(
        self,
        location: str,
        keyword: str,
        error_message: str,
        run_id: Optional[str] = None,
    ) -> str:
        eid = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO search_errors (error_id, run_id, location, keyword, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (eid, run_id, location, keyword, error_message, now),
            )
            conn.commit()
        return eid

    def list_search_errors(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM search_errors ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
