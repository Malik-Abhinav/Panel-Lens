"""Shared translation selection for IPC and HTTP; changes require an idle pipeline."""
from __future__ import annotations

from functools import wraps
import json
import os
from pathlib import Path
import threading
import tempfile
import urllib.error
import urllib.request

_lock = threading.RLock()
_active = 0


def settings_path() -> Path:
    return Path(os.environ.get('PANELLENS_SETTINGS_PATH', Path.home() / '.config/panellens/translation.json'))


def saved_settings() -> dict:
    try:
        value = json.loads(settings_path().read_text())
        if not isinstance(value, dict) or value.get('provider') != 'ollama':
            return {}
        if not isinstance(value.get('model'), str) or not value['model'].strip():
            return {}
        return value
    except (OSError, ValueError):
        return {}


def pipeline_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        global _active
        # Control requests must remain responsive during loading/downloads.
        if args and isinstance(args[0], dict) and args[0].get('type') in {'ping', 'translation_settings', 'configure_translation'}:
            return function(*args, **kwargs)
        with _lock:
            _active += 1
        try:
            return function(*args, **kwargs)
        finally:
            with _lock:
                _active -= 1
    return wrapped


def installed_models() -> list[str] | None:
    import translation_pipeline as pipeline
    try:
        with urllib.request.urlopen(pipeline.OLLAMA_BASE_URL + '/api/tags', timeout=2) as response:
            payload = json.load(response)
            if isinstance(payload, dict) and isinstance(payload.get('models'), list):
                return [item['name'] for item in payload['models'] if isinstance(item, dict) and isinstance(item.get('name'), str)]
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return None
    return None


def describe() -> dict:
    import translation_pipeline as pipeline
    models = installed_models() or []
    return {'provider': pipeline.TRANSLATION_RUNTIME, 'model': pipeline.OLLAMA_MODEL,
            'providers': ['ollama'], 'ollama_models': models,
            'runtime': pipeline.translation_runtime_status()}


def configure(value: dict) -> dict:
    global _active
    import translation_pipeline as pipeline
    provider = value.get('provider')
    model = value.get('model', pipeline.OLLAMA_MODEL)
    if provider != 'ollama' or not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError('Choose an installed Ollama model or enter its name.')
    model = model.strip()
    models = installed_models()
    if models is None:
        raise ValueError('Cannot reach Ollama. Open Ollama and try again.')
    if model not in models:
        raise ValueError(f'Model {model!r} is not installed in Ollama. Choose a model from the installed list.')
    with _lock:
        if _active:
            raise ValueError('Translation or model loading is active. Stop reading and retry after it finishes.')
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'provider': provider, 'model': model}, stream)
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        pipeline.TRANSLATION_RUNTIME = provider
        pipeline.OLLAMA_MODEL = model
        pipeline._model_state = 'cold'
        pipeline._translation_cache.clear()
        # Reserve the operation before releasing the lock so selection cannot race warmup.
        _active += 1
        def prepare():
            global _active
            try:
                pipeline.warm_translation_model()
            finally:
                with _lock:
                    _active -= 1
        threading.Thread(target=prepare, daemon=True, name='selected-model-load').start()
    return {'provider': provider, 'model': model}
