"""
Minecraft Server Status Discord Bot
====================================
Monitors a Minecraft server by:
  - Polling debug.log mtime over SFTP every few seconds
  - Waiting for RCON to come up once log activity is detected
  - Reporting state changes (OFFLINE / STARTING / ONLINE + player count) to Discord

State machine:
  OFFLINE  --[log becomes fresh]--> STARTING
  STARTING --[RCON connects]------> ONLINE
  ONLINE   --[log goes stale]-----> OFFLINE
  STARTING --[log goes stale]-----> OFFLINE
  ANY      --[connection error]---> (keep current state)
"""

import asyncio
import time
import logging
from enum import Enum

import signal
import atexit
import discord
import paramiko
from dotenv import load_dotenv
import os
from mcrcon import MCRcon

# ─── Intents ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True  # For reading message content
intents.members = True  # If needed for member tracking
client = discord.Client(intents=intents)  # Or Bot() if using commands

# ─── Configuration ────────────────────────────────────────────────────────────

load_dotenv()

SFTP_HOST = os.environ.get("SFTP_HOST")
SFTP_PORT = os.environ.get("SFTP_PORT")
SFTP_USER = os.environ.get("SFTP_USER")
SFTP_PASSWORD = os.environ.get("SFTP_PASSWORD")
SFTP_KEY_PATH = os.environ.get("SFTP_KEY_PATH")
DEBUG_LOG_PATH = os.environ.get("DEBUG_LOG_PATH")

RCON_HOST = os.environ.get("RCON_HOST")
RCON_PORT = os.environ.get("RCON_PORT")
RCON_PASSWORD = os.environ.get("RCON_PASSWORD")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")

POLL_INTERVAL = 5  # seconds between each SFTP stat check
LOG_STALE_SECONDS = 40  # seconds of no log update before declaring OFFLINE

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("mc-bot")

# ─── State ────────────────────────────────────────────────────────────────────


class Status(Enum):
    OFFLINE = "OFFLINE"
    STARTING = "STARTING"
    ONLINE = "ONLINE"


STATUS_EMOJI = {
    Status.OFFLINE: "🔴",
    Status.STARTING: "🟡",
    Status.ONLINE: "🟢",
}

# ─── SFTP ─────────────────────────────────────────────────────────────────────


class SFTPMonitor:
    def __init__(self):
        self._client = None
        self._sftp = None
        self._last_connect_attempt = 0
        self._reconnect_delay = 15  # seconds to wait between reconnect attempts

    def _connect(self):
        now = time.time()
        since_last = now - self._last_connect_attempt
        if since_last < self._reconnect_delay:
            log.info(
                f"Reconnect throttled — waiting {self._reconnect_delay - since_last:.0f}s more"
            )
            return False
        self._last_connect_attempt = now
        self._close()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(
            hostname=SFTP_HOST,
            port=SFTP_PORT,
            username=SFTP_USER,
            timeout=10,
            look_for_keys=False,
            allow_agent=False,
        )
        if SFTP_KEY_PATH:
            kwargs["key_filename"] = SFTP_KEY_PATH
        else:
            kwargs["password"] = SFTP_PASSWORD
        client.connect(**kwargs)
        self._client = client
        self._sftp = client.open_sftp()
        self._last_connect_attempt = 0  # reset on success
        log.info("SFTP connected.")

    def _close(self):
        try:
            if self._sftp:
                self._sftp.close()
            if self._client:
                self._client.close()
        except Exception:
            pass
        self._sftp = None
        self._client = None

    def get_log_info(self) -> tuple[float, str] | None:
        """
        Returns (mtime, tail_text) by reading only the last 8KB of the log.
        Returns None on connection failure.
        """
        try:
            if self._sftp is None:
                connected = self._connect()
                if connected is False:
                    return None  # throttled, skip this poll
            attrs = self._sftp.stat(DEBUG_LOG_PATH)
            mtime = float(attrs.st_mtime)
            size = attrs.st_size
            tail_size = 8192  # 8KB — more than enough to catch "Stopping server"
            offset = max(0, size - tail_size)
            with self._sftp.open(DEBUG_LOG_PATH, "r") as f:
                f.seek(offset)
                tail = f.read().decode("utf-8", errors="replace")
            return mtime, tail
        except (paramiko.SSHException, OSError, EOFError) as e:
            log.warning(f"SFTP error: {e}")
            self._close()
        return None


