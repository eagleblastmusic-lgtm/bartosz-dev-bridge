import ast
import pathlib
import pytest


def test_no_duplicate_test_functions() -> None:
    test_dir = pathlib.Path(__file__).parent
    errors = []

    for path in test_dir.glob("test_*.py"):
        if path.name == "test_no_duplicate_tests.py":
            continue
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(path))

        seen = set()
        duplicates = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    if node.name in seen:
                        duplicates.append(node.name)
                    else:
                        seen.add(node.name)
        if duplicates:
            errors.append(f"Module {path.name} contains duplicate test names: {duplicates}")

    if errors:
        pytest.fail("\n".join(errors))
