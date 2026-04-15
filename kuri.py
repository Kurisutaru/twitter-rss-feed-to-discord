import asyncio
import atexit
import calendar
import hashlib
import os
import platform
import random
import re
import shutil
import signal
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from io import BytesIO
from operator import attrgetter
from os.path import isfile
from pathlib import Path
from typing import Optional, Union
from urllib.parse import quote, unquote, urljoin, urlparse
from warnings import deprecated

import dateutil.parser
import discord
import feedparser
import orjson
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from bs4 import BeautifulSoup
from bs4.element import NavigableString
from discord.ui import LayoutView
from feedparser import FeedParserDict
from loguru import logger as log

# Keep lxml import – used in RSS patching
# noinspection PyUnresolvedReferences
from mashumaro.mixins.json import DataClassJSONMixin

from stoat_webhook import StoatEmbed, StoatMasquerade, StoatWebhook

# Get the directory where the script is located
script_dir = os.path.dirname(os.path.abspath(__file__))

# Configure log
log_file = os.path.join(script_dir, 'app.log')
log.remove()
log_format = ("<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
              "<level>{level: <8}</level> | "
              "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>")


def kuri_zip_compression(file_path):
    """Simple compression with current date"""
    directory = os.path.dirname(file_path) or "."
    today = datetime.now().strftime("%Y-%m-%d")

    log_dir = os.path.join(directory, 'logs')
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    zip_filename = os.path.join(log_dir, f"app.{today}.log.zip")

    with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(file_path, f"app.{today}.log")

    os.remove(file_path)


log.add(sys.stdout, format=log_format, colorize=True)
log.add(log_file, rotation="sunday", compression=kuri_zip_compression,
        encoding="utf-8", level="INFO", format=log_format)


class ScriptLock:
    """
    Prevents multiple instances of the script from running simultaneously.

    Fixes vs original:
    - Atomic O_CREAT|O_EXCL creation eliminates TOCTOU race condition
    - /proc/<pid>/cmdline check guards against PID recycling false-positives
    - `platform` imported once at module level, not per-call
    - Context manager support (__enter__ / __exit__)
    - _safe_remove() helper prevents bare os.remove() crashes
    """

    def __init__(self, lock_file='script.lock', script_dir_param=None, logger=None):
        base = script_dir_param or os.path.dirname(os.path.abspath(__file__))
        self.lock_file = os.path.join(base, lock_file)
        self.locked = False
        self.log = logger

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self):
        """
        Acquire the lock atomically.
        - If a live instance already holds it → log and exit.
        - If the lock file is stale (dead PID or corrupt) → remove and retry.
        - Uses O_CREAT|O_EXCL so only one process can succeed.
        """
        # ── Handle any pre-existing lock file ──────────────────────────
        if os.path.exists(self.lock_file):
            try:
                with open(self.lock_file) as file:
                    old_pid = int(file.read().strip())

                if self._is_process_running(old_pid):
                    self._log('warning',
                              f"Script already running (PID: {old_pid}). Exiting.")
                    sys.exit(0)
                else:
                    self._log('info',
                              f"Removing stale lock file (PID: {old_pid} no longer alive)")
                    self._safe_remove(self.lock_file)

            except (OSError, ValueError):
                self._log('warning', "Removing invalid/unreadable lock file")
                self._safe_remove(self.lock_file)

        # ── Atomic creation: only ONE process wins O_CREAT|O_EXCL ──────
        try:
            fd = os.open(
                self.lock_file,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)

        except FileExistsError:
            # Another process just created it between our check and here
            self._log('warning',
                      "Lock file appeared mid-flight (race). Another instance "
                      "just started. Exiting.")
            sys.exit(0)

        except OSError as e:
            self._log('error', f"Failed to create lock file: {e}")
            sys.exit(1)

        self.locked = True
        self._log('info', f"Lock acquired (PID: {os.getpid()})")

        atexit.register(self.release)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        return True

    def release(self):
        """Release the lock by removing the lock file."""
        if self.locked:
            self._safe_remove(self.lock_file)
            self.locked = False
            self._log('info', "Lock released")

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False  # do not suppress exceptions

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _signal_handler(self, signum, frame):
        self._log('info', f"Received signal {signum}, releasing lock")
        self.release()
        sys.exit(0)

    def _is_process_running(self, pid: int) -> bool:
        """
        Check if a process with the given PID is actually still running.

        On Linux/macOS we go one step further than os.kill(pid, 0):
        we verify /proc/<pid>/cmdline contains our script name to guard
        against PID recycling (the PID exists but belongs to a different
        program now).
        """
        if platform.system() == "Windows":
            import subprocess
            try:
                result = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid}', '/NH', '/FO', 'CSV'],
                    capture_output=True, text=True, timeout=5
                )
                return str(pid) in result.stdout
            except (subprocess.SubprocessError, FileNotFoundError):
                return False

        # ── POSIX ───────────────────────────────────────────────────────
        try:
            os.kill(pid, 0)
        except OSError:
            return False  # process is definitely gone

        # Guard against PID recycling on Linux via /proc
        cmdline_path = f"/proc/{pid}/cmdline"
        if os.path.exists(cmdline_path):
            try:
                with open(cmdline_path, 'rb') as file:
                    cmdline = file.read().replace(b'\x00', b' ').decode(errors='replace')
                script_name = os.path.basename(__file__)
                if script_name not in cmdline:
                    # PID exists, but it's a different program — treat as stale
                    return False
            except OSError:
                pass  # /proc disappeared mid-read → process died, treat as gone

        return True

    @staticmethod
    def _safe_remove(path: str) -> None:
        """Remove a file, silently ignoring 'already gone' errors."""
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            # Log if something genuinely unexpected happens
            print(f"[ScriptLock] Warning: could not remove {path}: {e}",
                  file=sys.stderr)

    def _log(self, level: str, msg: str) -> None:
        """Emit a log message through the injected logger or fall back to stderr."""
        if self.log:
            getattr(self.log, level)(msg)
        else:
            print(f"[ScriptLock/{level.upper()}] {msg}", file=sys.stderr)


# emoji
__emoji_play = '▶️'
__emoji_video = '🎬'
__emoji_photo = '🖼️'

# Special character
__braille_pattern_blank = '\u2800'

# Variable
__twitter_url: str = 'https://x.com'
__fxtwitter_url: str = 'https://fxtwitter.com'
__fxtwitter_api_url: str = 'https://api.fxtwitter.com'
__twitter_image_card_link_template: str = 'https://pbs.twimg.com/{}{}'
__post_video_identifier: str = 'ext_tw_video_thumb'
__rss_template: str = '{}/{}/rss'
__video_embed_content: str = f"{__emoji_play}[{__braille_pattern_blank}]({{}})"
__video_upload_content: str = f"{__emoji_play}{__braille_pattern_blank}"
__footer_append_template: str = ' • {}'
__discord_maximum_file_size: int = 10
__discord_maximum_embed_character: int = 4096
__discord_component_v2_split_image_count: int = 4

# JSONFile
__json_file: str = 'kuri.config.json'
__last_run_file: str = os.path.join(script_dir, 'last_run.json')

# ===== OPTIMIZATION: IN-MEMORY CACHES =====
_author_icon_cache = {}  # Cache author profile pictures


# Dataclasses
@dataclass
class TwitterUser:
    """Twitter user information for Discord embeds"""
    name: str
    link: str
    icon: str
    key: str
    discordWebhookUrl: list[str]
    discordMention: bool
    discordMentionRoleId: list[str]
    stoatWebhookUrl: list[str]


@dataclass
class EntryData:
    """Parsed tweet data"""
    title: str
    description: str
    link: str
    pubdate: datetime
    timestamp: float
    key: str
    mediaList: list['TwitterMedia'] = field(default_factory=list)
    hasVideo: bool = False

    def is_retweet(self) -> bool:
        """Check if this is a retweet"""
        return "RT by" in self.title or self.title.startswith('R to @')

    @property
    def clean_title(self) -> str:
        """Get cleaned tweet title"""
        return re.sub(r'^R to @\w+:\s*', '', self.title)


@dataclass
class Config:
    embedFooterText: str = ""
    embedFooterUrl: Optional[str] = None
    embedFooterImageUrl: Optional[str] = None
    includeReTweet: bool = False
    generateEmbed: bool = False
    useFxTwitterLinkInDiscord: bool = False
    useCustomProfile: bool = False
    useDiscordComponentV2: bool = False


@dataclass
class Profile:
    username: Optional[str] = None
    avatarUrl: Optional[str] = None


@dataclass
class TwitterWatch:
    twitterHandleName: str
    discordWebhookUrl: list[str] = field(default_factory=list)
    discordMention: bool = False
    discordMentionRoleId: list[str] = field(default_factory=list)
    stoatWebhookUrl: list[str] = field(default_factory=list)


@dataclass
class TwitterDiscordConfig(DataClassJSONMixin):
    config: Config
    nitterServer: list[str]
    profile: Optional[Profile] = None
    twitterWatch: list[TwitterWatch] = field(default_factory=list)