# ─── RCON ─────────────────────────────────────────────────────────────────────


def try_rcon_get_players() -> tuple[bool, int, list[str]]:
    """
    Synchronous — always call via asyncio.to_thread() to avoid blocking the event loop.
    Returns (success, player_count, player_names).
    """
    log.info(f"Attempting RCON connection to {RCON_HOST}:{RCON_PORT}...")
    try:
        with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT, timeout=5) as rcon:
            response = rcon.command("list")
            log.info(f"RCON response: {response!r}")
            count, names = 0, []
            if "players online:" in response:
                parts = response.split("players online:")
                for word in parts[0].split():
                    if word.isdigit():
                        count = int(word)
                        break
                if len(parts) > 1 and parts[1].strip():
                    names = [n.strip() for n in parts[1].split(",") if n.strip()]
            return True, count, names
    except Exception as e:
        log.warning(f"RCON failed: {e}")
        return False, 0, []


# ─── Embed builders ───────────────────────────────────────────────────────────

STATUS_COLOR = {
    Status.OFFLINE: 0xE74C3C,  # red
    Status.STARTING: 0xF39C12,  # orange
    Status.ONLINE: 0x2ECC71,  # green
}


def build_status_embed(status: Status, players: list[str]) -> discord.Embed:
    emoji = STATUS_EMOJI[status]

    if status == Status.ONLINE:
        title = f"{emoji}  Server is ONLINE"
        description = None
    elif status == Status.STARTING:
        title = f"{emoji}  Server is STARTING UP..."
        description = "⚠️ *If it crashes just start it again*"
    else:
        title = f"{emoji}  Server is OFFLINE"
        description = None

    embed = discord.Embed(
        title=title, description=description, color=STATUS_COLOR[status]
    )

    if players:
        embed.add_field(
            name=f"👥 Players online ({len(players)})",
            value=", ".join(players),
            inline=False,
        )
    else:
        embed.add_field(name="👥 Players online (0)", value="-", inline=False)

    embed.set_footer(text=f"🕐 Status updates every {POLL_INTERVAL} seconds")
    return embed


# ─── Cleanup ──────────────────────────────────────────────────────────────────


def cleanup():
    """Ensure SFTP connection is properly closed on exit so no zombie sessions remain."""
    log.info("Cleaning up SFTP connection...")
    sftp_monitor._close()


# ─── Discord Bot ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
bot = discord.Client(intents=intents)

sftp_monitor = SFTPMonitor()
atexit.register(cleanup)
signal.signal(signal.SIGTERM, lambda *_: cleanup())
current_status = Status.OFFLINE
last_mtime: float | None = None
current_players: list[str] = []
status_message: discord.Message | None = None


async def post_status(channel: discord.TextChannel, status: Status, players: list[str]):
    """Delete old status message and repost it so it always sits at the bottom."""
    global status_message
    if status_message:
        try:
            await status_message.delete()
        except discord.NotFound:
            pass
    status_message = await channel.send(embed=build_status_embed(status, players))


