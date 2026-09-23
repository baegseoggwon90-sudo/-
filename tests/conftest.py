import pytest


@pytest.fixture(autouse=True)
def _isolated_user_config(tmp_path, monkeypatch):
    """테스트가 실제 사용자 설정(~/.kbroll)을 건드리지 않게 한다."""
    monkeypatch.setenv("KBROLL_CONFIG_DIR", str(tmp_path / "_kbroll_config"))
