import ast
import importlib.util
from pathlib import Path
import sys

import pytest


PACKAGE = Path(__file__).resolve().parents[1] / "stockroom"
MODULES = sorted(PACKAGE.rglob("*.py"))


@pytest.mark.parametrize("path", MODULES, ids=lambda path: str(path.relative_to(PACKAGE)))
def test_imports_are_package_or_standard_library(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(("stockroom", *path.relative_to(PACKAGE).parts[:-1]))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                modules = [
                    importlib.util.resolve_name(
                        "." * node.level + (node.module or ""), package
                    )
                ]
            else:
                modules = [node.module]
        else:
            continue
        for module in modules:
            root = module.split(".")[0]
            assert root == "stockroom" or root in sys.stdlib_module_names, (
                f"{path.relative_to(PACKAGE)}:{node.lineno}: "
                f"non-standard-library import {module}"
            )
