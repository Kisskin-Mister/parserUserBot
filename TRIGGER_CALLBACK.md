# parserUserBot callback runtime

## Runtime chain
1. `trigger_worker.py --once`
   - reads unnotified rows from Postgres
   - creates callback payload
   - writes spool file into `runtime/callback_spool/`

2. `callback_dispatcher.py --once`
   - reads pending spool files
   - sends them through OpenClaw CLI RPC:
     - `openclaw gateway call chat.send`
   - moves delivered files to `runtime/callback_sent/`
   - moves failed files to `runtime/callback_failed/`

## Important note
- runtime path has **no llm logic**
- trigger worker does **not** call gateway directly
- dispatcher is the practical delivery layer that makes the system actually work end-to-end
- spool envelope stays compatible with the `sessions_send` message shape:
  - `sessionKey`
  - `message`
  - `idempotencyKey`

## launchd jobs
- `com.kisskin.parseruserbot.trigger`
- `com.kisskin.parseruserbot.dispatcher`

## Main commands
### Setup / start everything
```bash
bin/start_parseruserbot.sh --dry-run-trigger
```

### Smoke test
```bash
bin/smoke_test_parseruserbot.sh
```

### Smoke test with real trigger write
```bash
bin/smoke_test_parseruserbot.sh --real-trigger
```

## Useful checks
### launchd status
```bash
launchctl print gui/$(id -u)/com.kisskin.parseruserbot.trigger
launchctl print gui/$(id -u)/com.kisskin.parseruserbot.dispatcher
```

### logs
```bash
tail -f logs/trigger_worker.out.log logs/trigger_worker.err.log

tail -f logs/callback_dispatcher.out.log logs/callback_dispatcher.err.log
```

### spool/sent/failed
```bash
ls -lah runtime/callback_spool
ls -lah runtime/callback_sent
ls -lah runtime/callback_failed
```
