from datetime import datetime, timezone, timedelta


# 协调席位（共用占用账）的容量与占位时长。席位为全局共享资源，
# 干扰事件、协调席位与停用授权共用同一本占用账。
SEAT_CAPACITY = 3
SEAT_HOLD_SECONDS = 2 * 60 * 60

# 紧急等级优先级：数字越大越优先。同级按提交先后（占用账序号）占位。
URGENCY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def urgency_rank(level):
    if not isinstance(level, str):
        return URGENCY_RANK["low"]
    return URGENCY_RANK.get(level.strip().lower(), URGENCY_RANK["low"])


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class ConflictError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 409)


class NotFoundError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 404)


def require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def number(payload, name, minimum=None, maximum=None):
    value = payload.get(name)
    if isinstance(value, bool):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    if maximum is not None and value > maximum:
        raise DomainError("invalid_number", "%s 不能大于 %s" % (name, maximum))
    return value


def parse_timestamp(payload, name):
    value = require_text(payload, name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % name)
    return value


def normalize_create(payload):
    frequency = number(payload, "frequency_mhz", 0.001, 300000)
    bandwidth = number(payload, "bandwidth_mhz", 0.001)
    station_id = require_text(payload, "station_id")
    region = require_text(payload, "region")
    strength = number(payload, "strength_dbm")
    detected_at = parse_timestamp(payload, "detected_at")
    reporter = require_text(payload, "reporter")
    stable_key = "%s|%s|%s|%s" % (station_id, region, frequency, detected_at)
    return {
        "frequency_mhz": frequency,
        "bandwidth_mhz": bandwidth,
        "station_id": station_id,
        "region": region,
        "strength_dbm": strength,
        "detected_at": detected_at,
        "reporter": reporter,
        "measurement_revisions": [],
        "suspend_authorization": None,
        "_stable_key": stable_key,
    }


def hold_expires_at(occupied_iso, seconds=None):
    """占位到期时间：用于给候补事件估计最早释放时间。占位本身不会自动释放。"""
    seconds = SEAT_HOLD_SECONDS if seconds is None else seconds
    try:
        base = datetime.fromisoformat(occupied_iso.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        base = datetime.now(timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat()


def urgency_of(payload):
    """从事件 payload 的评估结果取紧急等级，返回 (rank, level)。"""
    level = None
    if isinstance(payload, dict):
        assessment = payload.get("assessment") or {}
        if isinstance(assessment, dict):
            level = assessment.get("level")
    rank = urgency_rank(level)
    name = level if isinstance(level, str) and level.strip() else "low"
    return rank, name


def normalize_seat_apply(payload):
    authorization = require_text(payload, "authorization_code")
    if not authorization.startswith("REG-"):
        raise DomainError("invalid_authorization", "停用授权编号无效", 403)
    return {"authorization_code": authorization}


def normalize_source(payload):
    source_type = require_text(payload, "source_type")
    external_id = require_text(payload, "external_id")
    observed_at = parse_timestamp(payload, "observed_at")
    strength = number(payload, "strength_dbm")
    region = payload.get("region")
    if region is not None:
        region = str(region).strip() or None
    return {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": observed_at,
        "strength_dbm": strength,
        "region": region,
        "station_id": payload.get("station_id"),
        "frequency_mhz": payload.get("frequency_mhz"),
    }
