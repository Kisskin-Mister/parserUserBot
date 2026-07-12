import asyncio

from parser_engine import AI_SYSTEM_PROMPT, MatchResult, match_keywords, match_parser_config, parse_strict_bool


def test_keywords_any_matches_case_insensitive():
    result = match_keywords("Senior Python backend remote", ["python", "golang"])
    assert result.matched is True


def test_negative_keywords_override_positive():
    result = match_keywords("Python backend internship", ["python"], ["internship"])
    assert result.matched is False
    assert "negative" in result.reason


def test_keywords_all_policy_requires_every_keyword():
    assert match_keywords("Python backend remote", ["python", "remote"], policy="all").matched is True
    assert match_keywords("Python backend", ["python", "remote"], policy="all").matched is False


def test_regex_mode():
    result = match_keywords("salary 5000 usd", [r"salary\s+\d+"], regex=True)
    assert result.matched is True


def test_strict_bool_parser_accepts_only_exact_lowercase():
    assert parse_strict_bool("true") is True
    assert parse_strict_bool("false") is False
    assert parse_strict_bool("True") is None
    assert parse_strict_bool("true.") is None
    assert parse_strict_bool('"true"') is None


class FakeAI:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def classify(self, text, user_prompt, *, system_prompt=AI_SYSTEM_PROMPT):
        self.calls.append((text, user_prompt, system_prompt))
        return self.result


def test_ai_parser_config_true():
    parser = {
        "mode": "ai",
        "config": {"user_prompt": "remote backend jobs only"},
    }
    ai = FakeAI(MatchResult(True, reason="AI returned true"))
    result = asyncio.run(match_parser_config("Remote Go role", parser, ai))
    assert result.matched is True
    assert ai.calls[0][1] == "remote backend jobs only"


def test_ai_parser_config_invalid_without_classifier():
    parser = {"mode": "ai", "config": {"user_prompt": "anything"}}
    result = asyncio.run(match_parser_config("hello", parser, None))
    assert result.matched is False
    assert result.error == "ai_classifier is None"


def test_keyword_parser_config_shape():
    parser = {
        "mode": "keyword",
        "config": {
            "keywords": ["java", "go"],
            "negative_keywords": ["frontend"],
            "policy": "any",
        },
    }
    result = asyncio.run(match_parser_config("Remote Java vacancy", parser, None))
    assert result.matched is True
