#!/bin/bash

LOGFILE="/var/log/scoreboard_service.log"

log_message() {
    TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
    #echo "[$TIMESTAMP] $1" #| tee -a $LOGFILE #not needed to log during job execution
    echo "[$TIMESTAMP] $1" | tee -a $LOGFILE
}

log_message "<< Scoreboard Service started >>"

if [ ! -d "/opt/scoreboard/.git" ]; then
    log_message "Repository not found. Cloning from git..."
    git clone git@github.com:GC-Unihockey/Scoreboard-2026.git /opt/scoreboard
else
    log_message "Pulling latest changes from git repository..."
fi

cd /opt/scoreboard || { log_message "Failed to access /opt/scoreboard"; exit 1; }

if git pull origin main; then
    log_message "Git pull completed successfully"
else
    log_message "Git pull encountered errors"
fi

# Start Serial_Wireless_vmix.py
log_message "Starting Scoreboard Service..."
python3 Scoreboard_Service/main_live.py &
PID=$!
log_message "Scoreboard Service started with PID $PID"
# Wait for the process to finish
wait $PID

log_message "<< Scoreboard Service stopped >>"