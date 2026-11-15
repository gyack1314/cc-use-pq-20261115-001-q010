import ast
from pathlib import Path


def check():
    root = Path(__file__).parent.parent
    files = sorted(root.glob("autumn_ledger/**/*.py")) + sorted(root.glob("tests/**/*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        compile(tree, str(path), "exec")
    from .api.server import create_app
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(Path(directory) / "check.sqlite3")
        specification = app.openapi()
        assert specification["paths"]
        assert "/editions/{identity}/publish" in specification["paths"]
    print(f"语法与接口模式检查通过：{len(files)}个Python文件")


if __name__ == "__main__":
    check()
