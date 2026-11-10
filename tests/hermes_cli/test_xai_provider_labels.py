"""Regression tests for xAI provider label disambiguation."""

import pytest

from hermes_cli.models import CANONICAL_PROVIDERS
from hermes_cli.providers import get_label, normalize_provider


@pytest.fixture
def no_models_dev_catalog(monkeypatch):
    """Hermetic: labels must come from Hermes' own data, not a live models.dev fetch."""
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})


def test_xai_oauth_provider_label_is_not_collapsed_to_api_key_label(no_models_dev_catalog):
    """The model picker must distinguish xAI API-key and OAuth providers."""
    assert get_label("xai") == "xAI"
    assert get_label("xai-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"
    assert get_label("grok-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"


def test_curated_providers_never_collapse_to_bare_id_offline(no_models_dev_catalog):
    """Whenever CANONICAL_PROVIDERS carries a human label, get_label must not collapse.

    Auto-extended plugin profiles without ``display_name`` are registered
    with ``label == slug`` (hermes_cli/models.py deliberately tolerates
    that), so there is nothing curated to assert for them; they are skipped
    rather than forced to grow a display name here.
    """
    checked = 0
    for entry in CANONICAL_PROVIDERS:
        if entry.label == entry.slug:
            continue  # plugin profile without display_name: nothing curated to assert
        checked += 1
        label = get_label(entry.slug)
        assert label not in {entry.slug, normalize_provider(entry.slug)}, (
            f"{entry.slug!r} has no human-readable label offline — add display_name "
            "to its ProviderProfile or a CANONICAL_PROVIDERS/_LABEL_OVERRIDES entry"
        )
    assert checked > 0
