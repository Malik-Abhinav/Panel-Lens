import pytest

import translation_pipeline as pipeline
import translation_settings as settings


def test_uninstalled_model_does_not_replace_working_selection(monkeypatch, tmp_path):
    monkeypatch.setenv('PANELLENS_SETTINGS_PATH', str(tmp_path / 'selection.json'))
    monkeypatch.setattr(settings, 'installed_models', lambda: ['installed-model'])
    monkeypatch.setattr(pipeline, 'OLLAMA_MODEL', 'installed-model')

    with pytest.raises(ValueError, match='not installed'):
        settings.configure({'provider': 'ollama', 'model': 'e'})

    assert pipeline.OLLAMA_MODEL == 'installed-model'
    assert not settings.settings_path().exists()


def test_offline_ollama_rejects_model_change(monkeypatch, tmp_path):
    monkeypatch.setenv('PANELLENS_SETTINGS_PATH', str(tmp_path / 'selection.json'))
    monkeypatch.setattr(settings, 'installed_models', lambda: None)

    with pytest.raises(ValueError, match='Cannot reach Ollama'):
        settings.configure({'provider': 'ollama', 'model': 'installed-model'})

    assert not settings.settings_path().exists()
