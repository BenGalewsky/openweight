from pathlib import Path

import pytest

from openweight import branches
from openweight.branches import git


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--docker", action="store_true", help="Also run tests that start sandbox containers."
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--docker"):
        return
    skip = pytest.mark.skip(reason="starts containers; run with --docker")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A main repository with the benchmark inputs in place."""
    repo = tmp_path / "main"
    files = {
        "plans/ptd_review_window.md": "# PTD\n",
        "plans/samples-redesign.md": "# Samples\n",
        "db/access_db_schema.sql": "CREATE TABLE t (id int);\n",
        "data/DIVER.CSV": "a,b\n",
        "data/VuSitu.html": "<html></html>\n",
        "src/openweight/cli.py": "# framework code\n",
    }
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    for name in branches.ACCESS_DBS:
        (repo / "db" / name).write_bytes(b"access " + name.encode())
        (repo / "db" / name).chmod(0o600)
    (repo / ".gitignore").write_text("*.accdb\nworkspaces/\n")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "framework")
    # Untracked files in plans/ and data/ are not inputs.
    (repo / "plans" / "scratch.md").write_text("draft\n")
    (repo / "data" / "Projects.json").write_text("{}\n")
    return repo
