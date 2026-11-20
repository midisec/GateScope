#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden_domain = {"fastapi", "starlette", "sqlalchemy", "requests", "httpx", "alembic"}
    failures: list[str] = []
    for path in (root / "src/gatescope/domain").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".", 1)[0] in forbidden_domain:
                    failures.append(f"domain imports {name}: {path.relative_to(root)}")
    for dockerfile in [root / "Dockerfile", root / "deploy/images/api.Dockerfile"]:
        content = dockerfile.read_text(encoding="utf-8")
        if "COPY . ." in content or "gatescope_executor" in content:
            failures.append(f"unsafe API image boundary: {dockerfile.relative_to(root)}")
        user_lines = [line.split("#", 1)[0].strip() for line in content.splitlines() if line.strip().startswith("USER ")]
        if not user_lines or user_lines[-1] in {"USER 0", "USER 0:0", "USER root"}:
            failures.append(f"API image is not pinned to a non-root user: {dockerfile.relative_to(root)}")
    ignored = {line.strip().rstrip("/") for line in (root / ".dockerignore").read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")}
    for required in {".git", ".env", "data", "artifacts", "tests"}:
        if required not in ignored:
            failures.append(f".dockerignore misses {required}")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    for hardening in ["read_only: true", "no-new-privileges:true", "cap_drop:", "127.0.0.1:8777:8777"]:
        if hardening not in compose:
            failures.append(f"compose misses hardening: {hardening}")
    api_source = (root / "app.py").read_text(encoding="utf-8")
    for forbidden in ["SCANNER.start()", "OPPORTUNITY.start()"]:
        if forbidden in api_source:
            failures.append(f"Web API starts a forbidden in-process worker: {forbidden}")
    if failures:
        raise SystemExit("\n".join(failures))
    print("architecture boundaries passed")


if __name__ == "__main__":
    main()
