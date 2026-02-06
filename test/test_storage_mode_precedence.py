from core.settings import get_settings


def test_storage_mode_wins_over_storage_provider(monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "minio")
    monkeypatch.setenv("STORAGE_PROVIDER", "local")

    get_settings.cache_clear()
    s = get_settings()
    assert s.storage.provider == "minio"


def test_storage_provider_used_when_mode_missing(monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setenv("STORAGE_PROVIDER", "minio")

    get_settings.cache_clear()
    s = get_settings()
    assert s.storage.provider == "minio"


def test_storage_defaults_to_local(monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.delenv("STORAGE_PROVIDER", raising=False)

    get_settings.cache_clear()
    s = get_settings()
    assert s.storage.provider == "local"