@dataclass
class TwitterMedia:
    """Represents assets in a tweet"""
    url: str
    type: str  # 'image', 'video', or 'video_thumbnail'


class MediaType(Enum):
    """Strong typing for assets types"""
    IMAGE = "image"
    VIDEO_URL = "video_url"  # URL to video (from fxtwitter)
    VIDEO_THUMBNAIL = "video_thumbnail"
    AUTHOR_ICON = "author_icon"


@dataclass
class DownloadedMedia:
    """Represents downloaded assets with proper typing"""
    filename: str
    data: bytes | None
    media_type: MediaType
    original_url: str


@dataclass
class WebhookMediaPayload:
    """Complete payload for webhook with proper separation of concerns"""
    # Author info
    author_icon_data: bytes | None = None
    author_icon_filename: Optional[str] = None

    # Videos (DownloadedMedia with data=bytes for uploaded, data=None for URL-only)
    videos: list[DownloadedMedia] = None

    # Images to attach and embed
    images: list[DownloadedMedia] = None

    # All attachments (images + author icon)
    all_attachments: list[DownloadedMedia] = None

    # Metadata
    cleaned_description: str = ""
    video_uploaded: bool = False

    def __post_init__(self):
        if self.videos is None:
            self.videos = []
        if self.images is None:
            self.images = []
        if self.all_attachments is None:
            self.all_attachments = []

    @property
    def image_count(self) -> int:
        return len(self.images)

    @property
    def video_count(self) -> int:
        return len(self.videos)

    @property
    def total_media_count(self) -> int:
        return self.image_count + self.video_count


@dataclass
class DestinationCheckpoints:
    """Per-destination, per-handler checkpoints for last processed tweet timestamps."""
    destinations: dict[str, dict[str, datetime]] = field(default_factory=dict)

    def get(self, destination: str, handler: str) -> datetime:
        """Get cutoff time for a destination+handler, defaults to year 2000."""
        return self.destinations.get(destination, {}).get(handler, datetime(2000, 1, 1))

    def update(self, destination: str, handler: str, dt: datetime):
        """Update checkpoint only if dt is newer than existing."""
        current = self.destinations.setdefault(destination, {}).get(handler, datetime.min)
        if dt > current:
            self.destinations[destination][handler] = dt

    def merge(self, other: "DestinationCheckpoints"):
        """Merge another checkpoint into this one (other takes priority)."""
        for destination, handlers in other.destinations.items():
            self.destinations.setdefault(destination, {}).update(handlers)

    def remove_nonexist_handler(self, handler_list: list[str]):
        """Remove destinations where no handler keys exist in handler_list."""
        for destination, handlers in self.destinations.items():
            self.destinations[destination] = {
                handle: dt for handle, dt in handlers.items()
                if handle in handler_list
            }


@dataclass(frozen=True)
class RandomEmbedColor:
    """
    Generates a random color and provides it in multiple formats useful for:
    - Discord: .int, .discord_int
    - Stoat/Revolt: .hex, .string, .hexcode
    - General use: .rgb_tuple, .rgba_tuple, .css_rgb, .css_rgba
    """

    r: int
    g: int
    b: int
    a: int = 255  # alpha (default opaque)

    @classmethod
    def random(cls) -> "RandomEmbedColor":
        """Factory method to create a random opaque color."""
        return cls(
            r=random.randint(0, 255),
            g=random.randint(0, 255),
            b=random.randint(0, 255),
            a=255
        )

    @classmethod
    def random_pastel(cls, saturation: float = 0.5) -> "RandomEmbedColor":
        """
        Generate a random pastel color.

        Args:
            saturation: How much to blend with white (0.0-1.0).
                       0.5 = soft pastels (default)
                       0.3 = very light pastels
                       0.7 = more vibrant pastels
        """
        # Clamp saturation between 0 and 1
        saturation = max(0.0, min(1.0, saturation))

        # Generate base random color
        base_r = random.randint(0, 255)
        base_g = random.randint(0, 255)
        base_b = random.randint(0, 255)

        # Mix with white (255, 255, 255) to create pastel
        # Formula: pastel = base * saturation + white * (1 - saturation)
        r = int(base_r * saturation + 255 * (1 - saturation))
        g = int(base_g * saturation + 255 * (1 - saturation))
        b = int(base_b * saturation + 255 * (1 - saturation))

        return cls(r=r, g=g, b=b, a=255)

    @classmethod
    def random_with_alpha(cls, alpha: int = 255) -> "RandomEmbedColor":
        """Random color with custom alpha (0–255)."""
        rand_color = cls.random()
        return cls(rand_color.r, rand_color.g, rand_color.b, alpha)

    @classmethod
    def random_gradient(cls) -> str:
        """Generate a simple random linear gradient for Stoat."""
        c1 = cls.random().hex
        c2 = cls.random().hex
        # direction = random.choice([
        #     "to right", "to bottom", "135deg", "45deg",
        #     "to bottom right", "to top left"
        # ])
        return f"linear-gradient(to right, {c1}, {c2})"

    @classmethod
    def random_pastel_gradient(cls, saturation: float = 0.5) -> str:
        """Generate a pastel gradient for Stoat."""
        c1 = cls.random_pastel(saturation).hex
        c2 = cls.random_pastel(saturation).hex
        return f"linear-gradient(to right, {c1}, {c2})"

    @property
    def color_int(self) -> int:
        """Discord-style integer (0xRRGGBB)."""
        return (self.r << 16) | (self.g << 8) | self.b

    @property
    def discord_int(self) -> int:
        """Same as .int — explicit alias for Discord."""
        return self.color_int

    @property
    def hex(self) -> str:
        """Stoat-compatible hex string with # prefix (e.g. "#1da1f2")."""
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}"

    @property
    def hexcode(self) -> str:
        """Alias for .hex — no # prefix (e.g. "1da1f2")."""
        return f"{self.r:02x}{self.g:02x}{self.b:02x}"

    @property
    def string(self) -> str:
        """Stoat-compatible string — returns .hex by default."""
        return self.hex

    @property
    def rgb_tuple(self) -> tuple[int, int, int]:
        """(r, g, b) tuple."""
        return (self.r, self.g, self.b)

    @property
    def rgba_tuple(self) -> tuple[int, int, int, int]:
        """(r, g, b, a) tuple."""
        return (self.r, self.g, self.b, self.a)

    @property
    def css_rgb(self) -> str:
        """CSS rgb() format — "rgb(29,161,242)"."""
        return f"rgb({self.r},{self.g},{self.b})"

    @property
    def css_rgba(self) -> str:
        """CSS rgba() format — "rgba(29,161,242,1)"."""
        alpha = self.a / 255.0
        return f"rgba({self.r},{self.g},{self.b},{alpha:.2f})".rstrip("0").rstrip(".")

    def __str__(self) -> str:
        """Default string representation — hex with #."""
        return self.hex

    def __int__(self) -> int:
        """Casting to int gives Discord integer."""
        return self.color_int


# Check if config file exist, if not abort
if not isfile(__json_file):
    log.error("Config file not found, abort current running script")
    exit(1)

with open(__json_file) as f:
    main_config = TwitterDiscordConfig.from_json(f.read())

nitter_url_list: list[str] = [*main_config.nitterServer, 'http://nitter.net']


def read_last_run(filename: str = __last_run_file) -> datetime:
    """Read the last processed tweet timestamp from file."""
    try:
        if os.path.exists(filename):
            with open(filename) as file:
                timestamp_str = file.read()
                timestamp_str = timestamp_str.strip()
                last_run = datetime.fromisoformat(timestamp_str)
                if last_run.tzinfo is not None:
                    last_run = last_run.astimezone(UTC).replace(tzinfo=None)
                log.info(f"Last processed tweet timestamp: {last_run} (UTC)")
                return last_run
        else:
            log.info("No last_run.txt found, will process all available entries")
            return datetime(2000, 1, 1)
    except Exception as read_error:
        log.error(f"Error reading last run: {read_error}, using default date")
        return datetime(2000, 1, 1)


def write_last_run(timestamp: datetime, filename: str = __last_run_file):
    """Write the timestamp of the latest processed tweet to file."""
    try:
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(UTC).replace(tzinfo=None)

        with open(filename, 'w') as file:
            file.write(timestamp.isoformat())
        log.info(f"Updated last processed tweet timestamp: {timestamp} (UTC)")
    except Exception as write_error:
        log.error(f"Error writing last run timestamp: {write_error}")


