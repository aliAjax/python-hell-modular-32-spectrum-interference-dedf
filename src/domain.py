from datetime import datetime


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class ConflictError(DomainError):
    def __init__(self, code, message, details=None):
        super().__init__(code, message, 409)
        self.details = details


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


URGENCY_LEVELS = ("critical", "high", "medium", "low")
URGENCY_RANK = {level: index for index, level in enumerate(URGENCY_LEVELS)}


def urgency_of(payload):
    """紧急等级取评估结果，未评估的事件按最低等级排队。"""
    assessment = payload.get("assessment") or {}
    level = assessment.get("level")
    return level if level in URGENCY_RANK else "low"


def normalize_apply(payload):
    authorization = require_text(payload, "authorization_code")
    if not authorization.startswith("REG-"):
        raise DomainError("invalid_authorization", "停用授权编号无效", 403)
    seat = payload.get("seat")
    if seat is not None:
        seat = str(seat).strip() or None
    expected_release_at = payload.get("expected_release_at")
    if expected_release_at is not None:
        if not isinstance(expected_release_at, str) or not expected_release_at.strip():
            raise DomainError("invalid_timestamp", "expected_release_at 必须是 ISO 时间")
        expected_release_at = expected_release_at.strip()
        try:
            datetime.fromisoformat(expected_release_at.replace("Z", "+00:00"))
        except ValueError:
            raise DomainError("invalid_timestamp", "expected_release_at 必须是 ISO 时间")
    return {"authorization_code": authorization, "seat": seat, "expected_release_at": expected_release_at}


def normalize_confirm(payload):
    seat = payload.get("seat")
    if seat is not None:
        seat = str(seat).strip() or None
    return {"seat": seat}
