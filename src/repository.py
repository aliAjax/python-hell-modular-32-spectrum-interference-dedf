import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import (
    ConflictError,
    NotFoundError,
    DomainError,
    SEAT_CAPACITY,
    URGENCY_RANK,
    hold_expires_at,
    urgency_rank,
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS seats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seat_no INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'free',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS occupancy (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seat_id INTEGER,
                    item_id INTEGER NOT NULL,
                    region TEXT,
                    urgency TEXT NOT NULL,
                    urgency_rank INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    authorization_code TEXT,
                    confirmed_at TEXT,
                    released_at TEXT,
                    earliest_release_at TEXT,
                    hold_expires_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id),
                    FOREIGN KEY(seat_id) REFERENCES seats(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_occupancy_active_item
                    ON occupancy(item_id) WHERE status IN ('held', 'waitlisted');
                CREATE TABLE IF NOT EXISTS operation_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_key TEXT NOT NULL,
                    step TEXT NOT NULL,
                    item_id INTEGER,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(operation_key, step)
                );
                """
            )
            # 协调席位为全局共享资源，按容量初始化席位。
            for seat_no in range(1, SEAT_CAPACITY + 1):
                conn.execute(
                    "INSERT OR IGNORE INTO seats(seat_no,status,created_at) VALUES(?,?,?)",
                    (seat_no, "free", now_iso()),
                )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items(), "seats": self.seat_state()}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 占用账（协调席位 / 停用授权 共用）
    # ------------------------------------------------------------------

    def _get_step(self, conn, operation_key, step):
        return conn.execute(
            "SELECT * FROM operation_steps WHERE operation_key=? AND step=?",
            (operation_key, step),
        ).fetchone()

    def _record_step(self, conn, operation_key, step, item_id, status, result, error, attempts):
        conn.execute(
            """
            INSERT INTO operation_steps(operation_key,step,item_id,status,attempts,result,last_error,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(operation_key,step) DO UPDATE SET
                status=excluded.status,
                attempts=excluded.attempts,
                result=excluded.result,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                operation_key,
                step,
                item_id,
                status,
                attempts,
                canonical_json(result) if result is not None else None,
                error,
                now_iso(),
                now_iso(),
            ),
        )

    def run_steps(self, operation_key, item_id, steps):
        """幂等步骤编排：已完成的步骤直接跳过，失败的步骤保留次数后重试，
        重试不会重复占位或重复写审计。"""
        results = {}
        for name, fn in steps:
            probe = self.connect()
            try:
                existing = self._get_step(probe, operation_key, name)
            finally:
                probe.close()
            if existing is not None and existing["status"] == "done":
                results[name] = json.loads(existing["result"]) if existing["result"] else None
                continue
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = fn(conn)
                attempts = (existing["attempts"] if existing is not None else 0) + 1
                self._record_step(conn, operation_key, name, item_id, "done", result, None, attempts)
                conn.execute("COMMIT")
                results[name] = result
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                attempts = (existing["attempts"] if existing is not None else 0) + 1
                self._record_step(conn, operation_key, name, item_id, "failed", None, str(exc), attempts)
                raise
            finally:
                conn.close()
        return results

    def _seat_no(self, conn, seat_id):
        if not seat_id:
            return None
        row = conn.execute("SELECT seat_no FROM seats WHERE id=?", (seat_id,)).fetchone()
        return row["seat_no"] if row else None

    def _occupancy_view(self, conn, row):
        result = dict(row)
        result["seat_no"] = self._seat_no(conn, result.get("seat_id"))
        result["authorization_effective"] = bool(result.get("confirmed_at"))
        return result

    def _waitlist(self, conn, item_id, region, urgency_name, rank, authorization_code, actor, role):
        now = now_iso()
        earliest = conn.execute(
            "SELECT MIN(hold_expires_at) AS earliest FROM occupancy WHERE status='held'"
        ).fetchone()["earliest"]
        cur = conn.execute(
            """
            INSERT INTO occupancy(seat_id,item_id,region,urgency,urgency_rank,status,authorization_code,
                                   confirmed_at,released_at,earliest_release_at,hold_expires_at,
                                   version,created_at,updated_at)
            VALUES(NULL,?,?,?,?, 'waitlisted', ?, NULL, NULL, ?, NULL, 1, ?, ?)
            """,
            (item_id, region, urgency_name, rank, authorization_code, earliest, now, now),
        )
        occ_id = cur.lastrowid
        position = conn.execute(
            """
            SELECT COUNT(*) AS c FROM occupancy
            WHERE status='waitlisted'
              AND (urgency_rank > ? OR (urgency_rank = ? AND id <= ?))
            """,
            (rank, rank, occ_id),
        ).fetchone()["c"]
        self.append_audit(
            conn,
            item_id,
            "seat_waitlisted",
            actor,
            role,
            {
                "authorization_code": authorization_code,
                "urgency": urgency_name,
                "earliest_release_at": earliest,
                "position": position,
            },
        )
        return {
            "status": "waitlisted",
            "occupancy_id": occ_id,
            "seat_no": None,
            "earliest_release_at": earliest,
            "position": position,
            "authorization_effective": False,
        }

    def apply_seat(self, item_id, authorization_code, urgency_level, region, actor, role):
        urgency_name = urgency_level if urgency_level in URGENCY_RANK else "low"
        rank = urgency_rank(urgency_name)
        operation_key = "seat_apply:%s" % item_id

        def step_apply(conn):
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            existing = conn.execute(
                "SELECT * FROM occupancy WHERE item_id=? AND status IN ('held','waitlisted') ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            if existing is not None:
                return self._occupancy_view(conn, existing)
            held_count = conn.execute(
                "SELECT COUNT(*) AS c FROM occupancy WHERE status='held'"
            ).fetchone()["c"]
            if held_count >= SEAT_CAPACITY:
                return self._waitlist(
                    conn, item_id, region, urgency_name, rank, authorization_code, actor, role
                )
            seat = conn.execute(
                "SELECT * FROM seats WHERE status='free' ORDER BY seat_no LIMIT 1"
            ).fetchone()
            if seat is None:
                return self._waitlist(
                    conn, item_id, region, urgency_name, rank, authorization_code, actor, role
                )
            now = now_iso()
            cur = conn.execute(
                """
                INSERT INTO occupancy(seat_id,item_id,region,urgency,urgency_rank,status,authorization_code,
                                       confirmed_at,released_at,earliest_release_at,hold_expires_at,
                                       version,created_at,updated_at)
                VALUES(?,?,?,?,?, 'held', ?, NULL, NULL, NULL, ?, 1, ?, ?)
                """,
                (seat["id"], item_id, region, urgency_name, rank, authorization_code,
                 hold_expires_at(now), now, now),
            )
            conn.execute("UPDATE seats SET status='occupied' WHERE id=?", (seat["id"],))
            self.append_audit(
                conn,
                item_id,
                "seat_held",
                actor,
                role,
                {"seat_no": seat["seat_no"], "authorization_code": authorization_code, "urgency": urgency_name},
            )
            return {
                "status": "held",
                "occupancy_id": cur.lastrowid,
                "seat_no": seat["seat_no"],
                "authorization_effective": False,
            }

        results = self.run_steps(operation_key, item_id, [("occupancy", step_apply)])
        return results["occupancy"]

    def confirm_seat(self, item_id, actor, role):
        # 幂等键含确认人：同一确认人的重试直接返回成功（不重复占位/审计），
        # 不同确认人提交则要真正执行并面对最新占用（并发仅一人成功）。
        operation_key = "seat_confirm:%s:%s" % (item_id, actor)

        def step_confirm(conn):
            row = conn.execute(
                "SELECT * FROM occupancy WHERE item_id=? AND status='held' ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            if row is None:
                raise DomainError("seat_not_held", "当前没有可确认的占位", 409)
            view = self._occupancy_view(conn, row)
            if row["confirmed_at"] is not None:
                err = ConflictError("seat_already_confirmed", "占位已被确认，授权已生效")
                err.latest = view
                raise err
            now = now_iso()
            cur = conn.execute(
                """
                UPDATE occupancy
                SET confirmed_at=?, version=version+1, updated_at=?
                WHERE id=? AND version=? AND status='held' AND confirmed_at IS NULL
                """,
                (now, now, row["id"], row["version"]),
            )
            if cur.rowcount != 1:
                latest_row = conn.execute("SELECT * FROM occupancy WHERE id=?", (row["id"],)).fetchone()
                err = ConflictError("seat_confirm_conflict", "占位已被他人确认，请查看最新占用")
                err.latest = self._occupancy_view(conn, latest_row)
                raise err
            self.append_audit(
                conn,
                item_id,
                "seat_confirmed",
                actor,
                role,
                {"seat_no": view["seat_no"], "authorization_code": row["authorization_code"]},
            )
            return {
                "status": "held",
                "confirmed": True,
                "confirmed_at": now,
                "seat_no": view["seat_no"],
                "authorization_effective": True,
            }

        results = self.run_steps(operation_key, item_id, [("confirm", step_confirm)])
        return results["confirm"]

    def _promote_waitlist(self, conn, actor, role, now):
        next_row = conn.execute(
            "SELECT * FROM occupancy WHERE status='waitlisted' ORDER BY urgency_rank DESC, id ASC LIMIT 1"
        ).fetchone()
        if next_row is None:
            return None
        seat = conn.execute("SELECT * FROM seats WHERE status='free' ORDER BY seat_no LIMIT 1").fetchone()
        if seat is None:
            return None
        conn.execute(
            """
            UPDATE occupancy
            SET seat_id=?, status='held', confirmed_at=NULL, earliest_release_at=NULL,
                hold_expires_at=?, version=version+1, updated_at=?
            WHERE id=? AND status='waitlisted'
            """,
            (seat["id"], hold_expires_at(now), now, next_row["id"]),
        )
        conn.execute("UPDATE seats SET status='occupied' WHERE id=?", (seat["id"],))
        self.append_audit(
            conn,
            next_row["item_id"],
            "seat_promoted",
            actor,
            role,
            {"seat_no": seat["seat_no"], "authorization_code": next_row["authorization_code"]},
        )
        return {"item_id": next_row["item_id"], "seat_no": seat["seat_no"]}

    def _refresh_earliest_release(self, conn):
        earliest = conn.execute(
            "SELECT MIN(hold_expires_at) AS e FROM occupancy WHERE status='held'"
        ).fetchone()["e"]
        conn.execute(
            "UPDATE occupancy SET earliest_release_at=? WHERE status='waitlisted'",
            (earliest,),
        )

    def _release(self, item_id, new_status, event_type, reason, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            occ = conn.execute(
                "SELECT * FROM occupancy WHERE item_id=? AND status IN ('held','waitlisted') ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            if occ is None:
                conn.execute("COMMIT")
                return {"released": False, "promoted": None}
            now = now_iso()
            promoted = None
            if occ["status"] == "held":
                seat_no = self._seat_no(conn, occ["seat_id"])
                conn.execute(
                    "UPDATE occupancy SET status=?, released_at=?, version=version+1, updated_at=? "
                    "WHERE id=? AND status='held'",
                    (new_status, now, now, occ["id"]),
                )
                if occ["seat_id"]:
                    conn.execute("UPDATE seats SET status='free' WHERE id=?", (occ["seat_id"],))
                self.append_audit(
                    conn, item_id, event_type, actor, role,
                    {"reason": reason, "seat_no": seat_no, "authorization_code": occ["authorization_code"]},
                )
                promoted = self._promote_waitlist(conn, actor, role, now)
            else:
                conn.execute(
                    "UPDATE occupancy SET status=?, version=version+1, updated_at=? "
                    "WHERE id=? AND status='waitlisted'",
                    (new_status, now, now, occ["id"]),
                )
                self.append_audit(
                    conn, item_id, event_type, actor, role,
                    {"reason": reason, "seat_no": None, "authorization_code": occ["authorization_code"]},
                )
            self._refresh_earliest_release(conn)
            conn.execute("COMMIT")
            return {"released": True, "released_status": new_status, "promoted": promoted}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def release_seat(self, item_id, actor, role, reason="resolved"):
        return self._release(item_id, "released", "seat_released", reason, actor, role)

    def withdraw_seat(self, item_id, actor, role, reason="cancelled"):
        return self._release(item_id, "withdrawn", "seat_withdrawn", reason, actor, role)

    def withdraw_unauthorized(self, item_id, actor, role, reason="unauthorized_occupancy"):
        return self._release(item_id, "withdrawn", "seat_withdrawn_unauthorized", reason, actor, role)

    def get_active_occupancy(self, item_id):
        # 返回该事件最近一条占用账（含已释放/已撤回），用于页面展示；
        # 是否仍在占位/候补由 status 判定。无记录即按未占用读取。
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM occupancy WHERE item_id=? ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            return self._occupancy_view(conn, row)
        finally:
            conn.close()

    def list_active_occupancy(self):
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT o.*, i.stable_key, i.status AS item_status, s.seat_no
                FROM occupancy o
                JOIN items i ON i.id = o.item_id
                LEFT JOIN seats s ON s.id = o.seat_id
                WHERE o.status IN ('held','waitlisted')
                ORDER BY CASE o.status WHEN 'held' THEN 0 ELSE 1 END,
                         o.urgency_rank DESC, o.id ASC
                """
            ).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["authorization_effective"] = bool(value["confirmed_at"])
                result.append(value)
            waitlisted = [r for r in result if r["status"] == "waitlisted"]
            waitlisted.sort(key=lambda r: (-r["urgency_rank"], r["id"]))
            for index, row in enumerate(waitlisted, start=1):
                row["position"] = index
            return result
        finally:
            conn.close()

    def waitlist_position(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT id,urgency_rank FROM occupancy WHERE item_id=? AND status='waitlisted'",
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            return conn.execute(
                """
                SELECT COUNT(*) AS c FROM occupancy
                WHERE status='waitlisted'
                  AND (urgency_rank > ? OR (urgency_rank = ? AND id <= ?))
                """,
                (row["urgency_rank"], row["urgency_rank"], row["id"]),
            ).fetchone()["c"]
        finally:
            conn.close()

    def seat_state(self):
        conn = self.connect()
        try:
            held = conn.execute(
                "SELECT COUNT(*) AS c FROM occupancy WHERE status='held'"
            ).fetchone()["c"]
            waitlisted = conn.execute(
                "SELECT COUNT(*) AS c FROM occupancy WHERE status='waitlisted'"
            ).fetchone()["c"]
            seats = conn.execute("SELECT seat_no,status FROM seats ORDER BY seat_no").fetchall()
            earliest = conn.execute(
                "SELECT MIN(hold_expires_at) AS e FROM occupancy WHERE status='held'"
            ).fetchone()["e"]
            return {
                "capacity": SEAT_CAPACITY,
                "held": held,
                "remaining": SEAT_CAPACITY - held,
                "waitlisted": waitlisted,
                "earliest_release_at": earliest,
                "seats": [{"seat_no": s["seat_no"], "status": s["status"]} for s in seats],
            }
        finally:
            conn.close()