def read_last_run_per_handler(filename: str = __last_run_file) -> DestinationCheckpoints:
    """
    Read per-destination, per-handler timestamps from JSON file.
    Returns DestinationCheckpoints with structure: { destination -> { handler -> datetime } }
    Got problem when let say one of the endpoint were failed, it marks all of them failed
    It just happen on this patch
    """
    try:
        if not os.path.exists(filename):
            log.info("No last_run.json found, starting fresh for all handlers")
            return DestinationCheckpoints()

        with open(filename, 'rb') as file:
            data = orjson.loads(file.read())

        def parse_handlers(handlers: dict) -> dict[str, datetime]:
            result = {}
            for handler, timestamp_str in handlers.items():
                dt = dateutil.parser.parse(timestamp_str)
                if dt.tzinfo is not None:
                    dt = dt.astimezone(UTC).replace(tzinfo=None)
                result[handler] = dt
            return result

        checkpoints = DestinationCheckpoints(
            destinations={dest: parse_handlers(handlers) for dest, handlers in data.items()}
        )

        for dest, handlers in checkpoints.destinations.items():
            log.info(f"Loaded checkpoints: {len(handlers)} handlers for [{dest}]")

        return checkpoints

    except Exception as e:
        log.error(f"Error reading checkpoints: {e}, using empty")
        return DestinationCheckpoints()


def write_last_run_per_handler(checkpoints: DestinationCheckpoints, filename: str = __last_run_file):
    """
    Write per-destination, per-handler timestamps to JSON file with atomic write pattern.
    Prevents corruption if script crashes during write.
    """
    try:
        def serialize_handlers(handlers: dict[str, datetime]) -> dict[str, str]:
            result = {}
            for handler, dt in handlers.items():
                if dt.tzinfo is not None:
                    dt = dt.astimezone(UTC).replace(tzinfo=None)
                result[handler] = dt.isoformat()
            return result

        data = {dest: serialize_handlers(handlers) for dest, handlers in checkpoints.destinations.items()}

        file_dir = os.path.dirname(filename) or script_dir
        fd, temp_path = tempfile.mkstemp(suffix='.tmp', prefix='last_run_', dir=file_dir, text=False)

        try:
            with os.fdopen(fd, 'wb') as temp_file:
                temp_file.write(orjson.dumps(data, option=orjson.OPT_INDENT_2))
            shutil.move(temp_path, filename)
            log.info("✅ Atomically updated checkpoints")
        except Exception as write_error:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise write_error

    except Exception as e:
        log.error(f"Error writing checkpoints: {e}")


def migrate_old_checkpoint():
    """
    One-time migration from single-file last_run.txt to per-destination/handler last_run.json.
    Safe to run multiple times - only migrates if old file exists and new doesn't.
    """
    old_file = os.path.join(script_dir, 'last_run.txt')
    new_file = os.path.join(script_dir, 'last_run.json')

    if os.path.exists(old_file) and not os.path.exists(new_file):
        try:
            log.info("Found old last_run.txt, migrating to per-destination/handler format...")

            old_timestamp = read_last_run(old_file)

            checkpoints = DestinationCheckpoints()
            for item in main_config.twitterWatch:
                if item.discordWebhookUrl:
                    checkpoints.update("discord", item.twitterHandleName, old_timestamp)
                if item.stoatWebhookUrl:
                    checkpoints.update("stoat", item.twitterHandleName, old_timestamp)

            write_last_run_per_handler(checkpoints, new_file)
            log.info("✅ Migrated checkpoint to per-destination/handler format")

            backup_file = old_file + '.backup'
            shutil.move(old_file, backup_file)
            log.info(f"Backed up old file to {backup_file}")

        except Exception as e:
            log.error(f"Failed to migrate old checkpoint: {e}")


def generate_timestamp(input_time: Union["time.struct_time", str]) -> int:
    """
    Convert a feed-published time to a POSIX timestamp (seconds since epoch).

    * ``input_time`` is a ``time.struct_time`` → ``calendar.timegm`` (feedparser)
    * ``input_time`` is an ISO-8601 string → ``datetime.fromisoformat`` → ``timestamp()`` (fastfeedparser)

    Returns ``int`` (UTC seconds).
    """
    if isinstance(input_time, str):
        # fastfeedparser returns a clean UTC ISO-8601 string
        # e.g. "2025-10-26T12:34:56Z"  or  "2025-10-26T12:34:56+00:00"
        dt = datetime.fromisoformat(input_time.replace("Z", "+00:00"))
        return int(dt.timestamp())
    else:
        # feedparser returns a struct_time (already in UTC)
        return calendar.timegm(input_time)


def generate_date_from_timestamp(input_time) -> datetime:
    """Convert timestamp to datetime"""
    return datetime.fromtimestamp(input_time, tz=UTC)


def convert_mb_to_bytes(input_mb: int) -> int:
    return input_mb * 1024 * 1024


def convert_bytes_to_mb(input_bytes: int) -> float:
    return input_bytes / 1024 / 1024


def truncate_text(text: str, tweet_link: str, limit=__discord_maximum_embed_character):
    """
    Truncate text to fit Discord webhook message limits.
    For Twitter posts that exceed Discord's character limit.
    Truncates at the last paragraph or line break before the limit,
    so it never cuts in the middle of a sentence/paragraph.

    Removing those emoji checker, because it became complicated to check paragraph boundary

    Args:
        text (str): The text to truncate
        tweet_link (str): The link to the Twitter post
        limit (int): Maximum length including the footer message (default: 4096)

    Returns:
        str: Truncated text with continuation message if it exceeds the limit
    """
    footer = f'\n\n...\n📄 [Full post: View on X]({tweet_link})'

    if len(text) <= limit:
        return text

    available = limit - len(footer)

    # Find all paragraph break positions (both regular and blockquote)
    # Regular paragraph: \n\n
    # Blockquote paragraph: \n> \n (empty blockquote line)
    paragraph_breaks = []

    # Find regular paragraph breaks
    pos = 0
    while True:
        pos = text.find('\n\n', pos)
        if pos == -1:
            break
        paragraph_breaks.append(pos)
        pos += 2

    # Find blockquote paragraph breaks (> \n> pattern or > \n>)
    pos = 0
    while True:
        pos = text.find('\n> \n', pos)
        if pos == -1:
            break
        paragraph_breaks.append(pos + 1)  # Position after first \n, before > \n
        pos += 3

    # Sort all break positions
    paragraph_breaks.sort()

    # Find the last paragraph break that fits
    cutoff = -1
    for pos in reversed(paragraph_breaks):
        if pos <= available:
            cutoff = pos
            break

    # Fallback to single newline
    if cutoff == -1:
        cutoff = text.rfind('\n', 0, available)

    # Last resort: hard cut
    if cutoff == -1:
        cutoff = available

    return text[:cutoff].rstrip() + footer


def generate_rss_url(twitter_handle_name: str, nitter_url: str) -> str:
    """Generate RSS feed URL for a Twitter user"""
    return __rss_template.format(nitter_url, twitter_handle_name)


def replace_url_to_twitter(input_string: str, twitter_url: str) -> str:
    """Convert nitter URL to Twitter URL"""
    return_string = urljoin(twitter_url, urlparse(input_string).path)
    return return_string


def replace_nitter_url_to_twitter_url(input_string: str) -> str:
    """Replace nitter domain with Twitter domain"""
    return_string = unquote(input_string)
    return_string = return_string.replace('http://', 'https://').replace('#m', '')
    for serv in nitter_url_list:
        return_string = return_string.replace(urlparse(serv).netloc, urlparse(__twitter_url).netloc)
    return return_string


async def download_video_smart(session: ClientSession, url: str,
                               max_size: int = convert_mb_to_bytes(__discord_maximum_file_size)) -> tuple[
    bytes | None, bool]:
    """
    OPTIMIZED: Download video with streaming size check (no HEAD request needed).
    Returns: (data, was_too_large)
    - If video is small: returns (bytes, False)
    - If video is too large: returns (None, True)
    - If error: returns (None, False)
    """
    try:
        async with session.get(url, timeout=ClientTimeout(total=30)) as response:
            if not response.ok:
                log.warning(f"Failed to fetch video: HTTP {response.status}")
                return None, False

            # Check Content-Length first if available
            content_length = response.headers.get('Content-Length')
            if content_length:
                size = int(content_length)
                log.info(f"Video size from header: {convert_bytes_to_mb(size):.2f}MB")
                if size > max_size:
                    log.info(f"Video too large ({convert_bytes_to_mb(size):.2f}MB), skipping download")
                    return None, True

            # Stream download with size limit
            chunks = []
            downloaded = 0
            async for chunk in response.content.iter_chunked(256 * 1024):  # 256KB chunks
                chunks.append(chunk)
                downloaded += len(chunk)
                if downloaded > max_size:
                    log.warning(f"Video exceeded {__discord_maximum_file_size}MB during download, aborting")
                    return None, True

            log.info(f"✅ Video downloaded: {convert_bytes_to_mb(downloaded):.2f}MB")
            return b''.join(chunks), False

    except TimeoutError:
        log.error(f"Timeout downloading video: {url}")
        return None, False
    except Exception as e:
        log.error(f"Error downloading video: {e}")
        return None, False


