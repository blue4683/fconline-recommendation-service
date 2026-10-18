import importlib
import pkgutil

import FRS
from FRS.__main__ import main


def test_all_modules_import_without_network():
    for module in pkgutil.walk_packages(FRS.__path__, prefix="FRS."):
        importlib.import_module(module.name)


def test_environment_command_is_safe(capsys):
    main(["--check-environment"])
    assert "수집·DB 접속·학습 미실행" in capsys.readouterr().out
