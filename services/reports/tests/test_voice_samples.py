"""Structural sanity checks for the 5 synthetic voice-sample fixtures (one per
language, see docs/KNOWN_LIMITATIONS.md). These are NOT real audio/STT/TTS —
each fixture is a hand-written text conversation_log demonstrating one of the
4 non-MCP scenario types. This test only proves the fixtures parse and are
shaped correctly; it makes no claim about a working voice agent.
"""

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "voice_samples"
ALLOWED_SCENARIOS = {"switching", "interruption", "clarification", "wrong_identity"}


def _load_all():
    paths = sorted(FIXTURES_DIR.glob("*.json"))
    return [(p.stem, json.loads(p.read_text(encoding="utf-8"))) for p in paths]


@pytest.mark.parametrize("name,sample", _load_all())
def test_voice_sample_is_well_formed(name, sample):
    assert sample["simulated"] is True
    assert sample["scenario"] in ALLOWED_SCENARIOS
    assert sample["language"]
    log = sample["conversation_log"]
    assert isinstance(log, list) and len(log) >= 2
    roles = {turn["role"] for turn in log}
    assert "assistant" in roles
    assert "user" in roles
    for turn in log:
        assert turn["content"].strip()
        assert turn["timestamp"]


def test_all_five_languages_present():
    samples = _load_all()
    assert len(samples) == 5
    languages = {sample["language"] for _, sample in samples}
    assert languages == {"en-IN", "hi-IN", "ta-IN", "te-IN", "kn-IN"}


def test_all_four_core_scenarios_covered():
    _, samples = zip(*_load_all())
    scenarios = {sample["scenario"] for sample in samples}
    assert scenarios == ALLOWED_SCENARIOS
