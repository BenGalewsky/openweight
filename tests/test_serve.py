from pathlib import Path

import pytest

from openweight import serve
from openweight.sandbox import Forward


def test_dev_server_before_compose(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}\n")
    launch = serve.default_launch(tmp_path)
    assert launch.workdir == "/workspace/web"
    assert launch.forwards == [Forward(5173, 5173)]
    assert "--host 0.0.0.0 --port 5173" in launch.command[-1]


def test_compose_stack_once_there_is_one(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}\n")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    launch = serve.default_launch(tmp_path)
    assert launch.command == ["docker", "compose", "up", "--build"]
    assert launch.workdir == "/workspace"
    assert launch.forwards == [Forward(3000, 3000), Forward(9001, 9001)]


def test_nothing_to_serve(tmp_path: Path) -> None:
    with pytest.raises(serve.ServeError, match="pass a command"):
        serve.default_launch(tmp_path)