def extract_video_url_from_nitter(nitter_video_url: str) -> str:
    """
    Extract actual Twitter video URL from nitter's /pic/ wrapper.
    Strips query parameters from the final URL.

    Input: http://nitter.net/pic/video.twimg.com%2Ftweet_video%2Ffile.mp4?tag=21
    Output: https://video.twimg.com/tweet_video/file.mp4
    """
    # Unquote first
    unquoted = unquote(nitter_video_url)

    # Extract the actual video.twimg.com URL from the path
    # Pattern: /pic/{actual_video_url}
    match = re.search(r'/pic/(.+)', unquoted)
    if match:
        actual_url = match.group(1)
        # Ensure https protocol
        if not actual_url.startswith('http'):
            actual_url = f'https://{actual_url}'
        actual_url = actual_url.replace('http://', 'https://')
    else:
        # Fallback: try to clean it manually
        cleaned = unquoted.replace('http://', 'https://').replace('/pic/', '')

        # Remove nitter domains
        for domain in nitter_url_list:
            cleaned = cleaned.replace(domain, '')

        # Clean up any double slashes (except after https:)
        actual_url = re.sub(r'(?<!:)//+', '/', cleaned)

    # Strip query parameters from the final URL (applies to both paths)
    return actual_url.split('?')[0]


def generate_twitter_embed_name(input_string: str) -> str:
    """Format Twitter username for embed"""
    return_string = input_string.replace(' / ', ' (') + ')'
    return return_string


def generate_twitter_profile_picture_link(input_string: str) -> str:
    """Extract Twitter profile picture URL"""
    return_string = unquote(urlparse(input_string).path).replace('/pic/', '')
    return return_string


def generate_twitter_picture_link(input_string: str, twitter_image_card_link_template: str) -> str:
    """Convert nitter image URL to Twitter CDN URL"""
    url_parse_data = urlparse(unquote(input_string))
    query_param = ""
    if bool(url_parse_data.query):
        query_param = '?' + url_parse_data.query
    return_string = twitter_image_card_link_template.format(url_parse_data.path, query_param).replace('/pic/', '')
    return return_string


def normalize_datetime_to_utc_naive(dt: datetime) -> datetime:
    """Normalize any datetime to UTC timezone-naive format for consistent comparison."""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    else:
        return dt


def generate_twitter_user_from_rss(feed_data: FeedParserDict, key: str, discord_webhook_url: list[str],
                                   discord_mention: bool,
                                   discord_mention_role_id: list[str],
                                   stoat_webhook_url: list[str], ) -> TwitterUser:
    """Create TwitterUser object from RSS feed data"""
    return TwitterUser(
        name=generate_twitter_embed_name(feed_data.feed.title),
        link=replace_url_to_twitter(feed_data.feed.link, __twitter_url),
        icon=generate_twitter_profile_picture_link(feed_data.feed.image.href),
        key=key,
        discordWebhookUrl=discord_webhook_url,
        discordMention=discord_mention,
        discordMentionRoleId=discord_mention_role_id,
        stoatWebhookUrl=stoat_webhook_url
    )


def detect_extension(url: str) -> str:
    """Detect file extension from URL"""
    parsed = urlparse(url)
    path = parsed.path

    ext = Path(path).suffix
    if ext:
        return ext.lstrip('.')

    if 'jpg' in url or 'jpeg' in url:
        return 'jpg'
    elif 'png' in url:
        return 'png'
    elif 'gif' in url:
        return 'gif'
    elif 'webp' in url:
        return 'webp'
    elif 'mp4' in url or 'video' in url:
        return 'mp4'

    return 'jpg'


def generate_media_filename(url: str, index: int = 0) -> str:
    """Generate unique filename for assets"""
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    ext = detect_extension(url)

    if index > 0:
        return f"twitter_media_{url_hash}_{index}.{ext}"
    return f"twitter_media_{url_hash}.{ext}"


def extract_media_from_description(description: str, twitter_card_template: str) -> tuple[list[TwitterMedia], bool]:
    """Extract all media URLs from RSS description."""
    extracted_media_list = []
    video_detected = False
    soup = BeautifulSoup(description, 'lxml')

    # === EXTRACT MEDIA FROM BLOCKQUOTES TOO ===
    # Process ALL video elements (including those in blockquotes)
    video_elements = soup.find_all('video')
    if video_elements:
        video_detected = True
        for video in video_elements:
            source = video.find('source')
            if source and source.get('src'):
                video_url = source.get('src')
                twitter_video_url = extract_video_url_from_nitter(video_url)
                extracted_media_list.append(TwitterMedia(url=twitter_video_url, type='video'))

            poster = video.get('poster', '')
            if poster:
                twitter_img_url = generate_twitter_picture_link(poster, twitter_card_template)
                extracted_media_list.append(TwitterMedia(url=twitter_img_url, type='video_thumbnail'))

    # Process ALL images (including those in blockquotes)
    for img in soup.find_all('img'):
        img_src = img.get('src', '')
        if not img_src:
            continue

        twitter_img_url = generate_twitter_picture_link(img_src, twitter_card_template)

        if __post_video_identifier in img_src or 'amplify_video_thumb' in img_src:
            video_detected = True
            extracted_media_list.append(TwitterMedia(url=twitter_img_url, type='video_thumbnail'))
        else:
            extracted_media_list.append(TwitterMedia(url=twitter_img_url, type='image'))

    return extracted_media_list, video_detected


def clean_tweet_text(title: str) -> str:
    """Clean tweet title by removing RT prefix"""
    text = re.sub(r'^R to @\w+:\s*', '', title)
    return text


def clean_tweet_description(html_content: str) -> str:
    """
    Remake version, I think the old one just plain wrong, or my logic just that bad
    Clean tweet description into Discord-ready Markdown text:
    1. <br> → newline
    2. <hr> → removed
    3. <img>/<video>/<source> → removed (entire line dropped if it was media-only)
    4. <a> → Discord Markdown [display](url)
       - nitter URLs replaced with x.com
       - protocol stripped from display text, EXCEPT on list keep_protocol_domains
       - truncated links (…) expand to full href as display text
       - #hashtag links → [#tag](https://x.com/hashtag/tag)
    5. <blockquote> → each content line prefixed with "> "
       - blank lines inside preserved as "> " (intended spacing)
       - trailing blank quote lines stripped
    6. Consecutive blank lines outside blockquotes collapsed to one

    Kuri Note : Please open issue if anything wrong
    """

    # Domains whose display text must keep the https:// protocol
    keep_protocol_domains = ['discord.gg', 'discord.com']

    def format_link(href: str, display: str) -> str:
        """Return a Discord Markdown link, applying all URL/display rules."""
        href = replace_nitter_url_to_twitter_url(href)
        display = replace_nitter_url_to_twitter_url(display.strip())

        if not href:
            return display

        # Hashtag links
        if display.startswith('#'):
            tag = display[1:]
            return f'[{display}](https://x.com/hashtag/{quote(tag)})'

        # Domains that must keep the protocol in display
        # I'm not sure if just left that link or wrap as Markdown Link ?
        # [link](link) or something
        if any(d in href for d in keep_protocol_domains):
            return f'{href}'

        # Truncated link -> use a full href as display, strip protocol
        if '…' in display or '...' in display:
            display = href.replace('https://', '').replace('http://', '')
            return f'[{display}]({href})'

        # Normal link -> strip protocol from display only
        display_clean = display.replace('https://', '').replace('http://', '')
        return f'[{display_clean}]({href})'

    def node_to_text(tag) -> str:
        """
        Recursively convert a BeautifulSoup node to plain Discord Markdown text.
        Handles all relevant tags inline so we never need post-processing sentinels.
        """
        if isinstance(tag, NavigableString):
            node_text = str(tag)
            # If the previous sibling was a <br>, the source HTML often has a literal
            # newline right after it,strip that one leading newline to avoid doubling
            prev = tag.previous_sibling
            if prev and getattr(prev, 'name', None) == 'br' and node_text.startswith('\n'):
                node_text = node_text[1:]
            return node_text

        name = tag.name

        # Discard media entirely, no placeholder, just empty string
        if name in ('img', 'video', 'source'):
            return ''

        # <br> -> newline
        if name == 'br':
            return '\n'

        # <hr> -> empty (removed)
        if name == 'hr':
            return ''

        # <a> -> markdown link
        if name == 'a':
            # Update to remove a Video text with link but no video link at all
            # Like [Video](some x.com url)
            inner_img = tag.find('img')
            if inner_img:
                img_src = inner_img.get('src', '')
                if __post_video_identifier in img_src or 'amplify_video_thumb' in img_src:
                    return ''
            href = tag.get('href', '')
            display = tag.get_text().strip()
            return format_link(href, display)

        # <blockquote> -> process children, then prefix every line with "> "
        if name == 'blockquote':
            inner = ''.join(node_to_text(child) for child in tag.children)
            process_lines = inner.split('\n')
            quoted = []
            previous_blank = False
            for process_line in process_lines:
                if process_line.strip():
                    quoted.append(f'> {process_line}')
                    previous_blank = False
                else:
                    if not previous_blank:
                        quoted.append('> ')
                    previous_blank = True
            # Strip trailing blank quote lines
            while quoted and quoted[-1] == '> ':
                quoted.pop()
            return '\n' + '\n'.join(quoted) + '\n'

        # Block-level tags, recurse into children
        if name in ('p', 'div', 'footer', 'cite', 'b', 'strong', 'em', 'i', 'span'):
            inner = ''.join(node_to_text(child) for child in tag.children)
            return inner

        # Unknown/unhandled tag (e.g. [document], comment nodes, etc.), recurse if possible
        if name is None or not hasattr(tag, 'children'):
            return ''
        return ''.join(node_to_text(child) for child in tag.children)

    soup = BeautifulSoup(html_content, 'lxml')

    # Convert the whole document to text using our recursive converter
    text = node_to_text(soup)

    # Clean up lines:
    # - Drop lines that are blank ONLY because media was removed
    #   (a line is "media-blank" if it's empty and was not preceded by real content on the same line —
    #    the recursive converter already returns '' for media, so consecutive \n\n from <img>\n<img>
    #    just collapses naturally here)
    # - Collapse consecutive blank lines to one (outside blockquotes already handled above)
    # Because sometimes on blockquote, its format like
    # <img>
    # <img>
    # blank
    # </p>
    # The result will be triple blank line, tried to remove the line if only img / video
    lines = text.split('\n')
    cleaned = []
    prev_blank = False

    for line in lines:
        if line.strip():
            cleaned.append(line)
            prev_blank = False
        else:
            # Preserve blank lines inside blockquotes (they're already "> ")
            # For regular blank lines, allow only one consecutive
            if not prev_blank:
                cleaned.append(line)
            prev_blank = True

    return '\n'.join(cleaned).strip()


