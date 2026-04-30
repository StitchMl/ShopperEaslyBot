set -e
sudo tee /etc/systemd/system/shopper-easly-bot.service >/dev/null <<'EOF'
[Unit]
Description=Shopper Easly Telegram aggregator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=opc
Group=opc
WorkingDirectory=/opt/shopper-easly-bot
ExecStart=/opt/shopper-easly-bot/.venv/bin/python -m shopper_merge_bot
Restart=always
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable shopper-easly-bot.service
sudo systemctl restart shopper-easly-bot.service
sleep 5
sudo systemctl status shopper-easly-bot.service --no-pager --full || true
sudo journalctl -u shopper-easly-bot.service -n 60 --no-pager
