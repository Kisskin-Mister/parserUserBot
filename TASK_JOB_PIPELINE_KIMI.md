# Задача Kimi: AI-controlled vacancy pipeline + n8n + browser worker

## Контекст

Работай в существующем репозитории `/mnt/ssd/projects/parserUserBot`.

Это действующий Pyrogram user-bot с FastAPI, PostgreSQL, API-managed parser configs, keyword/AI forwarding и тестами. Существующие незакоммиченные изменения в `docker-compose.yml`, `main.py`, `requirements.txt`, `examples/java-go-ai-parser.json` принадлежат пользователю. НЕ откатывай, НЕ перезаписывай и НЕ теряй их. Не меняй `.env`, Telegram session, секреты и production credentials. Не делай git commit/push. Не отправляй реальные отклики и не запускай production browser submission.

Перед изменениями прочитай README.md, database.py, api.py, parser_engine.py, main.py, tests, docker-compose.yml и текущий git diff.

## Цель

Расширить проект до рабочего MVP конвейера вакансий:

1. Существующий user-bot продолжает находить сообщения и пересылать их в Telegram-канал.
2. n8n Telegram Trigger получает пост из этого канала и вызывает ingestion endpoint parserUserBot.
3. Все события идемпотентно сохраняются в raw intake, но рабочая очередь не засоряется: правила быстро отсекают очевидный мусор, а все правдоподобные вакансии проходят обязательную AI-оценку в строгом JSON.
4. AI извлекает вакансию, оценивает пригодность Nazar и ставит verdict `qualified`, `garbage` или `needs_review` с краткой причиной и confidence.
5. Отдельный browser worker забирает только `approved_for_browser`, открывает официальный URL, получает фактический текст страницы и повторно оценивает вакансию через AI. Если вакансия плохая/закрытая/не remote — помечает её и не заполняет форму.
6. Если вакансия подходит, browser worker сопоставляет DOM-поля формы с детерминированным профилем, заполняет форму, загружает точное резюме и останавливается до финального submit.
7. При CAPTCHA создаётся human checkpoint со screenshot/URL. Человек решает CAPTCHA в той же persistent browser session. Автоматический solver и обход CAPTCHA не реализовывать.
8. После точного approval для `application_id + payload_hash + resume_sha256` browser worker имеет право нажать submit ровно один раз и сохранить доказательство результата.
9. В таблице явно видны `is_submitted`, status, время, confirmation и ошибка.

## Критерии Nazar

- Только Java/Go backend: Java Developer, Go/Golang Developer, Backend Developer с Java/Go как основной технологией.
- Только полностью удалённая работа. Hybrid/office/relocation-only — garbage/rejected.
- Уровень middle+/senior.
- Минимум 300 000 RUB; комфорт 350 000 RUB.
- Скрытая зарплата не отклоняется автоматически, но получает risk flag и обычно `needs_review` либо qualified при сильном совпадении.
- Иннотех не отправлять автоматически.
- Не выдумывать опыт или поля кандидата.
- Реальная отправка всегда требует exact approval.

## Архитектура MVP

Сохрани PostgreSQL как текущий storage. Не вводи SQLite и не требуй Redis/Celery.

Используй DB-backed queues с lease locking (`locked_by`, `locked_until`, `next_attempt_at`) и атомарным claim через PostgreSQL `FOR UPDATE SKIP LOCKED` либо эквивалентную транзакцию.

Предпочтительно добавить отдельные модули, а не раздувать `database.py` и `api.py`:

```text
vacancy_pipeline/
  models.py
  schemas.py
  repository.py
  rules.py
  ai_judge.py
  service.py
  worker.py
browser_worker/
  worker.py
  evaluator.py
  form_mapper.py
  captcha.py
  adapters/
    base.py
    generic.py
n8n/
  telegram-vacancy-ingest.json
  README.md
scripts/
  run_vacancy_worker.py
  run_browser_worker.py
```

Можно выбрать немного иную структуру, если она лучше соответствует текущему проекту.

## Схема данных

Создай безопасные idempotent migrations через текущий `Database.init()` либо маленький migration module, совместимый с текущим стилем.

Минимальные сущности:

### vacancy_intake

- id BIGSERIAL PK
- source_type (`telegram`, `hh`, `habr`, `getmatch`, `careerspace`, `direct`)
- source_chat_id nullable
- source_message_id nullable
- source_channel nullable
- message_url nullable
- raw_text
- raw_payload JSONB
- idempotency_key UNIQUE
- content_hash
- processing_status (`pending`, `processing`, `completed`, `failed`)
- created_at, updated_at

### job_vacancies

- id BIGSERIAL PK
- intake_id FK
- company, title
- canonical_url nullable
- official_apply_url nullable
- description_text
- remote_type (`remote_only`, `hybrid`, `office`, `unknown`)
- salary_min_rub nullable
- salary_max_rub nullable
- salary_hidden bool
- stack_tags JSONB
- seniority nullable
- ai_verdict (`qualified`, `garbage`, `needs_review`)
- ai_confidence numeric
- ai_score numeric
- ai_reason TEXT
- rule_reason TEXT
- state
- is_submitted BOOLEAN default false
- submitted_at nullable
- external_confirmation nullable
- created_at, updated_at

