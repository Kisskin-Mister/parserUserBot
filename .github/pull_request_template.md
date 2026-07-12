## Summary

-
-

## Type of change

- [ ] Bug fix
- [ ] Feature
- [ ] Documentation
- [ ] CI / maintenance
- [ ] Security hardening

## Test plan

- [ ] `ruff check .`
- [ ] `python -m compileall -q -x '(^|/)(\.git|\.venv|\.venv-community|__pycache__|\.pytest_cache|\.ruff_cache)(/|$)' .`
- [ ] `python -m pytest -q`

## Safety checklist

- [ ] No real Telegram credentials, phone numbers, tokens, session files, DB passwords, logs, resumes, or private chat content are included.
- [ ] `.env.example` uses safe placeholders such as `your_api_hash`, `your_phone_number`, `change_me`.
- [ ] Documentation was updated for behavior/config changes.
