from . import domain, rules
from .domain import DomainError, ConflictError


OCCUPANCY_ROLES = {"coordinator", "regulator"}
DEFAULT_EXPECTED_RELEASE_HOURS = 24


class Service:
    def __init__(self, repository):
        self.repository = repository

    # ------------------------------------------------------------------ items
    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        occupancy_state = None
        if action == "resolve":
            occupancy_state = "coordinating"
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload,
            expected_version, occupancy_state=occupancy_state,
        )
        result = self.get_item(item_id)
        # 结案/取消只释放本事件写入的席位与授权；账内其他事件不受影响。
        if action in ("resolve", "cancel"):
            release = self._release_after_lifecycle(
                item_id, action, actor, role, payload.get("reason", "")
            )
            result = self.get_item(item_id)
            result["occupancy_release"] = release
        return result

    def _release_after_lifecycle(self, item_id, kind, actor, role, reason):
        item = self.repository.get_item(item_id)
        details = {"actor": actor, "role": role, "reason": reason}
        try:
            return self.repository.start_release(
                kind, item_id, item["payload"].get("region"), details
            )
        except RuntimeError as exc:
            operation = self._find_open(item_id, kind)
            raise ConflictError(
                "occupancy_release_incomplete",
                "席位释放写入未完成，请按 operation 重试：%s" % (operation["op_id"] if operation else exc),
                details={"operation": operation, "reason": str(exc)},
            )

    def _find_open(self, item_id, kind=None):
        operations = self.repository.list_open_operations(item_id)
        if kind:
            operations = [op for op in operations if op["kind"] == kind]
        return operations[0] if operations else None

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        # 旧事件没有占用记录，按“未占用”读取。
        occupancy = self.repository.get_occupancy(item_id)
        item["occupancy"] = self._decorate_occupancy(occupancy)
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        summary = self.repository.state_summary()
        return summary

    # ------------------------------------------------------------- occupancy
    def _require_occupancy_identity(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in OCCUPANCY_ROLES:
            raise DomainError("forbidden", "只有协调员或监管员可以操作协调席位占用账", 403)

    def _require_region(self, item, role, region):
        if rules.ENFORCE_REGION and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能占用其他区域的协调席位", 403)

    def _decorate_occupancy(self, occupancy):
        if occupancy is None:
            return {"state": "unoccupied", "seat": None, "authorization_code": None}
        return occupancy

    def apply_occupancy(self, item_id, payload, actor, role, region=None):
        """申请：干扰事件、协调席位、停用授权共用占用账；满员则候补。"""
        self._require_occupancy_identity(actor, role)
        item = self.repository.get_item(item_id)
        if item["status"] != "located":
            raise DomainError("invalid_state", "只有完成定位（located）的事件才能申请协调席位", 409)
        self._require_region(item, role, region)
        normalized = domain.normalize_apply(payload)
        urgency = domain.urgency_of(item["payload"])
        expected_release_at = normalized["expected_release_at"] or self._default_release_at()
        details = {
            "authorization_code": normalized["authorization_code"],
            "seat": normalized["seat"],
            "expected_release_at": expected_release_at,
            "urgency": urgency,
            "region": item["payload"]["region"],
            "actor": actor,
            "role": role,
        }
        try:
            view = self.repository.start_apply(item_id, details)
        except ConflictError:
            raise
        except RuntimeError as exc:
            operation = self._find_open(item_id, "apply")
            raise ConflictError(
                "occupancy_apply_incomplete",
                "占用账写入未完成，请按 operation 重试：%s" % (operation["op_id"] if operation else exc),
                details={"operation": operation, "reason": str(exc)},
            )
        return view

    def confirm_occupancy(self, item_id, payload, actor, role, region=None, expected_version=None):
        """确认：两人同时确认同一席位，只有一人成功；确认后停用授权才生效。"""
        self._require_occupancy_identity(actor, role)
        item = self.repository.get_item(item_id)
        self._require_region(item, role, region)
        if expected_version is not None and int(expected_version) != int(item["version"]):
            raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
        normalized = domain.normalize_confirm(payload)
        details = {"seat": normalized["seat"], "actor": actor, "role": role}
        try:
            return self.repository.start_confirm(item_id, details)
        except ConflictError:
            raise
        except RuntimeError as exc:
            operation = self._find_open(item_id, "confirm")
            raise ConflictError(
                "occupancy_confirm_incomplete",
                "确认写入未完成，请按 operation 重试：%s" % (operation["op_id"] if operation else exc),
                details={"operation": operation, "reason": str(exc)},
            )

    def release_occupancy(self, item_id, payload, actor, role, region=None):
        """释放：只撤回该事件写入的席位和授权，其余事件继续沿用，并顺次提补候补。"""
        return self._withdraw("release", item_id, payload, actor, role, region)

    def revoke_occupancy(self, item_id, payload, actor, role, region=None):
        """事后发现越权：只撤销该事件的席位与授权，可回退其事件状态，不动其他事件。"""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role != "regulator":
            raise DomainError("forbidden", "只有监管员可以认定越权并撤回授权", 403)
        return self._withdraw("revoke", item_id, payload, actor, role, region)

    def _withdraw(self, kind, item_id, payload, actor, role, region):
        self._require_occupancy_identity(actor, role)
        item = self.repository.get_item(item_id)
        self._require_region(item, role, region)
        occupancy = self.repository.get_occupancy(item_id)
        if occupancy is None:
            raise DomainError("occupancy_missing", "该事件没有占用记录，无可撤回的席位或授权", 409)
        reason = str(payload.get("reason", "")).strip()
        if not reason:
            raise DomainError("field_required", "撤回必须给出原因")
        details = {"actor": actor, "role": role, "reason": reason}
        try:
            return self.repository.start_release(
                kind, item_id, item["payload"].get("region"), details
            )
        except RuntimeError as exc:
            operation = self._find_open(item_id, kind)
            raise ConflictError(
                "occupancy_release_incomplete",
                "撤回写入未完成，请按 operation 重试：%s" % (operation["op_id"] if operation else exc),
                details={"operation": operation, "reason": str(exc)},
            )

    def resume_occupancy(self, op_id, actor, role, region=None):
        """重跑未完成操作；已完成步骤不重复占位、不重复审计。"""
        self._require_occupancy_identity(actor, role)
        operation = self.repository.get_operation(op_id)
        if operation["state"] == "succeeded":
            return operation
        if region and role != "regulator":
            if operation.get("region") and operation["region"] != region:
                raise DomainError("region_mismatch", "不能继续其他区域的占用操作", 403)
        return self.repository.resume_occupancy(op_id)

    def get_occupancy(self, item_id):
        self.repository.get_item(item_id)
        return self._decorate_occupancy(self.repository.get_occupancy(item_id))

    def seats_overview(self, region=None):
        return self.repository.seats_overview(region)

    def set_pool_capacity(self, region, capacity, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role != "regulator":
            raise DomainError("forbidden", "只有监管员可以调整协调席位容量", 403)
        capacity = int(capacity)
        if capacity < 0:
            raise DomainError("invalid_number", "席位容量不能小于 0")
        self.repository.set_pool_capacity(region, capacity, actor)
        return self.repository.seats_overview(region)

    def _default_release_at(self):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) + timedelta(hours=DEFAULT_EXPECTED_RELEASE_HOURS)).isoformat()
