# Autostream Service Configuration

## scoreboard.service
Modify service: sudo nano /etc/systemd/system/auto_stream.service

- Reload service config after change: systemctl daemon-reload

## Disable autostart
sudo systemctl disable auto_stream.service

## Enable autostart
sudo systemctl enable auto_stream.service

## Check Log
  ```
  journalctl -u auto_stream.service -f --since today
  ```
  ```
  journalctl -u auto_stream.service -f --since "1 hour ago"
  ```