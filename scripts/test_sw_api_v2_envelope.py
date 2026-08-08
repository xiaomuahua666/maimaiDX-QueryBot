"""AWMC Gateway v2 响应信封解析回归测试。"""

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


PATH = Path(__file__).resolve().parent.parent / "libraries" / "maimaidx_sw_api.py"
tree = ast.parse(PATH.read_text(encoding="utf-8"))
selected_names = {
    "SwApiError",
    "SwApiClient",
    "_is_business_success",
    "find_sw_api_error",
    "is_sw_api_quota_error",
    "format_sw_api_quota_error",
}
selected = [
    node
    for node in tree.body
    if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    and node.name in selected_names
]
namespace = {
    "Any": Any,
    "Dict": Dict,
    "List": List,
    "Optional": Optional,
    "datetime": datetime,
    "timedelta": timedelta,
    "timezone": timezone,
    "json": json,
}
exec(compile(ast.Module(body=selected, type_ignores=[]), str(PATH), "exec"), namespace)

client = namespace["SwApiClient"]
error = namespace["SwApiError"]

assert client._parse_envelope(
    {
        "returnCode": 1,
        "returnMessage": "ignored compatibility payload",
        "businessData": {"userId": 123},
        "code": 0,
    }
) == {"userId": 123}
assert client._parse_envelope(
    {"returnCode": 1, "returnMessage": '{"userName":"TEST"}', "code": 0}
) == {"userName": "TEST"}
assert client._parse_envelope({"code": 0, "msg": '{"legacy":true}'}) == {
    "legacy": True
}

assert client._is_business_success(
    {
        "returnCode": 1,
        "returnMessage": '{"returnCode":1,"apiName":"com.sega.maimai.api.UpsertUserAllApi"}',
        "code": 0,
        "msg": '{"returnCode":1,"apiName":"com.sega.maimai.api.UpsertUserAllApi"}',
        "businessData": {
            "returnCode": 1,
            "apiName": "com.sega.maimai.api.UpsertUserAllApi",
        },
    }
) is True
assert client._is_business_success(
    {"returnCode": 1, "returnMessage": "{}"}
) is True
assert client._is_business_success({"businessData": {"returnCode": 1}}) is True
assert client._is_business_success(
    {"code": 0, "msg": '{"returnCode":1,"apiName":"com.sega.maimai.api.UpsertUserAllApi"}'}
) is True
assert client._is_business_success({"returnCode": 0}) is False
assert client._is_business_success({"returnCode": 102}) is False
assert client._is_business_success({"code": 0, "msg": "not json"}) is False

try:
    client._parse_envelope(
        {"returnCode": 0, "returnMessage": "upstream rejected", "code": -1}
    )
except error as exc:
    assert "upstream rejected" in str(exc)
else:
    raise AssertionError("普通业务 returnCode=0 必须判定为失败")

quota_payload = {
    "error": "quota_exceeded",
    "msg": "Reached Personal 1-Hour Quota Limit for Read requests.",
    "retryAfterSeconds": 1800,
    "quota": {
        "scope": "Personal",
        "category": "read",
        "window": "1h",
        "windowLabel": "1-Hour",
        "limit": 120,
        "used": 118,
        "requested": 4,
        "resetAt": "2026-08-06T15:06:26.000Z",
        "retryAtBeijing": "2026-08-06 23:06:26",
        "timeZone": "Asia/Shanghai",
    },
}
try:
    client._parse_envelope(quota_payload)
except error as exc:
    assert exc.is_quota_exceeded
    assert exc.retry_after_seconds == 1800
    assert exc.retry_at == "2026-08-06T15:06:26.000Z"
    assert namespace["is_sw_api_quota_error"](exc)
    quota_text = namespace["format_sw_api_quota_error"](exc)
    assert "当前 118/120，本次需要 4" in quota_text
    assert "2026-08-06 23:06:26（北京时间）后继续使用" in quota_text
else:
    raise AssertionError("quota_exceeded 必须保留结构化配额信息")

print("sw api v2 envelope tests: ok")