@deprecated("Use RandomEmbedColor")
def generate_embed_color() -> int:
    # Generate a random integer between 0 and 0xFFFFFF
    random_color = random.randint(0, 0xFFFFFF)
    return random_color


def generate_discord_embed_data(title: str, payload: WebhookMediaPayload,
                                timestamp: float, author_name: str = None, author_url: str = None,
                                author_icon_url: str = None) -> discord.Embed:
    """Create embed object for webhook."""
    embed = discord.Embed()

    if author_name and author_url:
        embed.set_author(name=author_name,
                         url=author_url,
                         icon_url=author_icon_url)

    embed.colour = RandomEmbedColor.random().discord_int

    if timestamp:
        embed.timestamp = datetime.fromtimestamp(timestamp, tz=UTC)

    if title:
        embed.description = clean_tweet_text(title)

    footer = main_config.config.embedFooterText

    # Remove any emoji
    footer = re.sub(r'<:[A-Za-z0-9_]+:[0-9]+>', '', footer)

    if payload.image_count > 0:
        footer += __footer_append_template.format(f"{payload.image_count}{__emoji_photo}")
    if payload.video_count > 0:
        footer += __footer_append_template.format(f"{payload.video_count}{__emoji_video}")

    embed.set_footer(text=footer, icon_url=main_config.config.embedFooterImageUrl)

    return embed


def generate_stoat_embed_data(title: str, payload: WebhookMediaPayload,
                              timestamp: float, author_name: str = None, author_url: str = None,
                              author_icon_url: str = None) -> StoatEmbed:
    """Create embed object for webhook."""
    embed = StoatEmbed()

    if author_name and author_url:
        embed.url = author_url
        embed.title = author_name

    embed.colour = RandomEmbedColor.random().hex

    if title:
        embed.description = clean_tweet_text(title)

    if author_icon_url:
        embed.icon_url = author_icon_url

    return embed


# Hey its B E T A
# Gonna replicate most of the part like old embed
def generate_discord_container_v2_data(
        title: str,
        payload: WebhookMediaPayload,
        timestamp: float,
        author_name: str = None,
        author_url: str = None,
        author_icon_filename: str = None,
        author_icon_url: str = None,
        tweet_link: str = None,
) -> discord.ui.Container:
    container = discord.ui.Container(accent_color=RandomEmbedColor.random().discord_int)

    # First SECTION: author + content side by side with avatar thumbnail and the content
    icon_ref = (
        f"attachment://{author_icon_filename}"
        if author_icon_filename
        else (author_icon_url or "")
    )

    # I feel its better use small heading ? instead normal text, to emphasize like old embed ?
    # Scrap it, its too big for H2
    author_line = f"### **[{author_name}]({author_url})**" if author_name else ""

    cleaned = ""
    if title:
        cleaned = clean_tweet_text(title)
        if tweet_link:
            cleaned = truncate_text(cleaned, tweet_link)

    section = discord.ui.Section(
        *[item for item in [
            discord.ui.TextDisplay(author_line) if author_line else None,
            discord.ui.TextDisplay(cleaned) if cleaned else None,
        ] if item is not None],
        accessory=discord.ui.Thumbnail(icon_ref),
    )
    container.add_item(section)

    # images blocks
    if payload.image_count > 0:
        # Split per 4 image so 2x2 grid ? or should I just dump all of them per 10 ?
        for i in range(0, payload.image_count, __discord_component_v2_split_image_count):
            split = payload.images[i:i + __discord_component_v2_split_image_count]
            gallery = discord.ui.MediaGallery()
            for media in split:
                gallery.add_item(media=f"attachment://{media.filename}", description=media.filename, spoiler=False)
            container.add_item(gallery)

    # footer blocks
    footer = main_config.config.embedFooterText

    if main_config.config.embedFooterUrl:
        footer = f"[{footer}]({main_config.config.embedFooterUrl})"

    if payload.image_count > 0:
        footer += __footer_append_template.format(f"{payload.image_count} {__emoji_photo}")
    if payload.video_count > 0:
        footer += __footer_append_template.format(f"{payload.video_count} {__emoji_video}")

    # Add timestamps
    footer += __footer_append_template.format(f"<t:{int(timestamp)}:R>")

    container.add_item(discord.ui.TextDisplay(f"-# {footer}"))
    return container


async def fetch_rss_feed(session: ClientSession, twitter_handle: str, nitter_url: str) -> FeedParserDict | None:
    """Fetch RSS feed asynchronously"""
    rss_url = generate_rss_url(twitter_handle, nitter_url)

    try:
        async with session.get(rss_url, timeout=ClientTimeout(total=30)) as response:
            if response.ok:
                content = await response.read()
                # feedparser is synchronous but fast, so it's acceptable
                feed = feedparser.parse(content)
                return feed
            else:
                log.warning(f"Failed to fetch RSS for {twitter_handle}: HTTP {response.status}")
                return None
    except TimeoutError:
        log.error(f"Timeout fetching RSS for {twitter_handle}")
        return None
    except Exception as e:
        log.error(f"Error fetching RSS for {twitter_handle}: {e}")
        return None


async def download_media(session: ClientSession, url: str, max_size: int = convert_mb_to_bytes(25)) -> bytes | None:
    """Download assets file asynchronously with size limit"""
    try:
        async with session.get(url, timeout=ClientTimeout(total=30)) as response:
            if response.ok:
                content = await response.read()
                if len(content) > max_size:
                    log.warning(f"Media too large ({convert_bytes_to_mb(len(content)):.2f}MB): {url}")
                    return None
                return content
            else:
                log.warning(f"Failed to download assets: HTTP {response.status}")
                return None
    except TimeoutError:
        log.error(f"Timeout downloading: {url}")
        return None
    except Exception as e:
        log.error(f"Error downloading {url}: {e}")
        return None


async def get_author_icon(session: ClientSession, url: str) -> bytes | None:
    """
    OPTIMIZED: Cache author icons to avoid re-downloading same profile pictures.
    Returns cached data if available, otherwise downloads and caches.
    """
    if url in _author_icon_cache:
        log.debug(f"✅ Using cached author icon: {url}")
        return _author_icon_cache[url]

    log.info(f"Downloading new author icon: {url}")
    data = await download_media(session, url, max_size=convert_mb_to_bytes(5))

    if data:
        _author_icon_cache[url] = data
        log.debug("Cached author icon for future use")

    return data


async def fetch_video_from_fxtwitter(session: ClientSession, tweet_link: str) -> Optional[str]:
    """Fetch video URL from fxtwitter meta tags"""
    fx_url = tweet_link.replace(__twitter_url, __fxtwitter_api_url)

    try:
        log.info(f"Fetching video from fxtwitter: {fx_url}")
        async with session.get(fx_url, timeout=ClientTimeout(total=60)) as response:
            if not response.ok:
                log.warning(f"fxtwitter API returned HTTP {response.status}")
                return None

            data = orjson.loads(await response.read())

            if data.get('code') != 200:
                log.warning(f"fxtwitter API error: {data.get('message', 'unknown')}")
                return None

            videos = (data.get('tweet') or {}).get('media', {}).get('videos', [])
            if not videos:
                log.warning("No videos found in fxtwitter API response")
                return None

            # Take the first video (fxtwitter orders by relevance)
            video = videos[0]
            # 'video' | 'gif', but twitter 'gif' not 'gif', but mp4
            # Can process to 'gif' with ffmpeg but eeeh ... lazy, also processing cost
            video_type = video.get('type')

            # For real videos: pick highest-bitrate variant
            # For GIFs: I found its bitrate 0
            # But anything ok I guess ? As long its high quality version and pick one
            variants = video.get('variants') or video.get('formats') or []
            if variants:
                best = max(variants, key=lambda v: v.get('bitrate', 0))
                video_url = best.get('url')
            else:
                # Fallback to top-level url
                video_url = video.get('url')

            if video_url:
                log.info(f"✅ Found {video_type} URL from fxtwitter API: {video_url}")
            else:
                log.warning("fxtwitter API: video entry had no usable URL")

            return video_url

    except TimeoutError:
        log.error(f"Timeout fetching fxtwitter page: {fx_url}")
        return None
    except Exception as e:
        log.error(f"Error fetching video from fxtwitter: {e}")
        return None