### applications

- id BIGSERIAL PK
- vacancy_id FK and UNIQUE for active candidate/profile
- state
- resume_path
- resume_sha256
- form_payload JSONB
- form_payload_hash
- cover_letter nullable
- approved_payload_hash nullable
- approved_at nullable
- is_submitted BOOLEAN default false
- submitted_at nullable
- external_confirmation nullable
- last_error nullable
- retry_count
- locked_by, locked_until, next_attempt_at
- created_at, updated_at

### application_events

Append-only audit log: application_id, old_state, new_state, actor, event_type, metadata JSONB, created_at.

### captcha_checkpoints

application_id, state (`waiting_human`, `resolved`, `aborted`), page_url, screenshot_path, persistent_session_key, instructions, created_at, resolved_at.

### submission_proofs

application_id UNIQUE, before/after screenshot paths, confirmation_text, confirmation_url, provider_application_id, uploaded_resume_sha256, submitted_payload_hash, proof_hash, created_at.

### llm_calls

purpose, vacancy_id/application_id, model, prompt_hash, input_hash, cache_hit, input/output token counts where provider returns them, response JSON, created_at. Cache completed calls by purpose + model + input hash.

Идемпотентность:

- Telegram: `telegram:{chat_id}:{message_id}`.
- Content hash — дополнительная дедупликация.
- Application: unique vacancy/profile and `sha256(vacancy + resume_sha256 + payload_hash)`.
- После состояния `submitting` любая неясная ошибка должна переводить в `needs_manual_verification`, а не повторно нажимать Submit.

## Ingestion API для n8n

Добавь защищённый endpoint, например:

`POST /vacancy-events`

Authorization: существующий Bearer ADMIN_TOKEN.

Request JSON:

```json
{
  "event_type": "telegram_vacancy_post",
  "idempotency_key": "telegram:-100123:456",
  "source": {
    "chat_id": "-100123",
    "message_id": "456",
    "channel": "channel_name",
    "message_url": "https://t.me/channel_name/456"
  },
  "post_text": "...",
  "attachments": [],
  "raw": {}
}
```

Response должен различать `created` и `duplicate`, возвращать intake id и status. Повтор одного события не создаёт вторую строку.

Добавь endpoints минимум для:

- list/filter vacancies;
- get vacancy;
- update human verdict/approval;
- create/prepare application;
- approve exact application payload;
- list CAPTCHA checkpoints;
- mark CAPTCHA resolved/aborted;
- inspect events/proofs.

Защити все mutating endpoints ADMIN_TOKEN. Не возвращай секреты и не логируй PII целиком.

## n8n workflow

Создай импортируемый `n8n/telegram-vacancy-ingest.json` без секретов.

Workflow:

```text
Telegram Trigger
→ Normalize forwarded post
→ POST /vacancy-events
→ IF duplicate: finish
→ otherwise finish with intake_id/status
```

Должны извлекаться chat_id, message_id, channel, permalink, post text/caption, attachments и стабильный idempotency key. URL API и token должны приходить из n8n credentials/env/placeholders, не быть зашиты реальными значениями.

Добавь `n8n/README.md` с точной инструкцией импорта, credential setup и тестовым payload через Manual Trigger/Webhook fallback.

## AI-контроль качества

Не использовать старый binary true/false classifier как единственный verdict. Добавь новый structured judge, совместимый с OpenAI-compatible API и текущими env:

- `OPENAI_BASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`

Ответ через строгую Pydantic schema/JSON parser, temperature 0:

```json
{
  "is_single_vacancy": true,
  "is_real_job": true,
  "target_role": "java_backend|go_backend|backend_other|not_target",
  "company": "...",
  "title": "...",
  "remote_type": "remote_only|hybrid|office|unknown",
  "salary_min_rub": null,
  "salary_max_rub": null,
  "salary_hidden": true,
  "seniority": "middle|middle_plus|senior|lead|unknown",
  "stack_tags": ["Java", "Spring"],
  "official_url": null,
  "verdict": "qualified|garbage|needs_review",
  "score": 0,
  "confidence": 0.0,
  "reason": "короткая конкретная причина",
  "risks": []
}
```

Правила:

1. Явный спам, дайджесты, несколько вакансий, курсы, новости и нерелевантные профессии можно помечать `garbage` детерминированно без LLM.
2. Любой пост, похожий на одну вакансию Java/Go/backend, обязательно проходит AI judge.
3. Provider/network/invalid JSON failure не превращается в garbage: ставить `needs_review`/retryable error.
4. Кэшировать AI verdict по normalized content hash.
5. Сохранять reason/confidence, чтобы было видно, почему вакансия признана мусором.
6. Рабочий list endpoint по умолчанию показывает qualified/needs_review, garbage доступен только явным фильтром.
7. Добавить token/call limits config, например `AI_MAX_CALLS_PER_BATCH`, `AI_MIN_RULE_CONFIDENCE`, `AI_DAILY_TOKEN_CAP` (реализовать хотя бы call cap + cache в MVP, не симулировать несуществующие provider данные).

