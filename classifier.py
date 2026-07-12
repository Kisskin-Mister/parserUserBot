import re
from pathlib import Path


def load_keywords(file_path: str) -> list[str]:
    path = Path(file_path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [line.strip().lower() for line in f if line.strip() and not line.startswith("#")]


def _compile_word_pattern(words: list[str]) -> re.Pattern[str] | None:
    cleaned_words = [word.strip().lower() for word in words if word.strip()]
    if not cleaned_words:
        return None
    escaped = [re.escape(word) for word in cleaned_words]
    return re.compile(r"(?<!\w)(?:" + "|".join(escaped) + r")(?!\w)", re.IGNORECASE)


KEYWORDS_VACANCY = ["вакансия", "ищем", "job", "hiring", "стек", "зп", "salary", "remote", "удаленка"]
KEYWORDS_NEWS = load_keywords("keywords.txt")
NEGATIVE_KEYWORDS = load_keywords("negative_keywords.txt")
TECH_KEYWORDS = ["go", "golang", "java"]

VACANCY_CONTEXT_PATTERN = _compile_word_pattern(KEYWORDS_VACANCY)
NEWS_PATTERN = _compile_word_pattern(KEYWORDS_NEWS)
NEGATIVE_PATTERN = _compile_word_pattern(NEGATIVE_KEYWORDS)
GO_PATTERN = _compile_word_pattern(["go", "golang"])
JAVA_PATTERN = _compile_word_pattern(["java"])
TECH_PATTERN = _compile_word_pattern(TECH_KEYWORDS)


def classify_post(text: str):
    text_lower = text.lower()

    if NEGATIVE_PATTERN and NEGATIVE_PATTERN.search(text_lower):
        return None, None

    has_job_context = bool(VACANCY_CONTEXT_PATTERN and VACANCY_CONTEXT_PATTERN.search(text_lower))
    has_tech = bool(TECH_PATTERN and TECH_PATTERN.search(text_lower))
    if has_job_context and has_tech:
        if GO_PATTERN and GO_PATTERN.search(text_lower):
            return "vacancy", "go"
        if JAVA_PATTERN and JAVA_PATTERN.search(text_lower):
            return "vacancy", "java"

    if NEWS_PATTERN and NEWS_PATTERN.search(text_lower):
        return "news", None

    return None, None
