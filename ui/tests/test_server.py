from ui.server import _signal_provider


def test_signal_provider_resolves_demo_alias():
    assert _signal_provider("haiku", {"anthropic"}, {}) == "anthropic"


def test_signal_provider_accepts_direct_provider_key():
    assert _signal_provider("anthropic", {"anthropic", "openai"}, {}) == "anthropic"


def test_signal_provider_accepts_single_provider_cluster():
    clusters = {"billing": {"openai"}}
    assert _signal_provider("billing", {"anthropic", "openai"}, clusters) == "openai"


def test_signal_provider_rejects_mixed_provider_cluster():
    clusters = {"billing": {"anthropic", "openai"}}
    assert _signal_provider("billing", {"anthropic", "openai"}, clusters) is None
