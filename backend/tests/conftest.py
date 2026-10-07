import pytest


@pytest.fixture(autouse=True)
def prompts_file(tmp_path, monkeypatch):
    """Every test gets its own prompt overrides file, so none writes backend/prompts.json."""
    path = tmp_path / "prompts.json"
    monkeypatch.setenv("CYOA_PROMPTS", str(path))
    return path


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch):
    """Nor does any test write backend/settings.json, or inherit a model or gate size saved there."""
    monkeypatch.setenv("CYOA_SETTINGS", str(tmp_path / "settings.json"))
