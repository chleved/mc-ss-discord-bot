# Minecraft Server Status Bot

A minimal Discord bot in Python that monitors a Minecraft server's status.
It uses SFTP to check for log activity (`debug.log`) and falls back to RCON to fetch online players when the server is alive. The bot updates a dedicated Discord embed with the current status (OFFLINE / STARTING / ONLINE) in real-time.

## Features
- **Smart Polling:** Checks SFTP for log updates without downloading the whole file.
- **RCON Integration:** Fetches online players seamlessly. 
- **Auto-Cleanup:** Deletes older bot messages on startup to keep the channel clean.

## Setup

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables**:
   Create a `.env` file in the project's root directory and fill in your details:

   ```env
   # === SFTP Configuration (Logs check) ===
   SFTP_HOST=your.sftp.host.com
   SFTP_PORT=22
   SFTP_USER=username
   SFTP_PASSWORD=password
   # SFTP_KEY_PATH=/path/to/key # Optional, overrides password if set
   DEBUG_LOG_PATH=/path/to/minecraft/logs/debug.log

   # === RCON Configuration (Player list) ===
   RCON_HOST=your.rcon.host.com
   RCON_PORT=25575
   RCON_PASSWORD=your_rcon_password

   # === Discord Configuration ===
   DISCORD_TOKEN=your_bot_token_here
   DISCORD_CHANNEL_ID=123456789012345678
   ```

3. **Run the Bot**:
   ```bash
   python mcstatusbot.py
   ```
