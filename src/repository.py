import json
import sqlite3
import uuid
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError
from .domain import URGENCY_RANK

ACTIVE_STATES = ("held", "waiting", "confirmed", "coordinating")
OCCUPIED_STATES = ("held", "confirmed", "coordinating")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path
        # point name -> remaining forced failures (used by failure/retry tests)
        self._fail_points = {}

    # ------------------------------------------------------------------ hooks
    def fail_once(self, point):
        """安排在下一次经过 point 时抛出一次运行时故障，用于验证可恢复写入。"""
        self._fail_points[point] = self._fail_points.get(point, 0) + 1

    def _maybe_fault(self, point):
        remaining = self._fail_points.get(point, 0)
        if remaining > 0:
            self._fail_points[point] = remaining - 1
            raise RuntimeError("forced failure at %s" % point)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
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
                    created_at TEXT NOT NULL,
                    idempotency_key TEXT
                );
                CREATE TABLE IF NOT EXISTS seat_pools (
                    region TEXT PRIMARY KEY,
                    capacity INTEGER NOT NULL CHECK (capacity >= 0),
                    updated_by TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS occupancy (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    op_id TEXT,
                    item_id INTEGER NOT NULL,
                    region TEXT NOT NULL,
                    seat TEXT,
                    state TEXT NOT NULL,
                    urgency TEXT NOT NULL,
                    urgency_rank INTEGER NOT NULL,
                    authorization_code TEXT NOT NULL,
                    expected_release_at TEXT,
                    applied_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    released_at TEXT,
                    release_reason TEXT,
                    released_op_id TEXT,
                    confirmed_op_id TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS occupancy_operations (
                    op_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    item_id INTEGER,
                    region TEXT,
                    state TEXT NOT NULL,
                    details TEXT NOT NULL,
                    result TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS occupancy_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    op_id TEXT NOT NULL,
                    step TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(op_id, step),
                    FOREIGN KEY(op_id) REFERENCES occupancy_operations(op_id)
                );
                """
            )
            # 迁移旧库：审计表补齐幂等键
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
            if "idempotency_key" not in columns:
                conn.execute("ALTER TABLE audit_events ADD COLUMN idempotency_key TEXT")
            occ_columns = {row["name"] for row in conn.execute("PRAGMA table_info(occupancy)").fetchall()}
            if "released_op_id" not in occ_columns:
                conn.execute("ALTER TABLE occupancy ADD COLUMN released_op_id TEXT")
            if "confirmed_op_id" not in occ_columns:
                conn.execute("ALTER TABLE occupancy ADD COLUMN confirmed_op_id TEXT")
            conn.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_idempotent
                    ON audit_events(idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_occupancy_active_item
                    ON occupancy(item_id) WHERE state IN ('held','waiting','confirmed','coordinating');
                CREATE UNIQUE INDEX IF NOT EXISTS idx_occupancy_active_authorization
                    ON occupancy(authorization_code) WHERE state IN ('held','waiting','confirmed','coordinating');
                CREATE UNIQUE INDEX IF NOT EXISTS idx_occupancy_active_seat
                    ON occupancy(region, seat) WHERE state IN ('held','confirmed','coordinating') AND seat IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_occupancy_op
                    ON occupancy(op_id) WHERE op_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_occupancy_region_state
                    ON occupancy(region, state, urgency_rank, applied_at, id);
                """
            )
        finally:
            conn.close()

    # ----------------------------------------------------------------- helpers
    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _row_to_occupancy(self, row):
        if row is None:
            return None
        value = dict(row)
        return value

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload, idempotency_key=None):
        """追加审计事件；给定 idempotency_key 时，重放返回既有事件，绝不重复写账。"""
        if idempotency_key is not None:
            existing = conn.execute(
                "SELECT id FROM audit_events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return existing["id"]
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
        cursor = conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at,idempotency_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash,
             event["created_at"], idempotency_key),
        )
        return cursor.lastrowid

    # -------------------------------------------------------------------- items
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

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload,
                     expected_version=None, occupancy_state=None):
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
            if occupancy_state is not None:
                ledger = conn.execute(
                    "SELECT * FROM occupancy WHERE item_id=? AND state IN ('held','waiting','confirmed','coordinating')",
                    (item_id,),
                ).fetchone()
                if ledger is not None:
                    conn.execute(
                        "UPDATE occupancy SET state=?, version=version+1 WHERE id=?",
                        (occupancy_state, ledger["id"]),
                    )
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
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()

    # ============================================================== seat pools
    def ensure_pool(self, conn, region, capacity=3):
        conn.execute(
            "INSERT INTO seat_pools(region,capacity,updated_by,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(region) DO NOTHING",
            (region, capacity, "system", now_iso()),
        )

    def set_pool_capacity(self, region, capacity, actor, reason=""):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self.ensure_pool(conn, region)
            conn.execute(
                "UPDATE seat_pools SET capacity=?, updated_by=?, updated_at=? WHERE region=?",
                (capacity, actor, now_iso(), region),
            )
            self.append_audit(
                conn, None, "pool_configured", actor, None,
                {"region": region, "capacity": capacity, "reason": reason},
                idempotency_key=None,
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_pool(self, conn, region):
        self.ensure_pool(conn, region)
        return conn.execute("SELECT * FROM seat_pools WHERE region=?", (region,)).fetchone()

    # ============================================================ occupancy ops
    def active_occupancy(self, conn, item_id):
        return conn.execute(
            "SELECT * FROM occupancy WHERE item_id=? AND state IN ('held','waiting','confirmed','coordinating')",
            (item_id,),
        ).fetchone()

    def get_occupancy(self, item_id):
        conn = self.connect()
        try:
            row = self.active_occupancy(conn, item_id)
            return self._row_to_occupancy(row)
        finally:
            conn.close()

    def _free_seats(self, conn, region, capacity):
        taken = {
            row["seat"]
            for row in conn.execute(
                "SELECT seat FROM occupancy WHERE region=? AND state IN ('held','confirmed','coordinating') "
                "AND seat IS NOT NULL",
                (region,),
            ).fetchall()
        }
        free = ["SEAT-%d" % index for index in range(1, capacity + 1) if "SEAT-%d" % index not in taken]
        return free, taken

    def _earliest_release(self, conn, region):
        row = conn.execute(
            "SELECT MIN(expected_release_at) AS earliest FROM occupancy "
            "WHERE region=? AND state IN ('held','confirmed','coordinating') AND expected_release_at IS NOT NULL",
            (region,),
        ).fetchone()
        return row["earliest"] if row else None

    # -------------------------------------------------- durable operation engine
    def _find_open_operation(self, conn, kind, item_id):
        return conn.execute(
            "SELECT * FROM occupancy_operations WHERE kind=? AND item_id=? AND state='open'",
            (kind, item_id),
        ).fetchone()

    def _load_operation(self, conn, op_id):
        return conn.execute(
            "SELECT * FROM occupancy_operations WHERE op_id=?", (op_id,)
        ).fetchone()

    def _create_operation(self, kind, item_id, region, details):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._find_open_operation(conn, kind, item_id)
            if existing is not None:
                existing_id = existing["op_id"]
                conn.execute("COMMIT")
                return self.get_operation(existing_id)
            op_id = "%s-%s-%s" % (kind, item_id, uuid.uuid4().hex[:12])
            conn.execute(
                "INSERT INTO occupancy_operations(op_id,kind,item_id,region,state,details,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (op_id, kind, item_id, region, "open", canonical_json(details), now_iso(), now_iso()),
            )
            conn.execute("COMMIT")
            return self.get_operation(op_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_operation(self, op_id):
        conn = self.connect()
        try:
            row = self._load_operation(conn, op_id)
            if row is None:
                raise NotFoundError("operation_not_found", "占用操作不存在")
            result = dict(row)
            result["details"] = json.loads(result["details"])
            result["steps"] = [
                dict(step_row) for step_row in conn.execute(
                    "SELECT step,state,attempts,result,updated_at FROM occupancy_steps WHERE op_id=? ORDER BY id",
                    (op_id,),
                ).fetchall()
            ]
            return result
        finally:
            conn.close()

    def list_open_operations(self, item_id=None):
        conn = self.connect()
        try:
            if item_id is None:
                rows = conn.execute(
                    "SELECT * FROM occupancy_operations WHERE state='open' ORDER BY created_at, op_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM occupancy_operations WHERE state='open' AND item_id=? ORDER BY created_at, op_id",
                    (item_id,),
                ).fetchall()
            return [self.get_operation(row["op_id"]) for row in rows]
        finally:
            conn.close()

    def _bump_attempt(self, conn, op_id, step):
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM occupancy_steps WHERE op_id=? AND step=?", (op_id, step)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO occupancy_steps(op_id,step,state,attempts,updated_at) VALUES(?,?, 'pending', 1, ?)",
                (op_id, step, now_iso()),
            )
            attempts = 1
        else:
            attempts = int(row["attempts"]) + 1
            conn.execute(
                "UPDATE occupancy_steps SET attempts=?, state='pending', updated_at=? WHERE op_id=? AND step=?",
                (attempts, now_iso(), op_id, step),
            )
        conn.execute("COMMIT")
        return attempts

    def _mark_step(self, conn, op_id, step, state, result):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE occupancy_steps SET state=?, result=?, updated_at=? WHERE op_id=? AND step=?",
            (state, canonical_json(result), now_iso(), op_id, step),
        )
        conn.execute("COMMIT")

    def _finish_operation(self, conn, op_id, state, result=None, error_code=None, error_message=None):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE occupancy_operations SET state=?, result=?, error_code=?, error_message=?, updated_at=? "
            "WHERE op_id=?",
            (state, canonical_json(result) if result is not None else None, error_code, error_message,
             now_iso(), op_id),
        )
        conn.execute("COMMIT")

    STEP_PLANS = {
        "apply": ("record",),
        "confirm": ("confirm",),
        "release": ("release", "withdraw_item"),
        "revoke": ("release", "withdraw_item"),
        "resolve": ("release",),
        "cancel": ("release",),
    }

    def resume_occupancy(self, op_id):
        conn = self.connect()
        try:
            op = self._load_operation(conn, op_id)
            if op is None:
                raise NotFoundError("operation_not_found", "占用操作不存在")
            op = dict(op)
            op["details"] = json.loads(op["details"])
            if op["state"] == "succeeded":
                return json.loads(op["result"])
            if op["state"] == "failed":
                raise DomainError(op["error_code"] or "operation_failed",
                                  op["error_message"] or "占用操作已失败", 409)
            steps = self.STEP_PLANS[op["kind"]]
            results = {}
            for step in steps:
                step_row = conn.execute(
                    "SELECT * FROM occupancy_steps WHERE op_id=? AND step=?", (op_id, step)
                ).fetchone()
                if step_row is not None and step_row["state"] == "succeeded":
                    results[step] = json.loads(step_row["result"])
                    continue
                attempts = self._bump_attempt(conn, op_id, step)
                try:
                    result = self._run_step(conn, op, step, results)
                except DomainError as exc:
                    # 业务冲突（席位被抢、仍在候补等）是最终结论，操作收档；
                    # 其他意外故障不落档，保留未完成步骤以便 resume 续跑。
                    self._finish_operation(
                        conn, op_id, "failed",
                        error_code=exc.code, error_message=str(exc),
                    )
                    raise
                result = result or {}
                result["attempts"] = attempts
                self._mark_step(conn, op_id, step, "succeeded", result)
                results[step] = result
            final = self._finalize_result(conn, op, results)
            self._finish_operation(conn, op_id, "succeeded", result=final)
            return final
        except Exception:
            raise
        finally:
            conn.close()

    def _finalize_result(self, conn, op, results):
        if op["kind"] == "apply":
            occ = conn.execute(
                "SELECT * FROM occupancy WHERE id=?", (results["record"]["occupancy_id"],)
            ).fetchone()
            result = self._occupancy_view(conn, dict(occ))
            if result["state"] == "waiting":
                result["earliest_release_at"] = self._earliest_release(conn, occ["region"])
            return result
        if op["kind"] == "confirm":
            occ = self.active_occupancy(conn, op["item_id"])
            return self._occupancy_view(conn, dict(occ))
        # release family
        released = results["release"]
        return released

    def _occupancy_view(self, conn, occ):
        view = dict(occ)
        view.pop("urgency_rank", None)
        if view["state"] == "waiting":
            view["earliest_release_at"] = self._earliest_release(conn, occ["region"])
            ahead = conn.execute(
                "SELECT COUNT(*) AS total FROM occupancy WHERE region=? AND state='waiting' "
                "AND (urgency_rank < ? OR (urgency_rank = ? AND (applied_at < ? OR (applied_at = ? AND id < ?))))",
                (occ["region"], occ["urgency_rank"], occ["urgency_rank"],
                 occ["applied_at"], occ["applied_at"], occ["id"]),
            ).fetchone()["total"]
            view["waiting_ahead"] = ahead
        return view

    def _run_step(self, conn, op, step, prior_results):
        if op["kind"] == "apply" and step == "record":
            return self._step_apply_record(conn, op)
        if op["kind"] == "confirm" and step == "confirm":
            return self._step_confirm(conn, op)
        if step == "release" and op["kind"] in ("release", "revoke", "resolve", "cancel"):
            return self._step_release(conn, op)
        if step == "withdraw_item" and op["kind"] in ("release", "revoke"):
            return self._step_withdraw_item(conn, op, prior_results["release"])
        raise DomainError("invalid_step", "未知的占用步骤 %s" % step, 500)

    # ------------------------------------------------------------- apply worker
    def start_apply(self, item_id, details):
        op = self._create_operation("apply", item_id, details["region"], details)
        return self.resume_occupancy(op["op_id"])

    def _step_apply_record(self, conn, op):
        details = op["details"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            region = details["region"]
            pool = self.get_pool(conn, region)
            capacity = int(pool["capacity"])
            item_row = conn.execute("SELECT * FROM items WHERE id=?", (op["item_id"],)).fetchone()
            if item_row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            # 恢复重放：上一轮记录已提交但步骤状态未及落账时，直接沿用，绝不重复占位。
            own = conn.execute(
                "SELECT * FROM occupancy WHERE op_id=?", (op["op_id"],)
            ).fetchone()
            if own is not None:
                conn.execute("COMMIT")
                return {"occupancy_id": own["id"], "state": own["state"], "seat": own["seat"]}
            existing = self.active_occupancy(conn, op["item_id"])
            if existing is not None:
                raise ConflictError(
                    "occupancy_active", "该事件已在占用账中（状态 %s）" % existing["state"],
                    details=self._occupancy_view(conn, dict(existing)),
                )
            occupied_count = conn.execute(
                "SELECT COUNT(*) AS total FROM occupancy "
                "WHERE region=? AND state IN ('held','confirmed','coordinating')",
                (region,),
            ).fetchone()["total"]
            seat_hint = details.get("seat")
            free_seats, taken = self._free_seats(conn, region, capacity)
            if occupied_count < capacity:
                if seat_hint:
                    if seat_hint in taken:
                        raise ConflictError(
                            "seat_taken", "席位 %s 已被其他事件占用" % seat_hint,
                            details=self._seat_holder(conn, region, seat_hint),
                        )
                    seat = seat_hint
                else:
                    seat = free_seats[0]
                state = "held"
            else:
                # 满员：无论是否指定席位都进入候补，按紧急等级、同级按提交先后排队。
                state = "waiting"
                seat = None
            urgency = details["urgency"]
            try:
                cursor = conn.execute(
                    "INSERT INTO occupancy(op_id,item_id,region,seat,state,urgency,urgency_rank,authorization_code,"
                    "expected_release_at,applied_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (op["op_id"], op["item_id"], region, seat, state, urgency, URGENCY_RANK[urgency],
                     details["authorization_code"], details.get("expected_release_at"), now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                message = str(exc)
                if "authorization" in message:
                    raise ConflictError("authorization_in_use", "停用授权已被其他事件占用，不能重复登记")
                if "seat" in message:
                    raise ConflictError(
                        "seat_taken", "席位已被其他事件占用",
                        details=self._seat_holder(conn, region, seat_hint),
                    )
                raise ConflictError("occupancy_active", "该事件已有有效占用记录")
            occupancy_id = cursor.lastrowid
            self._maybe_fault("apply:record")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return {"occupancy_id": occupancy_id, "state": state, "seat": seat}

    def _seat_holder(self, conn, region, seat):
        row = conn.execute(
            "SELECT o.*, i.payload AS item_payload FROM occupancy o JOIN items i ON i.id=o.item_id "
            "WHERE o.region=? AND o.seat=? AND o.state IN ('held','confirmed','coordinating')",
            (region, seat),
        ).fetchone()
        if row is None:
            return None
        holder = self._occupancy_view(conn, {k: row[k] for k in row.keys() if k != "item_payload"})
        holder["item_id"] = row["item_id"]
        try:
            payload = json.loads(row["item_payload"])
            holder["station_id"] = payload.get("station_id")
        except (ValueError, TypeError):
            pass
        return holder

    # ----------------------------------------------------------- confirm worker
    def start_confirm(self, item_id, details):
        conn = self.connect()
        try:
            row = self.active_occupancy(conn, item_id)
            region = row["region"] if row is not None else details.get("region")
        finally:
            conn.close()
        op = self._create_operation("confirm", item_id, region, details)
        return self.resume_occupancy(op["op_id"])

    def _step_confirm(self, conn, op):
        details = op["details"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            ledger_row = self.active_occupancy(conn, op["item_id"])
            if ledger_row is None:
                raise DomainError("occupancy_missing", "该事件没有占用记录，请先申请协调席位", 409)
            ledger = dict(ledger_row)
            if ledger["state"] == "waiting":
                raise ConflictError(
                    "still_waiting", "事件仍在候补队列中，尚不能确认",
                    details=self._occupancy_view(conn, ledger),
                )
            if ledger["state"] in ("confirmed", "coordinating"):
                # 可能是本操作上一轮已提交确认、步骤未落账；沿用既有结果，绝不重复审计。
                if ledger.get("confirmed_op_id") == op["op_id"]:
                    conn.execute("COMMIT")
                    return {"occupancy_id": ledger["id"], "seat": ledger["seat"], "state": ledger["state"]}
                raise ConflictError(
                    "already_confirmed", "席位已经确认，授权已生效",
                    details=self._occupancy_view(conn, ledger),
                )
            target_seat = details.get("seat") or ledger["seat"]
            holder = conn.execute(
                "SELECT * FROM occupancy WHERE region=? AND seat=? "
                "AND state IN ('held','confirmed','coordinating') AND id<>?",
                (ledger["region"], target_seat, ledger["id"]),
            ).fetchone()
            if holder is not None:
                raise ConflictError(
                    "seat_taken", "席位 %s 刚被其他事件确认，请重新读取占用账" % target_seat,
                    details=self._seat_holder(conn, ledger["region"], target_seat),
                )
            try:
                conn.execute(
                    "UPDATE occupancy SET state='confirmed', seat=?, confirmed_at=?, confirmed_op_id=?, "
                    "version=version+1 WHERE id=?",
                    (target_seat, now_iso(), op["op_id"], ledger["id"]),
                )
            except sqlite3.IntegrityError:
                raise ConflictError(
                    "seat_taken", "席位 %s 确认冲突，只有一方成功" % target_seat,
                    details=self._seat_holder(conn, ledger["region"], target_seat),
                )
            conn.execute(
                "UPDATE items SET status='suspended', version=version+1, updated_at=? WHERE id=?",
                (now_iso(), op["item_id"]),
            )
            self.append_audit(
                conn, op["item_id"], "occupancy_confirmed",
                details["actor"], details["role"],
                {"occupancy_id": ledger["id"], "seat": target_seat,
                 "authorization_code": ledger["authorization_code"]},
                idempotency_key="confirm:%d" % ledger["id"],
            )
            self._maybe_fault("confirm:confirm")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        self._maybe_fault("confirm:after_commit")
        return {"occupancy_id": ledger["id"], "seat": target_seat, "state": "confirmed"}

    # ------------------------------------------------------------- release worker
    def start_release(self, kind, item_id, region, details):
        op = self._create_operation(kind, item_id, region, details)
        return self.resume_occupancy(op["op_id"])

    def _step_release(self, conn, op):
        details = op["details"]
        terminal = {"release": "released", "revoke": "revoked",
                    "resolve": "released", "cancel": "released"}[op["kind"]]
        conn.execute("BEGIN IMMEDIATE")
        try:
            own_row = conn.execute(
                "SELECT * FROM occupancy WHERE released_op_id=?", (op["op_id"],)
            ).fetchone()
            if own_row is not None and own_row["state"] in ("released", "revoked"):
                # 释放已提交、步骤未落账的重放：沿用既有结果，不重复提补、不重复审计。
                conn.execute("COMMIT")
                return {"state": own_row["state"], "released": True,
                        "occupancy_id": own_row["id"], "seat": own_row["seat"], "promoted": []}
            ledger_row = self.active_occupancy(conn, op["item_id"])
            if ledger_row is None:
                # 事件本就没有活动占用：视为成功，且不动其他事件。
                conn.execute("COMMIT")
                return {"state": "inactive", "released": False, "promoted": []}
            ledger = dict(ledger_row)
            conn.execute(
                "UPDATE occupancy SET state=?, released_at=?, release_reason=?, released_op_id=?, version=version+1 WHERE id=?",
                (terminal, now_iso(), details.get("reason", ""), op["op_id"], ledger["id"]),
            )
            self.append_audit(
                conn, op["item_id"], "occupancy_%s" % op["kind"],
                details["actor"], details["role"],
                {"occupancy_id": ledger["id"], "seat": ledger["seat"],
                 "state": terminal, "reason": details.get("reason", ""),
                 "authorization_code": ledger["authorization_code"]},
                idempotency_key="%s:%d" % (op["kind"], ledger["id"]),
            )
            promoted = self._promote_waiting(conn, ledger["region"], details["actor"], details["role"])
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        # 模拟提交后、步骤落账前崩溃：重试必须识别已提交的释放，不重复占位/审计。
        self._maybe_fault("release:after_commit")
        return {"state": terminal, "released": True, "occupancy_id": ledger["id"],
                "seat": ledger["seat"], "promoted": promoted}

    def _promote_waiting(self, conn, region, actor, role):
        """释放后按紧急等级、同级按提交先后把候补事件提到 held，直到席位再次占满。"""
        pool = self.get_pool(conn, region)
        capacity = int(pool["capacity"])
        free_seats, _ = self._free_seats(conn, region, capacity)
        promoted = []
        waiting_rows = conn.execute(
            "SELECT * FROM occupancy WHERE region=? AND state='waiting' "
            "ORDER BY urgency_rank, applied_at, id",
            (region,),
        ).fetchall()
        for waiting in waiting_rows:
            if not free_seats:
                break
            seat = free_seats.pop(0)
            conn.execute(
                "UPDATE occupancy SET state='held', seat=?, version=version+1 WHERE id=?",
                (seat, waiting["id"]),
            )
            self.append_audit(
                conn, waiting["item_id"], "occupancy_promoted", actor, role,
                {"occupancy_id": waiting["id"], "seat": seat, "from_state": "waiting"},
                idempotency_key="promote:%d" % waiting["id"],
            )
            promoted.append({"occupancy_id": waiting["id"], "item_id": waiting["item_id"], "seat": seat})
        return promoted

    def _step_withdraw_item(self, conn, op, release_result):
        """撤回本事件写入的停用授权：仅当事件仍停在 suspended 时退回到 located。"""
        details = op["details"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            item_row = conn.execute("SELECT status FROM items WHERE id=?", (op["item_id"],)).fetchone()
            if item_row is None:
                conn.execute("COMMIT")
                return {"item_status": "missing"}
            status = item_row["status"]
            if status == "suspended":
                conn.execute(
                    "UPDATE items SET status='located', version=version+1, updated_at=? WHERE id=?",
                    (now_iso(), op["item_id"]),
                )
                self.append_audit(
                    conn, op["item_id"], "authorization_withdrawn",
                    details["actor"], details["role"],
                    {"reason": details.get("reason", ""), "kind": op["kind"]},
                    idempotency_key="withdraw:%d:%s" % (
                        release_result.get("occupancy_id") or 0, op["kind"]),
                )
                status = "located"
            self._maybe_fault("%s:withdraw_item" % op["kind"])
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return {"item_status": status}

    # --------------------------------------------------------------- snapshots
    def seats_overview(self, region=None):
        conn = self.connect()
        try:
            if region is None:
                regions = [row["region"] for row in conn.execute(
                    "SELECT DISTINCT region FROM seat_pools ORDER BY region").fetchall()]
                items_regions = [row["region"] for row in conn.execute(
                    "SELECT DISTINCT region FROM occupancy").fetchall()]
                regions = sorted(set(regions) | set(items_regions))
                return {"pools": [self._pool_snapshot(conn, name) for name in regions]}
            return self._pool_snapshot(conn, region)
        finally:
            conn.close()

    def _pool_snapshot(self, conn, region):
        pool = self.get_pool(conn, region)
        capacity = int(pool["capacity"])
        rows = conn.execute(
            "SELECT o.*, i.payload AS item_payload, i.status AS item_status FROM occupancy o "
            "JOIN items i ON i.id=o.item_id WHERE o.region=? "
            "ORDER BY CASE o.state WHEN 'held' THEN 0 WHEN 'confirmed' THEN 1 WHEN 'coordinating' THEN 2 "
            "WHEN 'waiting' THEN 3 ELSE 4 END, o.urgency_rank, o.applied_at, o.id",
            (region,),
        ).fetchall()
        active, waiting = [], []
        for row in rows:
            entry = self._occupancy_view(conn, {k: row[k] for k in row.keys() if k not in ("item_payload", "item_status")})
            entry["item_id"] = row["item_id"]
            entry["item_status"] = row["item_status"]
            try:
                payload = json.loads(row["item_payload"])
                entry["station_id"] = payload.get("station_id")
            except (ValueError, TypeError):
                entry["station_id"] = None
            if entry["state"] == "waiting":
                waiting.append(entry)
            elif entry["state"] in OCCUPIED_STATES:
                active.append(entry)
        earliest = self._earliest_release(conn, region)
        for index, entry in enumerate(waiting):
            entry["waiting_ahead"] = index
            reasons = ["席位已满（占用 %d/%d）" % (len(active), capacity)]
            if earliest:
                reasons.append("最早释放时间 %s" % earliest)
            reasons.append("前方候补 %d 人" % index)
            entry["blocked_reason"] = "；".join(reasons)
        return {
            "region": region,
            "capacity": capacity,
            "occupied": len(active),
            "remaining": max(0, capacity - len(active)),
            "earliest_release_at": earliest,
            "active": active,
            "waiting": waiting,
        }
