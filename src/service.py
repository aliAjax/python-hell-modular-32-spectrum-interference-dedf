from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

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

        # 占用账阻塞：仍在候补的事件不能继续协调或结案。
        occupancy = self.repository.get_active_occupancy(item_id)
        if rules.is_waitlisted(occupancy) and action in {"coordinate", "resolve"}:
            raise DomainError(
                "seat_waitlisted",
                "仍在协调席位候补（第 %s 位，最早释放时间 %s），暂不能执行 %s"
                % (occupancy.get("position"), occupancy.get("earliest_release_at"), action),
                409,
            )

        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )

        seat = None
        if action == "suspend":
            # 停用申请即占用申请：按紧急等级优先、同级提交先后占位，满员候补。
            seat = self.repository.apply_seat(
                item_id,
                event_payload.get("authorization_code"),
                rules.urgency_level(new_payload),
                item["payload"].get("region"),
                actor,
                role,
            )
        elif action == "resolve":
            seat = self.repository.release_seat(item_id, actor, role)
        elif action == "cancel":
            seat = self.repository.withdraw_seat(item_id, actor, role)

        result = self.get_item(item_id)
        if seat is not None:
            result["seat"] = seat
        return result

    # ------------------------------------------------------------------
    # 协调席位（共用占用账）
    # ------------------------------------------------------------------

    def _require_seat_role(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in {"coordinator", "regulator"}:
            raise DomainError("forbidden", "当前角色不能操作协调席位", 403)

    def apply_seat(self, item_id, payload, actor, role, region=None):
        self._require_seat_role(actor, role)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_seat_apply(payload)
        return self.repository.apply_seat(
            item_id,
            normalized["authorization_code"],
            rules.urgency_level(item["payload"]),
            item["payload"].get("region"),
            actor,
            role,
        )

    def confirm_seat(self, item_id, actor, role):
        self._require_seat_role(actor, role)
        return self.repository.confirm_seat(item_id, actor, role)

    def withdraw_unauthorized(self, item_id, payload, actor, role):
        self._require_seat_role(actor, role)
        reason = (payload or {}).get("reason") or "unauthorized_occupancy"
        return self.repository.withdraw_unauthorized(item_id, actor, role, reason)

    def seat_occupancy(self, item_id):
        occupancy = self.repository.get_active_occupancy(item_id)
        return self._seat_view(occupancy)

    def seat_ledger(self):
        return self.repository.list_active_occupancy()

    def seat_state(self):
        return self.repository.seat_state()

    def _seat_view(self, occupancy):
        if occupancy is None:
            return {
                "status": "unoccupied",
                "seat_no": None,
                "urgency": None,
                "authorization_effective": False,
                "confirmed_at": None,
                "earliest_release_at": None,
                "position": None,
                "region": None,
            }
        position = occupancy.get("position")
        if occupancy.get("status") == "waitlisted" and position is None:
            position = self.repository.waitlist_position(occupancy["item_id"])
        return {
            "status": occupancy.get("status"),
            "seat_no": occupancy.get("seat_no"),
            "urgency": occupancy.get("urgency"),
            "authorization_effective": occupancy.get("authorization_effective", False),
            "confirmed_at": occupancy.get("confirmed_at"),
            "earliest_release_at": occupancy.get("earliest_release_at"),
            "position": position,
            "region": occupancy.get("region"),
        }

    def _blocking_reason(self, occupancy):
        if not occupancy:
            return None
        if occupancy.get("status") == "waitlisted":
            return "seat_waitlisted: 席位候补第 %s 位，最早释放时间 %s" % (
                occupancy.get("position") or self.repository.waitlist_position(occupancy["item_id"]),
                occupancy.get("earliest_release_at"),
            )
        return None

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        occupancy = self.repository.get_active_occupancy(item_id)
        item["seat"] = self._seat_view(occupancy)
        item["blocking_reason"] = self._blocking_reason(occupancy)
        return item

    def list_items(self, status=None):
        items = self.repository.list_items(status)
        for item in items:
            occupancy = self.repository.get_active_occupancy(item["id"])
            item["seat"] = self._seat_view(occupancy)
            item["blocking_reason"] = self._blocking_reason(occupancy)
        return items

    def state(self):
        return self.repository.state_summary()
