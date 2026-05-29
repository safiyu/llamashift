#!/bin/bash

# Ensure the script is run with sudo
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script with sudo: sudo ./install_service.sh"
  exit 1
fi

# Detect the user who ran sudo, fallback to safiyu
USER_NAME=${SUDO_USER:-safiyu}

# Get the absolute path of the directory where this script is located
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="/etc/systemd/system/llamashift.service"

echo "Creating systemd service file at $SERVICE_FILE..."

cat <<EOF > $SERVICE_FILE
[Unit]
Description=LlamaShift Webapp
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/env python3 $APP_DIR/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling and starting llamashift service..."
systemctl enable llamashift.service
systemctl start llamashift.service

echo "Service installed and started successfully!"
echo "You can check its status anytime with: sudo systemctl status llamashift"
