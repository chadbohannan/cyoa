import pytest


@pytest.fixture(autouse=True)
def prompts_file(tmp_path, monkeypatch):
    """Every test gets its own prompt overrides file, so none writes backend/prompts.json."""
    path = tmp_path / "prompts.json"
    monkeypatch.setenv("CYOA_PROMPTS", str(path))
    return path