async def generate_media_webhook(
        session: ClientSession,
        tweet_link: str,
        title: str,
        tweet_media_list: list[TwitterMedia],
        tweet_has_video: bool,
        twitter_user: TwitterUser
) -> WebhookMediaPayload:
    """
    Generate media webhook data with async downloads.
    OPTIMIZED: Uses cached author icons and smart video download (no HEAD request).
    """
    max_file_size = convert_mb_to_bytes(__discord_maximum_file_size)
    payload = WebhookMediaPayload()

    # Clean description first
    payload.cleaned_description = truncate_text(clean_tweet_description(title), tweet_link)

    # Separate media by type
    videos_from_rss = [m for m in tweet_media_list if m.type == 'video']
    # video_thumbnails = [m for m in tweet_media_list if m.type == 'video_thumbnail']
    images = [m for m in tweet_media_list if m.type == 'image']

    # === VIDEO HANDLING ===
    if tweet_has_video:
        log.info("Tweet have video")
        if videos_from_rss:
            # Use video from RSS
            video_url = videos_from_rss[0].url
            log.info(f"Using video URL from RSS: {video_url}")
        else:
            # Fetch from fxtwitter
            log.info("🎥 Getting video link from fxtwitter...")
            video_url = await fetch_video_from_fxtwitter(session, tweet_link)

            if video_url:
                log.info("✅ Got video URL from fxtwitter")
            else:
                log.warning("❌ Failed to get video from fxtwitter, will use thumbnails")
                video_url = None

        if video_url:
            # OPTIMIZED: Use smart download (no separate HEAD request)
            log.info("Downloading video with smart size check...")
            filename = generate_media_filename(video_url)

            video_data, was_too_large = await download_video_smart(session, video_url, max_size=max_file_size)

            if video_data:
                payload.video_uploaded = True
                # Video downloaded successfully
                downloaded_video = DownloadedMedia(
                    filename=filename,
                    data=video_data,
                    media_type=MediaType.VIDEO_URL,
                    original_url=video_url
                )
                payload.videos.append(downloaded_video)
                log.info(f"✅ Video downloaded successfully: {filename}")
            else:
                # Video too large or download failed, store URL only
                if was_too_large:
                    log.info("Video is over 10MB, will use URL only")
                else:
                    log.warning("Failed to download video, will use URL only")

                url_only_video = DownloadedMedia(
                    filename=filename,
                    data=None,
                    media_type=MediaType.VIDEO_URL,
                    original_url=video_url
                )
                payload.videos.append(url_only_video)

    # === CONCURRENT DOWNLOADS ===
    download_tasks = []
    task_metadata = []

    # Task: Author icon (OPTIMIZED: uses cache)
    if twitter_user.icon:
        log.info(f"Queueing author icon download: {twitter_user.icon}")
        download_tasks.append(get_author_icon(session, twitter_user.icon))
        task_metadata.append((MediaType.AUTHOR_ICON, twitter_user.icon))

    # Tasks: Images (always download)
    for media in images:
        log.info(f"Queueing image download: {media.url}")
        download_tasks.append(download_media(session, media.url, max_size=max_file_size))
        task_metadata.append((MediaType.IMAGE, media.url))

    # Tasks: Video thumbnails (ONLY if we don't have video)
    # Update, redundant due already pull from fxtwitter
    # if tweet_has_video and not payload.video_uploaded:
    #    for media in video_thumbnails:
    #        log.info(f"Queueing video thumbnail download (fallback): {media.url}")
    #        download_tasks.append(download_media(session, media.url, max_size=max_file_size))
    #        task_metadata.append((MediaType.VIDEO_THUMBNAIL, media.url))

    # Download all concurrently
    if download_tasks:
        log.info(f"🚀 Starting {len(download_tasks)} concurrent downloads...")
        download_results = await asyncio.gather(*download_tasks, return_exceptions=True)
        log.info("✅ All downloads completed")

        # Process results with proper typing
        for (media_type, url), result in zip(task_metadata, download_results):
            if isinstance(result, Exception):
                log.error(f"Failed to download {media_type.value} from {url}: {result}")
                continue

            if result is None:
                log.warning(f"Download returned None for {media_type.value}: {url}")
                continue

            # Generate filename
            if media_type == MediaType.AUTHOR_ICON:
                filename = f"profile_{hashlib.md5(url.encode()).hexdigest()[:8]}.jpg"
                payload.author_icon_data = result
                payload.author_icon_filename = filename
                log.info(f"✅ Author icon ready: {filename}")

            elif media_type == MediaType.IMAGE:
                filename = generate_media_filename(url, len(payload.images))
                downloaded = DownloadedMedia(
                    filename=filename,
                    data=result,
                    media_type=media_type,
                    original_url=url
                )
                payload.images.append(downloaded)
                payload.all_attachments.append(downloaded)
                log.info(f"✅ Image ready: {filename}")

            elif media_type == MediaType.VIDEO_THUMBNAIL:
                filename = generate_media_filename(url, len(payload.images))
                downloaded = DownloadedMedia(
                    filename=filename,
                    data=result,
                    media_type=media_type,
                    original_url=url
                )
                payload.images.append(downloaded)
                payload.all_attachments.append(downloaded)
                log.info(f"✅ Video thumbnail ready (fallback): {filename}")

    log.info(f"Payload ready: {payload.video_count} videos, {payload.image_count} images")
    return payload


async def post_to_single_discord_webhook(
        session: ClientSession,
        webhook_url: str,
        content: str,
        tweet_link: str,
        payload: WebhookMediaPayload,
        timestamp: float,
        twitter_user: TwitterUser
) -> bool:
    """
    OPTIMIZED: Post to a single webhook Discord (videos + embed).
    Extracted for parallel execution across multiple webhooks.
    """
    try:
        log.info(f"Processing webhook: {webhook_url[:50]}...")

        # === STEP 1: Send videos first ===
        if payload.videos:
            try:
                log.info(f"📹 Sending {len(payload.videos)} video(s)...")
                for video in payload.videos:
                    video_webhook = discord.Webhook.from_url(
                        url=webhook_url,
                        session=session
                    )

                    if video.data:
                        # Video was downloaded, upload as file
                        log.info(f"Uploading video as file: {video.filename}")
                        file = discord.File(fp=BytesIO(video.data), filename=video.filename)
                        await video_webhook.send(content=__video_upload_content.format(video.original_url),
                                                 file=file)
                    else:
                        # Video too large or download failed, send URL
                        log.info(f"Sending video URL: {video.original_url}")
                        await video_webhook.send(content=__video_embed_content.format(video.original_url))

                    log.info(f"✅ Video posted: {video.filename}")
            except discord.HTTPException as e:
                log.error(f"❌ Failed to post video. HTTP {e.status} {e.text}")
                return False

        # === STEP 2: Send content with or without embed ===
        # Reuseable
        webhook = discord.Webhook.from_url(url=webhook_url, session=session)
        username = None
        avatar_url = None

        if main_config.config.useCustomProfile:
            username = main_config.profile.username
            avatar_url = main_config.profile.avatarUrl

        if main_config.config.generateEmbed:
            # Send with embed (original behavior)
            log.info("Sending with embed (generateEmbed=True)")

            # Build author icon ref (shared between V1 and V2)
            if payload.author_icon_data:
                files = [discord.File(fp=BytesIO(payload.author_icon_data), filename=payload.author_icon_filename)]
                author_icon_url = f"attachment://{payload.author_icon_filename}"
            else:
                files = []
                author_icon_url = twitter_user.icon

            # Attach all images
            for media in payload.all_attachments:
                files.append(discord.File(fp=BytesIO(media.data), filename=media.filename))

            # Component V2
            if main_config.config.useDiscordComponentV2:
                log.info("Sending with Component V2 (useDiscordComponentV2=True)")

                author_icon_filename = payload.author_icon_filename if payload.author_icon_data else None
                author_icon_url_fallback = twitter_user.icon if not author_icon_filename else None

                # Top-level text: tweet link + optional role mentions
                top_text_parts = discord.ui.TextDisplay(content)

                container = generate_discord_container_v2_data(
                    title=payload.cleaned_description,
                    payload=payload,
                    timestamp=timestamp,
                    author_name=twitter_user.name,
                    author_url=twitter_user.link,
                    author_icon_filename=author_icon_filename,
                    author_icon_url=author_icon_url_fallback,
                    tweet_link=tweet_link,
                )

                try:
                    layout = LayoutView()
                    layout.add_item(top_text_parts)
                    layout.add_item(container)

                    await webhook.send(
                        view=layout,
                        files=files or discord.utils.MISSING,
                        username=username,
                        avatar_url=avatar_url,
                        allowed_mentions=discord.AllowedMentions(roles=True),
                    )
                    log.info("✅ Component V2 posted successfully")
                    return True
                except discord.HTTPException as e:
                    log.error(f"❌ Failed to post Component V2. HTTP {e.status}: {e.text}")
                    return False

            # Discord Embed
            else:
                log.info("Sending with embed V1 (useDiscordComponentV2=false)")
                embeds = []

                if payload.images:
                    for idx, media in enumerate(payload.images):
                        is_first = idx == 0
                        embed = generate_discord_embed_data(
                            title=payload.cleaned_description if is_first else "",
                            payload=payload,
                            timestamp=timestamp if is_first else None,
                            author_name=twitter_user.name if is_first else None,
                            author_url=twitter_user.link if is_first else None,
                            author_icon_url=author_icon_url if is_first else None,
                        )
                        embed.set_image(url=f"attachment://{media.filename}")
                        embed.url = tweet_link
                        embeds.append(embed)
                else:
                    # No images, text-only embed
                    embed = generate_discord_embed_data(
                        title=payload.cleaned_description,
                        payload=payload,
                        timestamp=timestamp,
                        author_name=twitter_user.name,
                        author_url=twitter_user.link,
                        author_icon_url=author_icon_url,
                    )
                    embed.url = tweet_link
                    embeds.append(embed)

                try:
                    await webhook.send(
                        content=content,
                        embeds=embeds,
                        files=files,
                        username=username,
                        avatar_url=avatar_url,
                    )
                    log.info("✅ Embed V1 posted successfully")
                    return True
                except discord.HTTPException as e:
                    log.error(f"❌ Failed to post embed V1. HTTP {e.status}: {e.text}")
                    return False
        else:
            # Send just content without embed (simple mode)
            log.info("Sending without embed (generateEmbed=False)")
            try:
                await webhook.send(
                    content=content,
                    username=username,
                    avatar_url=avatar_url
                )
                log.info("✅ Embed posted successfully")
                return True
            except discord.HTTPException as e:
                log.error(f"❌ Failed to post embed. HTTP {e.status}: {e.text}")
                return False

    except Exception as webhook_error:
        log.error(f"❌ Error sending to webhook: {webhook_error}")
        return False


