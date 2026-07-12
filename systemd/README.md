# systemd service files for parserUserBot

These files deploy parserUserBot as systemd services on Linux (Raspberry Pi).

## Components

| File | Purpose | Type |
|---|---|---|
| `parserUserBot-main.service` | Telegram client daemon | long-running |
| `parserUserBot-trigger.service` | Produces callback outbox entries | oneshot |
| `parserUserBot-trigger.timer` | Runs trigger every 30s | timer |
| `parserUserBot-dispatcher.service` | Sends outbox entries to OpenClaw | oneshot |
| `parserUserBot-dispatcher.timer` | Runs dispatcher every 30s | timer |

## Install

```bash
# Copy service files
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable and start the main daemon
sudo systemctl enable --now parserUserBot-main.service

# Enable and start the timers (trigger + dispatcher)
sudo systemctl enable --now parserUserBot-trigger.timer
sudo systemctl enable --now parserUserBot-dispatcher.timer
```

## Monitor

```bash
# Check status
sudo systemctl status parserUserBot-main
sudo systemctl list-timers parserUserBot-*

# View logs
journalctl -u parserUserBot-main -f
journalctl -u parserUserBot-trigger -f
```

## Notes

- Adjust `ExecStart` paths if using a virtualenv (replace `/usr/bin/python3` with `.venv/bin/python`)
- Adjust `User=` if running as a different user
- The `.env` file must exist at the `EnvironmentFile` path
- PostgreSQL must be running before the bot starts
