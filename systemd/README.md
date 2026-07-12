# systemd service files

These are production templates for Linux. Before copying them into `/etc/systemd/system/`, replace `/opt/parserUserBot` and `User=parseruserbot` if your checkout or service account differs.

```bash
sudo useradd --system --home /opt/parserUserBot --shell /usr/sbin/nologin parseruserbot || true
sudo mkdir -p /opt
sudo cp -a /path/to/parserUserBot /opt/parserUserBot
sudo chown -R parseruserbot:parseruserbot /opt/parserUserBot
sudo cp /opt/parserUserBot/systemd/*.service /opt/parserUserBot/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now parserUserBot-main.service
sudo systemctl enable --now parserUserBot-trigger.timer parserUserBot-dispatcher.timer
```

Monitor:

```bash
systemctl status parserUserBot-main.service
systemctl list-timers 'parserUserBot-*'
journalctl -u parserUserBot-main.service -f
journalctl -u parserUserBot-trigger.service -f
journalctl -u parserUserBot-dispatcher.service -f
```
