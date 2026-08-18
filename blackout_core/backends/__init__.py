"""Concrete model backends.

Deliberately not imported by blackout_core/__init__.py or by this package's
own __init__: importing blackout_core (and running the chaos harness against
it) must not require the `openai` package or a live Ollama process. Import
the specific backend module you need, e.g.:

    from blackout_core.backends.ollama_backend import OllamaBackend
    from blackout_core.backends.openai_backend import OpenAIBackend
"""