## Browser evaluator/worker

Реализуй безопасный MVP через Playwright Python. Если зависимость тяжёлая для основного контейнера — создай отдельный optional compose profile/service и dependency file. Не ломай существующий worker/API startup.

Browser workflow:

1. Claim только application state `approved_for_browser`.
2. Проверить URL scheme http/https, блокировать localhost/private-network/неразрешённые схемы для данных из вакансии; поддержать configurable domain allowlist.
3. Открыть официальный URL в persistent context.
4. Извлечь title/main/body text и бизнес-признаки активности вакансии.
5. Повторно вызвать AI page evaluator в строгом JSON: active/closed, actual role, remote, salary, verdict/reason.
6. Если garbage/closed/not remote/not Java-Go backend — обновить vacancy/application state, записать event, не заполнять.
7. Найти форму. Generic adapter извлекает только labels, input types/names/placeholders/options, без передачи полного HTML модели.
8. Form mapper сопоставляет поля с allowlisted profile keys. Для стандартных полей сначала deterministic mapping; AI только для неизвестных labels и только JSON mapping.
9. Никогда не придумывать ответы. Неизвестный обязательный вопрос → `needs_human_answer`.
10. Upload только точного allowlisted resume path, проверить SHA-256.
11. До submit сделать screenshot + rendered form summary, read-back значений, form payload hash, state `ready_for_submit_approval`.
12. Submit разрешён только если `approved_payload_hash == current form_payload_hash`, resume hash совпадает, approval существует.
13. Submit один раз. После click собрать success markers, final URL, screenshot, confirmation text/ID и proof. Click без business confirmation не считается успехом.
14. По умолчанию browser submission disabled env-флагом, например `BROWSER_FINAL_SUBMIT_ENABLED=false`.

Тесты не должны выходить в интернет. Создай локальные HTML fixtures для:

- подходящей vacancy/form;
- closed vacancy;
- office-only vacancy;
- unknown required question;
- CAPTCHA page;
- success page.

## CAPTCHA: только human-in-the-loop

Не реализовывать 2Captcha/anti-captcha/solver API и не обходить CAPTCHA.

Detection минимум:

- recaptcha/hcaptcha iframe/class/src;
- Cloudflare/challenge markers;
- visible text prove you are human/проверка/капча;
- configurable selectors.

При обнаружении:

- сохранить screenshot;
- сохранить persistent session key и URL;
- создать captcha checkpoint `waiting_human`;
- application state `captcha_required`;
- не закрывать/не уничтожать session state без необходимости;
- endpoint позволяет человеку отметить `resolved` или `aborted`;
- после `resolved` worker обязан перепроверить отсутствие CAPTCHA, а не слепо продолжить.

Добавь документированный hook/notification payload для n8n/Telegram: application_id, company/title, URL, screenshot path, instructions. Реальную отправку Telegram можно не включать без credentials, но контракт и тест должны быть рабочими.

## Docker/config/docs

- Обнови `.env.example` placeholders, без секретов.
- Не меняй реальные `.env`.
- Сохрани текущий PostgreSQL compose.
- Добавь optional services/workers так, чтобы обычный `docker compose config` проходил.
- Обнови README с архитектурой, state machine, API examples, запуском vacancy worker/browser dry-run, n8n import, CAPTCHA flow и approval flow.
- Не заявляй, что browser production-ready для всех сайтов: generic MVP + adapter interface.

## Тестирование и качество

Работай TDD там, где возможно. Обязательные тесты:

1. idempotent Telegram ingestion;
2. duplicate returns same/no extra row;
3. obvious garbage rejected without LLM;
4. plausible job invokes structured AI judge;
5. malformed/provider failure → needs_review, не garbage;
6. AI cache avoids second call;
7. qualified list excludes garbage by default;
8. DB claim is single-consumer/idempotent;
9. payload hash invalidates stale approval;
10. no submit without exact approval;
11. unknown required form question stops;
12. CAPTCHA creates checkpoint and prevents submit;
13. ambiguous submit failure never auto-retries;
14. proof required before is_submitted=true;
15. n8n workflow JSON parses and contains no real secret.

Запусти и исправь:

```bash
python -m pytest -q
ruff check .
python -m compileall -q .
docker compose config
```

Если Playwright browser binary отсутствует, unit/integration tests должны использовать mocks/local parser fixtures и явно документировать отдельную install command; не скачивай гигабайтные browser images без необходимости.

## Результат

Не ограничивайся планом. Реализуй код и тесты в репозитории. В конце верни:

- список изменённых/добавленных файлов;
- краткое описание data flow;
- реальные команды и результаты тестов/lint/compile/compose;
- что реализовано полностью;
- честные blockers/что требует credentials или browser runtime;
- подтверждение, что реальные отклики не отправлялись и CAPTCHA не обходилась.