async def post_to_single_stoat_webhook(
        session: ClientSession,
        webhook_url: str,
        content: str,
        tweet_link: str,
        payload: WebhookMediaPayload,
        timestamp: float,
        twitter_user: TwitterUser
) -> bool:
    """
    OPTIMIZED: Post to a single webhook for Stoat (videos + embed).
    Extracted for parallel execution across multiple webhooks.
    """
    try:
        log.info(f"Processing webhook: {webhook_url[:50]}...")

        # === STEP 1: Send videos first ===
        if payload.videos:
            log.info(f"📹 Sending {len(payload.videos)} video(s)...")
            for video in payload.videos:
                video_webhook = StoatWebhook(
                    webhook_url=webhook_url,
                    session=session,
                    rate_limit_retry=True
                )

                # Stoat only send URL
                log.info(f"Sending video URL: {video.original_url}")
                video_webhook.content = __video_embed_content.format(video.original_url)

                video_response = await video_webhook.execute()

                if video_response.ok:
                    log.info(f"✅ Video posted: {video.filename}")
                else:
                    log.error(f"❌ Failed to post video. HTTP {video_response.status}")
                    return False

        # === STEP 2: Send content with or without embed ===
        if main_config.config.generateEmbed:
            # Send with embed (original behavior)
            log.info("Sending with embed (generateEmbed=True)")
            webhook = StoatWebhook(
                webhook_url=webhook_url,
                session=session,
                rate_limit_retry=True
            )

            # Attach author icon
            # if payload.author_icon_data:
            #     webhook.add_file(file=payload.author_icon_data, filename=payload.author_icon_filename)
            #     author_icon_url = f"attachment://{payload.author_icon_filename}"
            # else:
            #     author_icon_url = twitter_user.icon

            # forgot to remove, just code try the feature
            # masquerade = StoatMasquerade(
            #     name=twitter_user.name,
            #     avatar=twitter_user.icon,
            #     color=RandomEmbedColor.random_pastel_gradient()
            # )
            # webhook.set_masquerade(masquerade)

            if main_config.config.useCustomProfile:
                masquerade = StoatMasquerade()
                masquerade.name = main_config.profile.username
                masquerade.avatar_url = main_config.profile.avatarUrl
                masquerade.colour = RandomEmbedColor.random_gradient()
                webhook.masquerade = masquerade

            embed = generate_stoat_embed_data(
                title=payload.cleaned_description,
                payload=payload,
                timestamp=timestamp,
                author_name=twitter_user.name,
                author_url=twitter_user.link,
                author_icon_url=twitter_user.icon
            )
            webhook.add_embed(embed)

            # Add Image into Content
            if payload.images:
                for idx, media in enumerate(payload.images):
                    content = f'{content} [{__braille_pattern_blank}]({media.original_url})'

            webhook.set_content(content)
            response = await webhook.execute()
            if response.ok:
                log.info("✅ Embed posted successfully")
                return True
            else:
                log.error(f"❌ Failed to post embed. HTTP {response.status} + {response.reason}")
                return False
        else:
            # Send just content without embed (simple mode)
            log.info("Sending without embed (generateEmbed=False)")
            webhook = StoatWebhook(webhook_url=webhook_url,
                                   session=session,
                                   rate_limit_retry=True)

            if main_config.config.useCustomProfile:
                masquerade = StoatMasquerade()
                masquerade.name = main_config.profile.username
                masquerade.avatar_url = main_config.profile.avatarUrl
                masquerade.colour = RandomEmbedColor.random_gradient()
                webhook.masquerade = masquerade

            webhook.set_content(content)

            response = await webhook.execute()
            if response.ok:
                log.info("✅ Content posted successfully")
                return True
            else:
                log.error(f"❌ Failed to post content. HTTP {response.status}")
                return False

    except Exception as e:
        log.error(f"❌ Error sending to webhook {webhook_url[:50]}...: {e}")
        return False


async def send_to_discord_with_media(session: ClientSession,
                                     tweet_link: str,
                                     embed_title: str,
                                     tweet_media_list: list[TwitterMedia],
                                     tweet_has_video: bool,
                                     timestamp: float,
                                     twitter_user: TwitterUser
                                     ) -> bool:
    """
    OPTIMIZED: Send tweet to Discord with parallel webhook posting.
    If multiple webhooks exist, posts to all concurrently.
    """
    content = tweet_link
    if main_config.config.useFxTwitterLinkInDiscord:
        content = content.replace(__twitter_url, __fxtwitter_url)

    if twitter_user.discordMention and twitter_user.discordMentionRoleId:
        mentions = ' '.join([f'<@&{role_id}>' for role_id in twitter_user.discordMentionRoleId])
        content = f'{content}\n{mentions}'

    # Generate payload once (shared across all webhooks)
    payload = await generate_media_webhook(
        session, tweet_link, embed_title, tweet_media_list,
        tweet_has_video, twitter_user
    )

    # OPTIMIZED: Post to all webhooks in parallel
    if len(twitter_user.discordWebhookUrl) > 1:
        log.info(f"🚀 Posting to {len(twitter_user.discordWebhookUrl)} webhooks in parallel...")
        webhook_tasks = [
            post_to_single_discord_webhook(
                session, webhook_url, content, tweet_link,
                payload, timestamp, twitter_user
            )
            for webhook_url in twitter_user.discordWebhookUrl
        ]

        results = await asyncio.gather(*webhook_tasks, return_exceptions=True)

        # Check if all succeeded
        success_count = sum(1 for r in results if r is True)
        log.info(f"✅ Posted to {success_count}/{len(twitter_user.discordWebhookUrl)} webhooks successfully")

        return success_count > 0  # Return True if at least one webhook succeeded
    else:
        # Single webhook, post directly
        return await post_to_single_discord_webhook(
            session, twitter_user.discordWebhookUrl[0], content, tweet_link,
            payload, timestamp, twitter_user
        )


