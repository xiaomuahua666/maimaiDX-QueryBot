"""AWMC Gateway v2 响应信封解析回归测试。"""

import ast
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


PATH = Path(__file__).resolve().parent.parent / "libraries" / "maimaidx_sw_api.py"
tree = ast.parse(PATH.read_text(encoding="utf-8"))
selected = [
    node
    for node in tree.body
    if isinstance(node, ast.ClassDef) and node.name in {"SwApiError", "SwApiClient"}
]
namespace = {
    "Any": Any,
    "Dict": Dict,
    "List": List,
    "Optional": Optional,
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

try:
    client._parse_envelope(
        {"returnCode": 0, "returnMessage": "upstream rejected", "code": -1}
    )
except error as exc:
    assert "upstream rejected" in str(exc)
else:
    raise AssertionError("普通业务 returnCode=0 必须判定为失败")

print("sw api v2 envelope tests: ok")
