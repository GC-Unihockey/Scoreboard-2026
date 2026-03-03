# Service Configuration


- Repository folder: /opt/scoreboard
- Service shell script: /usr/local/bin/SB_Service_start/Scoreboard_Service.sh
  
## scoreboard.service
Modify service: sudo nano /etc/systemd/system/scoreboard.service

```bash
[Unit]
Description=Scoreboard Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=administrator
Group=administrator

WorkingDirectory=/usr/local/bin
ExecStart=/usr/local/bin/Scoreboard_Service.sh

Restart=always
RestartSec=2

# Clean shutdown (SIGINT like Ctrl+C)
KillSignal=SIGINT
TimeoutStopSec=15

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- Reload service config after change: systemctl daemon-reload

## Log
- Check log (currently logged to Journal): 
  
  ```
  tail -f /var/log/scoreboard_service.log
  ```
  ```
  journalctl -u scoreboard.service --since today
  ```