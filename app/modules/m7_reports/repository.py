"""报送与本地接收端的持久化。

所有状态都先落库再发送：发送中断、回执丢失或进程重启后都能从已存状态恢复，
不依赖内存标记。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.errors import DomainError

SCHEMA = """
CREATE TABLE IF NOT EXISTS report_contract_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active_version INTEGER NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    contract_version INTEGER NOT NULL,
    contract_generation INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    content TEXT NOT NULL,
    approved TEXT NOT NULL,
    allocation TEXT NOT NULL,
    allocation_total TEXT NOT NULL,
    unallocated TEXT NOT NULL,
    status TEXT NOT NULL,
    receipt TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receiver_inbox (
    report_id TEXT PRIMARY KEY,
    contract_version INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    receipt_no TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    duplicate_deliveries INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS receiver_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    available INTEGER NOT NULL DEFAULT 1
);
"""

STATUS_PENDING = "PENDING"
STATUS_ACCEPTED_NO_RECEIPT = "ACCEPTED_NO_RECEIPT"
STATUS_ACCEPTED = "ACCEPTED"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReportRepository:
    def __init__(self, path: str, initial_contract_version: int = 1):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
        with self._tx() as db:
            if db.execute("SELECT COUNT(*) FROM report_contract_state").fetchone()[0] == 0:
                db.execute(
                    "INSERT INTO report_contract_state (id, active_version, generation, updated_at) VALUES (1, ?, 0, ?)",
                    (initial_contract_version, now()),
                )
            if db.execute("SELECT COUNT(*) FROM receiver_state").fetchone()[0] == 0:
                db.execute("INSERT INTO receiver_state (id, available) VALUES (1, 1)")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _tx(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            db.close()
            raise
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            try:
                db.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            db.close()

    # ---- 契约版本状态 -------------------------------------------------

    @staticmethod
    def _state_row(row) -> dict:
        return {
            "active_version": row["active_version"],
            "generation": row["generation"],
            "updated_at": row["updated_at"],
        }

    def contract_state(self) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM report_contract_state WHERE id = 1").fetchone()
        return self._state_row(row)

    def switch_contract(self, target_version: int, expected_version) -> dict:
        if expected_version is None:
            raise DomainError("STALE_VERSION", "换版必须携带期望的当前版本", status=409)
        try:
            expected = int(expected_version)
            target = int(target_version)
        except (TypeError, ValueError):
            raise DomainError("STALE_VERSION", "契约期望版本无效", status=409)
        with self._tx() as db:
            row = db.execute("SELECT * FROM report_contract_state WHERE id = 1").fetchone()
            if row["active_version"] != expected:
                raise DomainError(
                    "STALE_VERSION",
                    f"期望版本 {expected}，当前已是 {row['active_version']}",
                    status=409,
                )
            if row["active_version"] != target:
                db.execute(
                    "UPDATE report_contract_state SET active_version = ?, generation = generation + 1, updated_at = ? WHERE id = 1",
                    (target, now()),
                )
            fresh = db.execute("SELECT * FROM report_contract_state WHERE id = 1").fetchone()
            return self._state_row(fresh)

    # ---- 报送 ---------------------------------------------------------

    @staticmethod
    def _report_row(row) -> dict:
        if row is None:
            return None
        return {
            "report_id": row["report_id"],
            "contract_version": row["contract_version"],
            "contract_generation": row["contract_generation"],
            "content_digest": row["content_digest"],
            "content": json.loads(row["content"]),
            "approved": row["approved"],
            "allocation": json.loads(row["allocation"]),
            "allocation_total": row["allocation_total"],
            "unallocated": row["unallocated"],
            "status": row["status"],
            "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
            "attempts": row["attempts"],
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_report(self, report_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
        report = self._report_row(row)
        if report is None:
            raise DomainError("NOT_FOUND", f"报送不存在：{report_id}", status=404)
        return report

    def find_report(self, report_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
        return self._report_row(row)

    def list_reports(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM reports ORDER BY created_at, report_id").fetchall()
        return [self._report_row(row) for row in rows]

    def create_report(self, record: dict, expected_version: int) -> dict:
        """新建并冻结报送。

        在同一写事务内复核当前契约版本：若换版事务先一步提交，这里会看到
        新版本并拒绝过期的新建请求。
        """
        stamp = now()
        with self._tx() as db:
            state = db.execute("SELECT * FROM report_contract_state WHERE id = 1").fetchone()
            if state["active_version"] != int(expected_version):
                raise DomainError(
                    "STALE_VERSION",
                    f"报送期望契约版本 {expected_version}，当前已是 {state['active_version']}",
                    status=409,
                )
            if record["contract_version"] != state["active_version"]:
                raise DomainError(
                    "STALE_VERSION",
                    f"报送契约版本 {record['contract_version']} 已不是当前版本 {state['active_version']}",
                    status=409,
                )
            existing = db.execute("SELECT * FROM reports WHERE report_id = ?", (record["report_id"],)).fetchone()
            if existing is not None:
                if existing["content_digest"] != record["content_digest"]:
                    raise DomainError(
                        "CONTENT_CONFLICT",
                        f"报送 {record['report_id']} 已固定为另一份内容，拒绝改写",
                        status=409,
                    )
                return self._report_row(existing)
            db.execute(
                """INSERT INTO reports
                   (report_id, contract_version, contract_generation, content_digest, content, approved,
                    allocation, allocation_total, unallocated, status, receipt,
                    attempts, last_error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?)""",
                (
                    record["report_id"], record["contract_version"], state["generation"],
                    record["content_digest"],
                    json.dumps(record["content"], ensure_ascii=False, sort_keys=True),
                    record["approved"],
                    json.dumps(record["allocation"], ensure_ascii=False, sort_keys=True),
                    record["allocation_total"], record["unallocated"],
                    STATUS_PENDING, stamp, stamp,
                ),
            )
            row = db.execute("SELECT * FROM reports WHERE report_id = ?", (record["report_id"],)).fetchone()
            return self._report_row(row)

    def mark_attempt(self, report_id: str, error: str | None = None) -> dict:
        with self._tx() as db:
            row = db.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", f"报送不存在：{report_id}", status=404)
            db.execute(
                "UPDATE reports SET attempts = attempts + 1, last_error = ?, updated_at = ? WHERE report_id = ?",
                (error, now(), report_id),
            )
            fresh = db.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
            return self._report_row(fresh)

    def bind_receipt(self, report_id: str, receipt: dict) -> dict:
        with self._tx() as db:
            row = db.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", f"报送不存在：{report_id}", status=404)
            if row["content_digest"] != receipt["content_digest"]:
                raise DomainError(
                    "CONTENT_CONFLICT",
                    "接收端回执对应的内容与本报送不一致",
                    status=409,
                )
            db.execute(
                "UPDATE reports SET status = ?, receipt = ?, last_error = NULL, updated_at = ? WHERE report_id = ?",
                (STATUS_ACCEPTED, json.dumps(receipt, ensure_ascii=False, sort_keys=True), now(), report_id),
            )
            fresh = db.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
            return self._report_row(fresh)

    def mark_accepted_without_receipt(self, report_id: str) -> dict:
        with self._tx() as db:
            db.execute(
                "UPDATE reports SET status = ?, last_error = ?, updated_at = ? WHERE report_id = ?",
                (STATUS_ACCEPTED_NO_RECEIPT, "RECEIPT_LOST", now(), report_id),
            )
            fresh = db.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
            return self._report_row(fresh)

    # ---- 本地接收端 ---------------------------------------------------

    @staticmethod
    def _receipt_row(row) -> dict:
        return {
            "report_id": row["report_id"],
            "contract_version": row["contract_version"],
            "content_digest": row["content_digest"],
            "receipt_no": row["receipt_no"],
            "accepted_at": row["accepted_at"],
            "duplicate_deliveries": row["duplicate_deliveries"],
        }

    def receiver_available(self) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT available FROM receiver_state WHERE id = 1").fetchone()[0])

    def set_receiver_available(self, available: bool) -> bool:
        with self._tx() as db:
            db.execute("UPDATE receiver_state SET available = ? WHERE id = 1", (1 if available else 0,))
            return bool(db.execute("SELECT available FROM receiver_state WHERE id = 1").fetchone()[0])

    def receiver_receipt(self, report_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM receiver_inbox WHERE report_id = ?", (report_id,)).fetchone()
        return self._receipt_row(row) if row else None

    def receiver_inbox(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM receiver_inbox ORDER BY accepted_at, report_id").fetchall()
        return [self._receipt_row(row) for row in rows]

    def receiver_accept(self, report_id: str, contract_version: int, content_digest: str,
                        receipt_no: str) -> dict:
        """接收端受理。同一报送同一内容只收件一次；换内容拒收。"""
        with self._tx() as db:
            if not db.execute("SELECT available FROM receiver_state WHERE id = 1").fetchone()[0]:
                raise DomainError("RECEIVER_UNAVAILABLE", "本地接收端暂不可用", status=503)
            existing = db.execute("SELECT * FROM receiver_inbox WHERE report_id = ?", (report_id,)).fetchone()
            if existing is not None:
                if existing["content_digest"] != content_digest:
                    raise DomainError(
                        "CONTENT_CONFLICT",
                        f"接收端已受理报送 {report_id} 的另一份内容",
                        status=409,
                    )
                db.execute(
                    "UPDATE receiver_inbox SET duplicate_deliveries = duplicate_deliveries + 1 WHERE report_id = ?",
                    (report_id,),
                )
                row = db.execute("SELECT * FROM receiver_inbox WHERE report_id = ?", (report_id,)).fetchone()
                return self._receipt_row(row)
            accepted_at = now()
            db.execute(
                """INSERT INTO receiver_inbox
                   (report_id, contract_version, content_digest, receipt_no, accepted_at, duplicate_deliveries)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (report_id, contract_version, content_digest, receipt_no, accepted_at),
            )
            row = db.execute("SELECT * FROM receiver_inbox WHERE report_id = ?", (report_id,)).fetchone()
            return self._receipt_row(row)
