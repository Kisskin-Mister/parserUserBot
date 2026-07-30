# Telegram vacancy ingestion for n8n

[`telegram-vacancy-ingest.json`](telegram-vacancy-ingest.json) is an inactive,
importable n8n workflow with this data flow:

```text
Telegram Trigger (channel_post)
  -> Normalize Forwarded Post
  -> POST /vacancy-events
  -> IF Duplicate
       true  -> Duplicate - Finish
       false -> Created - Finish
```

The normalizer accepts Telegram `channel_post` and `message` updates (including
the `$json.body` wrapper produced by an n8n Webhook node). It uses the received
chat and message identifiers to build the stable key
`telegram:{chat_id}:{message_id}`. Text or caption, forwarded-origin metadata,
the permalink, and attachment metadata are preserved. Files are not downloaded.

## Prerequisites

- A Telegram Bot API token. Add that bot to the channel where parserUserBot
  forwards vacancies. Give it the minimum channel permissions needed to receive
  channel posts.
- A reachable parserUserBot API with `POST /vacancy-events` and `ADMIN_TOKEN`
  configured. Use HTTPS when n8n and the API communicate over an untrusted
  network.
- A self-hosted n8n release with the built-in Telegram Trigger, Code, HTTP
  Request, IF, and Edit Fields nodes. The export uses only built-in nodes.

Telegram permits one webhook per bot, so the same bot cannot back two active
Telegram Trigger workflows at once.

## Import and configure

1. In n8n, choose **Workflows -> Import from File** and select
   `n8n/telegram-vacancy-ingest.json`.
2. Open **Telegram Trigger**, create or select a **Telegram API** credential,
   and put the BotFather access token in that credential. The exported JSON
   contains no Telegram credential or token.
3. Optionally open **Additional Fields -> Restrict to Chat IDs** and enter the
   forwarding target channel ID, for example `-1001234567890`. This is strongly
   recommended when the bot is present in more than one chat.
4. Set `PARSERUSERBOT_BASE_URL` in the n8n service environment to the API origin,
   without the `/vacancy-events` suffix. Examples are `http://api:8000` when
   both services share a Docker network, or `https://parser.example.invalid` for
   a reverse-proxied deployment. Restart n8n after changing its environment.
5. Open **POST Vacancy Event**. Create or select an **HTTP Bearer Auth**
   credential and store only the parserUserBot `ADMIN_TOKEN` as its token. n8n
   supplies the `Authorization: Bearer ...` header. Do not include the word
   `Bearer` in the credential value.
6. Execute the workflow once in test mode and publish/activate it only after the
   API returns an `intake_id` and `status`.

If the n8n installation blocks `$env` access in expressions, replace the URL
expression in **POST Vacancy Event** with the reachable base URL plus
`/vacancy-events`. This URL is configuration, not a credential. Keep the admin
token in the n8n Bearer credential rather than in the node or workflow JSON.

The HTTP response may identify a duplicate with `status: "duplicate"`,
`duplicate: true`, or `created: false`; the IF node accepts all three forms.
Both terminal nodes expose `intake_id`, `status`, and a normalized `duplicate`
boolean.

## Normalized request

The HTTP node sends this contract:

```json
{
  "event_type": "telegram_vacancy_post",
  "idempotency_key": "telegram:-1001234567890:456",
  "source": {
    "chat_id": "-1001234567890",
    "message_id": "456",
    "channel": "vacancy_forward_target",
    "message_url": "https://t.me/vacancy_forward_target/456"
  },
  "post_text": "Senior Go backend, fully remote",
  "attachments": [],
  "raw": {
    "telegram_update": {},
    "normalized_forward": null
  }
}
```

The idempotency key intentionally refers to the received target-channel post.
Forwarded-source details remain in `raw.normalized_forward`. This keeps retries
stable even if the original post is private or has no public username.

## Test the API directly

Set shell variables locally; do not commit their values:

```bash
export PARSERUSERBOT_BASE_URL=http://localhost:8000
read -rsp "ADMIN_TOKEN: " PARSERUSERBOT_ADMIN_TOKEN; printf '\n'

curl --fail-with-body -i \
  -X POST "${PARSERUSERBOT_BASE_URL%/}/vacancy-events" \
  -H "Authorization: Bearer ${PARSERUSERBOT_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{
    "event_type": "telegram_vacancy_post",
    "idempotency_key": "telegram:-1001234567890:456",
    "source": {
      "chat_id": "-1001234567890",
      "message_id": "456",
      "channel": "vacancy_fixture",
      "message_url": "https://t.me/vacancy_fixture/456"
    },
    "post_text": "Senior Go backend, fully remote, 380000 RUB",
    "attachments": [],
    "raw": {"fixture": true}
  }'
```

Run the same command again. The second response must return the same intake ID
and identify the event as a duplicate; it must not create another intake row.

## Manual Trigger test

For a test that does not register a Telegram webhook, duplicate the imported
workflow, leave the duplicate inactive, and disconnect **Telegram Trigger**.
Add **Manual Trigger** followed by a **Code** node, connect that Code node to
**Normalize Forwarded Post**, and use this Code-node fixture:

```javascript
return [{
  json: {
    update_id: 900001,
    channel_post: {
      message_id: 456,
      date: 1760000000,
      chat: {
        id: -1001234567890,
        type: 'channel',
        title: 'Vacancy fixture',
        username: 'vacancy_fixture'
      },
      caption: 'Senior Java backend, fully remote, 350000 RUB',
      document: {
        file_id: 'fixture-file-id',
        file_unique_id: 'fixture-unique-id',
        file_name: 'job-description.pdf',
        mime_type: 'application/pdf'
      },
      forward_origin: {
        type: 'channel',
        date: 1759999900,
        chat: { id: -1009876543210, title: 'Original source' },
        message_id: 12
      }
    }
  }
}];
```

Execute the Manual Trigger twice. The first run should take the created branch;
the second should take the duplicate branch.

## Webhook fallback test

As another local-only fallback, duplicate the workflow and replace Telegram
Trigger with a **Webhook** node configured as `POST`, path
`telegram-vacancy-ingest-test`, and response mode **When Last Node Finishes**.
Connect it directly to **Normalize Forwarded Post**. The normalizer unwraps the
Webhook node's `body` automatically.

While **Listen for test event** is active, send:

```bash
export N8N_BASE_URL=http://localhost:5678

curl --fail-with-body -i \
  -X POST "${N8N_BASE_URL%/}/webhook-test/telegram-vacancy-ingest-test" \
  -H "Content-Type: application/json" \
  --data '{
    "update_id": 900002,
    "channel_post": {
      "message_id": 457,
      "date": 1760000000,
      "chat": {
        "id": -1001234567890,
        "type": "channel",
        "title": "Vacancy fixture",
        "username": "vacancy_fixture"
      },
      "text": "Senior Golang developer, remote only"
    }
  }'
```

Delete or disable the unauthenticated test Webhook node after testing. The
Telegram Trigger remains the production ingestion path.

## Operational notes

- n8n retries are safe because parserUserBot owns idempotency. Do not replace
  the stable Telegram key with an execution ID or timestamp.
- A Telegram media group produces one trigger event per message. Each event has
  its own message ID, while `media_group_id` is retained on attachment metadata.
- The workflow forwards attachment metadata only. It does not fetch binaries,
  submit applications, handle CAPTCHA, or contain production secrets.
