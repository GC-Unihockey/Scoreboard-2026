# Stream Data Update Service Configuration

## update-streamdata.service
Modify service: sudo nano /etc/systemd/system/update-streamdata.service

## update-streamdata.timer
Modify service: sudo nano /etc/systemd/system/update-streamdata.timer

- Reload service config after change: systemctl daemon-reload

## Check Log
  ```
  journalctl -u update-streamdata -f --since today
  ```
  ```
  journalctl -u update-streamdata -f --since "1 hour ago"
  ```