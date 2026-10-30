import pytest


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
