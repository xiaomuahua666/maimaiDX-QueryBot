"""Static regression check: score features must use the datasource adapter."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "maiApi.query_user_b50",
    "maiApi.query_user_get_dev",
    "maiApi.query_user_plate",
    "maiApi.query_user_post_dev",
)

# The adapter is the only layer allowed to know the upstream user-score APIs.
SCORE_DIRS = (ROOT / "command", ROOT / "libraries")
ADAPTER = ROOT / "libraries" / "maimaidx_datasource.py"


def main() -> None:
    violations: list[str] = []
    for directory in SCORE_DIRS:
        for path in directory.glob("*.py"):
            if path == ADAPTER:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                owner = func.value
                if not isinstance(owner, ast.Attribute) or not isinstance(owner.value, ast.Name):
                    continue
                name = f"{owner.value.id}.{owner.attr}_{func.attr}"
                if name in FORBIDDEN:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: {name}"
                    )
    if violations:
        raise SystemExit("direct score API calls found:\n" + "\n".join(violations))
    print("score datasource routing: ok")


if __name__ == "__main__":
    main()
