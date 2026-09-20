from pathlib import Path

from scripts.check_venv_imports import check_packages


def write_package(root: Path, name: str) -> None:
    package = root / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("value = 1\n", encoding="utf-8")


def test_package_inside_venv_passes(tmp_path, monkeypatch, capsys):
    venv = tmp_path / "venv"
    site_packages = venv / "lib" / "python3.11" / "site-packages"
    write_package(site_packages, "fake_inside")
    monkeypatch.setattr("scripts.check_venv_imports.importlib.import_module", lambda name: type(
        "Module", (), {"__file__": str(site_packages / name / "__init__.py")}
    )())

    assert check_packages(venv, ["fake_inside"], []) == 0
    assert "fake_inside:" in capsys.readouterr().out


def test_package_outside_venv_fails_and_is_named(tmp_path, monkeypatch, capsys):
    venv = tmp_path / "venv"
    outside = tmp_path / "system" / "fake_outside" / "__init__.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr("scripts.check_venv_imports.importlib.import_module", lambda name: type(
        "Module", (), {"__file__": str(outside)}
    )())

    assert check_packages(venv, ["fake_outside"], []) == 1
    output = capsys.readouterr().out
    assert "fake_outside:" in output
    assert "Packages shadowing the venv" in output


def test_missing_optional_package_is_reported_without_failing(tmp_path, monkeypatch, capsys):
    def missing(_name):
        raise ModuleNotFoundError("missing")

    monkeypatch.setattr("scripts.check_venv_imports.importlib.import_module", missing)

    assert check_packages(tmp_path / "venv", ["fake_optional"], ["fake_optional"]) == 0
    assert "fake_optional: not installed" in capsys.readouterr().out