async def monitor_loop():
    global current_status, last_mtime, current_players

    await bot.wait_until_ready()
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        log.error(f"Could not find Discord channel {DISCORD_CHANNEL_ID}")
        return

    log.info("Monitor loop started.")

    # Clean up any status messages left over from a previous bot session
    log.info("Scanning channel for old status messages to clean up...")
    async for message in channel.history(limit=50):
        if (
            message.author == bot.user
            and message.embeds
            and message.embeds[0].title
            and any(message.embeds[0].title.startswith(p) for p in ("🔴", "🟡", "🟢"))
        ):
            try:
                await message.delete()
                log.info(f"Deleted old status message: {message.id}")
            except discord.NotFound:
                pass

    await post_status(channel, current_status, current_players)

    while not bot.is_closed():
        now = time.time()

        # Run blocking SFTP call in a thread so it never freezes the event loop
        result = await asyncio.to_thread(sftp_monitor.get_log_info)

        if result is None:
            log.warning("SFTP unreachable — keeping current state.")
            await asyncio.sleep(POLL_INTERVAL)
            continue

        mtime, log_tail = result
        log_age = now - mtime
        server_stopping = "Stopping server" in log_tail
        log.debug(
            f"Log age: {log_age:.1f}s | Stopping: {server_stopping} | State: {current_status.value}"
        )

        # ── OFFLINE ────────────────────────────────────────────────────────
        if current_status == Status.OFFLINE:
            if (
                log_age < LOG_STALE_SECONDS
                and (last_mtime is None or mtime != last_mtime)
                and not server_stopping
            ):
                log.info("Log activity detected → STARTING")
                current_status = Status.STARTING
                current_players = []
                last_mtime = mtime
                await post_status(channel, Status.STARTING, [])

        # ── STARTING ───────────────────────────────────────────────────────
        elif current_status == Status.STARTING:
            if server_stopping:
                log.info("'Stopping server' detected during STARTING → OFFLINE")
                current_status = Status.OFFLINE
                current_players = []
                last_mtime = mtime
                await post_status(channel, Status.OFFLINE, [])
            elif log_age > LOG_STALE_SECONDS:
                log.info("Log went stale during STARTING → OFFLINE")
                current_status = Status.OFFLINE
                current_players = []
                last_mtime = mtime
                await post_status(channel, Status.OFFLINE, [])
            else:
                # Run blocking RCON call in a thread
                success, count, names = await asyncio.to_thread(try_rcon_get_players)
                if success:
                    log.info(f"RCON up — {count} player(s) → ONLINE")
                    current_status = Status.ONLINE
                    current_players = names
                    last_mtime = mtime
                    await post_status(channel, Status.ONLINE, names)

        # ── ONLINE ─────────────────────────────────────────────────────────
        elif current_status == Status.ONLINE:
            success, count, names = await asyncio.to_thread(try_rcon_get_players)

            if success:
                joined = [p for p in names if p not in current_players]
                left = [p for p in current_players if p not in names]

                for player in joined:
                    log.info(f"Player joined: {player}")
                    embed = discord.Embed(
                        description=f"🟢  **{player}** joined the server",
                        color=0x2ECC71,
                    )
                    await channel.send(embed=embed)

                for player in left:
                    log.info(f"Player left: {player}")
                    embed = discord.Embed(
                        description=f"🔴  **{player}** left the server", color=0xE74C3C
                    )
                    await channel.send(embed=embed)

                if joined or left:
                    current_players = names
                    await post_status(channel, Status.ONLINE, names)

            else:
                # RCON failed — fall back to log tail + mtime
                log.warning("RCON down — falling back to log tail check")
                if server_stopping or log_age > LOG_STALE_SECONDS:
                    if server_stopping:
                        log.info("'Stopping server' detected → OFFLINE")
                    else:
                        log.info(f"Log stale ({log_age:.0f}s) and RCON down → OFFLINE")
                    for player in current_players:
                        embed = discord.Embed(
                            description=f"🔴  **{player}** left the server",
                            color=0xE74C3C,
                        )
                        await channel.send(embed=embed)
                    current_status = Status.OFFLINE
                    current_players = []
                    last_mtime = mtime
                    await post_status(channel, Status.OFFLINE, [])
                else:
                    log.info(
                        f"Log fresh ({log_age:.0f}s), no stop signal — keeping ONLINE despite RCON failure"
                    )

        last_mtime = mtime
        await asyncio.sleep(POLL_INTERVAL)


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    bot.loop.create_task(monitor_loop())


bot.run(DISCORD_TOKEN)