async def send_to_stoat_with_media(session: ClientSession,
                                   tweet_link: str,
                                   embed_title: str,
                                   tweet_media_list: list[TwitterMedia],
                                   tweet_has_video: bool,
                                   timestamp: float,
                                   twitter_user: TwitterUser
                                   ) -> bool:
    content = tweet_link
    if main_config.config.useFxTwitterLinkInDiscord:
        content = content.replace(__twitter_url, __fxtwitter_url)

    # Havent Research Yet
    # if twitter_user.discordMention and twitter_user.discordMentionRoleId:
    #     mentions = ' '.join([f'<@&{role_id}>' for role_id in twitter_user.discordMentionRoleId])
    #     content = f'{content}\n{mentions}'

    # Generate payload once (shared across all webhooks)
    payload = await generate_media_webhook(
        session, tweet_link, embed_title, tweet_media_list,
        tweet_has_video, twitter_user
    )

    # OPTIMIZED: Post to all webhooks in parallel
    if len(twitter_user.stoatWebhookUrl) > 1:
        log.info(f"🚀 Posting to {len(twitter_user.stoatWebhookUrl)} webhooks in parallel...")
        webhook_tasks = [
            post_to_single_stoat_webhook(
                session, webhook_url, content, tweet_link,
                payload, timestamp, twitter_user
            )
            for webhook_url in twitter_user.stoatWebhookUrl
        ]

        results = await asyncio.gather(*webhook_tasks, return_exceptions=True)

        # Check if all succeeded
        success_count = sum(1 for r in results if r is True)
        log.info(f"✅ Posted to {success_count}/{len(twitter_user.stoatWebhookUrl)} webhooks successfully")

        return success_count > 0  # Return True if at least one webhook succeeded
    else:
        # Single webhook, post directly
        return await post_to_single_stoat_webhook(
            session, twitter_user.stoatWebhookUrl[0], content, tweet_link,
            payload, timestamp, twitter_user
        )


async def main():
    """Main async function with performance timing"""
    try:
        script_start = time.time()

        # Config Migration
        migrate_old_checkpoint()

        # CHANGED: Read per-handler checkpoints instead of single timestamp
        checkpoints = read_last_run_per_handler()  # dict[handler -> datetime]

        # Create aiohttp session with optimized settings
        connector = TCPConnector(
            limit=30,
            limit_per_host=10,
            ttl_dns_cache=300,
            force_close=False,
            enable_cleanup_closed=True
        )

        timeout = ClientTimeout(total=60, connect=10)

        async with ClientSession(
                connector=connector,
                timeout=timeout,
                headers={
                    'User-Agent': 'curl/8.16.0',
                    'Accept-Encoding': 'gzip, deflate',
                    'Connection': 'keep-alive',
                }
        ) as session:

            twitter_user_list: list[TwitterUser] = []
            entry_data: list[EntryData] = []
            handle_list = [item.twitterHandleName for item in main_config.twitterWatch]

            nitter_server_distribution_list = random.choices(main_config.nitterServer, k=len(main_config.twitterWatch))

            # Create tasks for fetching all RSS feeds
            feed_tasks = []
            for item, nitter in zip(main_config.twitterWatch, nitter_server_distribution_list):
                feed_tasks.append(fetch_rss_feed(session, item.twitterHandleName, nitter))

            # Fetch all feeds concurrently
            log.info(f"Fetching {len(feed_tasks)} RSS feeds concurrently...")
            rss_start = time.time()
            feed_results = await asyncio.gather(*feed_tasks, return_exceptions=True)
            log.info(f"⏱️ RSS fetch took: {time.time() - rss_start:.2f}s")

            # Process feed results
            for item, feedParse in zip(main_config.twitterWatch, feed_results):
                if isinstance(feedParse, Exception) or feedParse is None:
                    log.warning(f"Failed to fetch feed for {item.twitterHandleName}")
                    continue

                if not hasattr(feedParse, 'feed') or not feedParse.entries:
                    log.warning(f"Invalid or empty feed for {item.twitterHandleName}")
                    continue

                # CHANGED: Get cutoff time for THIS specific handler
                handler_name = item.twitterHandleName
                cutoff_candidates = []
                if item.discordWebhookUrl:
                    cutoff_candidates.append(checkpoints.get("discord", handler_name))
                if item.stoatWebhookUrl:
                    cutoff_candidates.append(checkpoints.get("stoat", handler_name))

                # Fallback if somehow neither is configured
                cutoff_time = min(cutoff_candidates) if cutoff_candidates else datetime.now(UTC)
                log.info(f"Processing {handler_name}, cutoff: {cutoff_time} (UTC)")

                # Add to twitter_user_list
                if hasattr(feedParse.feed, 'image'):
                    twitter_user_list.append(
                        generate_twitter_user_from_rss(
                            feed_data=feedParse,
                            key=handler_name,
                            discord_webhook_url=item.discordWebhookUrl,
                            discord_mention=item.discordMention,
                            discord_mention_role_id=item.discordMentionRoleId,
                            stoat_webhook_url=item.stoatWebhookUrl
                        )
                    )
                else:
                    twitter_user_list.append(
                        TwitterUser(
                            name=handler_name,
                            link=f"{__twitter_url}/{handler_name}",
                            icon="",
                            key=handler_name,
                            discordWebhookUrl=item.discordWebhookUrl,
                            discordMention=item.discordMention,
                            discordMentionRoleId=item.discordMentionRoleId,
                            stoatWebhookUrl=item.stoatWebhookUrl
                        )
                    )

                # Process entries
                for data in feedParse.entries:
                    pub_date = dateutil.parser.parse(timestr=data.published)
                    pub_date = normalize_datetime_to_utc_naive(pub_date)

                    # CHANGED: Compare against THIS handler's cutoff time
                    if pub_date <= cutoff_time:
                        continue

                    extracted_media, video_detected = extract_media_from_description(
                        data.description,
                        __twitter_image_card_link_template
                    )

                    temp_data = EntryData(
                        title=data.title,
                        description=data.description,
                        link=replace_url_to_twitter(data.link, __twitter_url),
                        pubdate=pub_date,
                        timestamp=generate_timestamp(data.published_parsed),
                        key=handler_name,
                        mediaList=extracted_media,
                        hasVideo=video_detected
                    )

                    if temp_data.is_retweet():
                        if main_config.config.includeReTweet:
                            entry_data.append(temp_data)
                    else:
                        entry_data.append(temp_data)

            # Sort by publication date (oldest first)
            entry_data = sorted(entry_data, key=attrgetter('pubdate'))
            log.info(f"Found {len(entry_data)} new entries to post")

            # Post count
            posted_count = 0
            # Now using dataclass for multiple target (Stoat and Discord)
            latest = DestinationCheckpoints()

            post_start = time.time()

            for data in entry_data:
                twitter_user = next((item for item in twitter_user_list if item.key == data.key), None)
                if not twitter_user:
                    log.warning(f"No user found for key {data.key}")
                    continue

                try:
                    log.info(f"Posting tweet from {data.key} at {data.pubdate}: {data.link}")

                    discord_success = False
                    stoat_success = False

                    if twitter_user.discordWebhookUrl and data.pubdate > checkpoints.get("discord", data.key):
                        discord_success = await send_to_discord_with_media(
                            session=session,
                            tweet_link=data.link,
                            embed_title=data.description,
                            tweet_media_list=data.mediaList,
                            tweet_has_video=data.hasVideo,
                            timestamp=data.timestamp,
                            twitter_user=twitter_user
                        )
                        if discord_success:
                            latest.update("discord", data.key, data.pubdate)

                    if twitter_user.stoatWebhookUrl and data.pubdate > checkpoints.get("stoat", data.key):
                        stoat_success = await send_to_stoat_with_media(
                            session=session,
                            tweet_link=data.link,
                            embed_title=data.description,
                            tweet_media_list=data.mediaList,
                            tweet_has_video=data.hasVideo,
                            timestamp=data.timestamp,
                            twitter_user=twitter_user
                        )
                        if stoat_success:
                            latest.update("stoat", data.key, data.pubdate)

                    if discord_success or stoat_success:
                        posted_count += 1
                        log.info(f"✅ Successfully posted tweet from {data.key} at {data.pubdate}")

                except Exception as post_error:
                    log.error(f"Error posting: {post_error}")
                    continue

            log.info(f"⏱️ Webhook posting took: {time.time() - post_start:.2f}s")
            log.info(f"Successfully posted {posted_count}/{len(entry_data)} tweets")

            # Merge latest into checkpoints and save
            # Addition : Remove non exist handler / removed twitter handle
            if latest.destinations:
                checkpoints.remove_nonexist_handler(handle_list)
                checkpoints.merge(latest)
                write_last_run_per_handler(checkpoints)

                for dest, handlers in latest.destinations.items():
                    for handler, ts in handlers.items():
                        log.info(f"  - [{dest}] {handler}: {ts}")
            elif entry_data and posted_count == 0:
                log.warning("⚠️ Had entries but failed to post any - NOT updating checkpoint")
            else:
                log.info("No new entries found, checkpoint unchanged")

            log.info(f"⏱️ Total script execution time: {time.time() - script_start:.2f}s")
            log.info(f"📊 Cache stats: {len(_author_icon_cache)} author icons cached")
            log.info("Script completed successfully")

    except Exception as main_error:
        log.exception(f"Caught an exception: {main_error}")
        exit(1)


if __name__ == "__main__":
    # Run the async main function
    with ScriptLock('kuri.lock', script_dir_param=script_dir, logger=log) as lock:
        asyncio.run(main())
