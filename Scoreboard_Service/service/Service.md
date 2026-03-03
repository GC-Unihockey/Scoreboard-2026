# Scoreboard Service Configuration

- Repository folder: /opt/scoreboard
- Service shell script: /usr/local/bin/SB_Service_start/Scoreboard_Service.sh
  
## scoreboard.service
Modify service: sudo nano /etc/systemd/system/scoreboard.service


- Reload service config after change: systemctl daemon-reload

## Log
- Check log (currently logged to Journal): 
  
  ```
  tail -f /var/log/scoreboard_service.log
  ```
  ```
  journalctl -u scoreboard.service -f --since today
  ```