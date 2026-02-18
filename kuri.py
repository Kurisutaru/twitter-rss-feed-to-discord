import asyncio
import atexit
import calendar
import hashlib
import os
import random
import re
import shutil
import signal
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from operator import attrgetter
from os.path import isfile
from pathlib import Path
from typing import List, Optional, Union
from urllib.parse import urljoin, unquote, urlparse
from warnings import deprecated

import dateutil.parser
import feedparser
import orjson
from aiohttp import ClientSession, TCPConnector, ClientTimeout
from bs4 import BeautifulSoup
from discord_webhook import DiscordEmbed, DiscordWebhook
from feedparser import FeedParserDict
from loguru import logger as log
# Keep lxml import – used in RSS patching
# noinspection PyUnresolvedReferences
from lxml import etree
from mashumaro.mixins.json import DataClassJSONMixin

from stoat_webhook import StoatWebhook, StoatMasquerade, StoatEmbed

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
    """Prevents multiple instances of the script from running simultaneously"""

    def __init__(self, lock_file='script.lock', logger=None):
        self.lock_file = os.path.join(script_dir, lock_file)
        self.locked = False
        self.log = logger if logger else log

    def acquire(self):
        """Acquire the lock. If another instance is running, exit."""
        if os.path.exists(self.lock_file):
            try:
                with open(self.lock_file, 'r') as file:
                    old_pid = int(file.read().strip())

                if self._is_process_running(old_pid):
                    self.log.warning(f"Script is already running (PID: {old_pid}). Exiting.")
                    sys.exit(0)
                else:
                    self.log.info(f"Removing stale lock file (PID: {old_pid})")
                    os.remove(self.lock_file)
            except (ValueError, IOError):
                self.log.warning("Removing invalid lock file")
                os.remove(self.lock_file)

        try:
            with open(self.lock_file, 'w') as file:
                file.write(str(os.getpid()))
            self.locked = True
            self.log.info(f"Lock acquired (PID: {os.getpid()})")

            atexit.register(self.release)
            signal.signal(signal.SIGTERM, self._signal_handler)
            signal.signal(signal.SIGINT, self._signal_handler)
            return True
        except IOError as e:
            self.log.error(f"Failed to create lock file: {e}")
            sys.exit(1)

    def release(self):
        """Release the lock by removing the lock file"""
        if self.locked and os.path.exists(self.lock_file):
            try:
                os.remove(self.lock_file)
                self.locked = False
                self.log.info("Lock released")
            except OSError as e:
                self.log.error(f"Failed to remove lock file: {e}")

    def _signal_handler(self, signum, frame):
        """Handle termination signals"""
        self.log.info(f"Received signal {signum}, releasing lock")
        self.release()
        sys.exit(0)

    def _is_process_running(self, pid):
        """Check if a process with the given PID is running (cross-platform)."""
        import platform

        if platform.system() == "Windows":
            import subprocess
            try:
                result = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid}', '/NH', '/FO', 'CSV'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                return str(pid) in result.stdout
            except (subprocess.SubprocessError, FileNotFoundError):
                return False
        else:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False


# ===== PROCESS LOCK =====
lock = ScriptLock('kuri.lock')
lock.acquire()

# emoji
__emoji_play = '▶️'
__emoji_video = '🎬'
__emoji_photo = '🖼️'

# Special character
__braille_pattern_blank = '\u2800'

# Variable
__twitter_url: str = 'https://x.com'
__fxtwitter_url: str = 'https://fxtwitter.com'
__twitter_image_card_link_template: str = 'https://pbs.twimg.com/{}{}'
__post_video_identifier: str = 'ext_tw_video_thumb'
__rss_template: str = '{}/{}/rss'
__video_embed_content: str = f"{__emoji_play}[{__braille_pattern_blank}]({{}})"
__video_upload_content: str = f"{__emoji_play}{__braille_pattern_blank}"
__footer_append_template: str = ' • {}'
__discord_maximum_file_size: int = 10
__discord_maximum_embed_character: int = 4096

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
    mediaList: List['TwitterMedia'] = field(default_factory=list)
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
    embedFooterText: str
    embedFooterImageUrl: str
    includeReTweet: bool
    generateEmbed: bool
    useFxTwitterLinkInDiscord: bool


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
    twitterWatch: list[TwitterWatch]


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
    data: Optional[bytes]
    media_type: MediaType
    original_url: str


@dataclass
class WebhookMediaPayload:
    """Complete payload for webhook with proper separation of concerns"""
    # Author info
    author_icon_data: Optional[bytes] = None
    author_icon_filename: Optional[str] = None

    # Videos (DownloadedMedia with data=bytes for uploaded, data=None for URL-only)
    videos: List[DownloadedMedia] = None

    # Images to attach and embed
    images: List[DownloadedMedia] = None

    # All attachments (images + author icon)
    all_attachments: List[DownloadedMedia] = None

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


@dataclass(frozen=True)
class RandomEmbedColor:
    """
    Generates a random colour and provides it in multiple formats useful for:
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
        """Factory method to create a random opaque colour."""
        return cls(
            r=random.randint(0, 255),
            g=random.randint(0, 255),
            b=random.randint(0, 255),
            a=255
        )

    @classmethod
    def random_pastel(cls, saturation: float = 0.5) -> "RandomEmbedColor":
        """
        Generate a random pastel colour.

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
        """Random colour with custom alpha (0–255)."""
        return cls.random().__class__(r=cls.random().r, g=cls.random().g, b=cls.random().b, a=alpha)

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
    def int(self) -> int:
        """Discord-style integer (0xRRGGBB)."""
        return (self.r << 16) | (self.g << 8) | self.b

    @property
    def discord_int(self) -> int:
        """Same as .int — explicit alias for Discord."""
        return self.int

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
        return self.int


# Check if config file exist, if not abort
if not isfile(__json_file):
    log.error("Config file not found, abort current running script")
    exit(1)

with open(__json_file, 'r') as f:
    main_config = TwitterDiscordConfig.from_json(f.read())

nitter_url_list: list[str] = [*main_config.nitterServer, 'http://nitter.net']


def read_last_run(filename: str = __last_run_file) -> datetime:
    """Read the last processed tweet timestamp from file."""
    try:
        if os.path.exists(filename):
            with open(filename, 'r') as file:
                timestamp_str = file.read()
                timestamp_str = timestamp_str.strip()
                last_run = datetime.fromisoformat(timestamp_str)
                if last_run.tzinfo is not None:
                    last_run = last_run.astimezone(timezone.utc).replace(tzinfo=None)
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
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)

        with open(filename, 'w') as file:
            file.write(timestamp.isoformat())
        log.info(f"Updated last processed tweet timestamp: {timestamp} (UTC)")
    except Exception as write_error:
        log.error(f"Error writing last run timestamp: {write_error}")


def read_last_run_per_handler(filename: str = __last_run_file) -> dict[str, datetime]:
    """
    Read per-handler timestamps from JSON file using orjson.
    Returns dict mapping handler name -> last processed datetime (UTC, timezone-naive).
    """
    try:
        if os.path.exists(filename):
            with open(filename, 'rb') as file:
                data = orjson.loads(file.read())

            result = {}
            for handler, timestamp_str in data.items():
                # CHANGED: Use dateutil.parser instead of datetime.fromisoformat
                dt = dateutil.parser.parse(timestamp_str)

                # Normalize to UTC timezone-naive
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                result[handler] = dt

            log.info(f"Loaded checkpoints for {len(result)} handlers")
            return result
        else:
            log.info("No last_run.json found, starting fresh for all handlers")
            return {}
    except Exception as e:
        log.error(f"Error reading checkpoints: {e}, using empty dict")
        return {}


def write_last_run_per_handler(checkpoints: dict[str, datetime], filename: str = __last_run_file):
    """
    Write per-handler timestamps to JSON file with atomic write pattern.
    Prevents corruption if script crashes during write.
    """
    try:
        # Prepare data: convert datetime to ISO strings
        data = {}
        for handler, dt in checkpoints.items():
            # Ensure UTC timezone-naive
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            data[handler] = dt.isoformat()

        # Get directory for temp file (must be same filesystem for atomic rename)
        file_dir = os.path.dirname(filename) or script_dir

        # Create temp file in same directory
        fd, temp_path = tempfile.mkstemp(
            suffix='.tmp',
            prefix='last_run_',
            dir=file_dir,
            text=False  # Binary mode for orjson
        )

        try:
            # Write to temp file
            with os.fdopen(fd, 'wb') as temp_file:
                temp_file.write(orjson.dumps(data, option=orjson.OPT_INDENT_2))

            # Atomic rename (replaces old file)
            shutil.move(temp_path, filename)

            log.info(f"✅ Atomically updated checkpoints for {len(checkpoints)} handlers")

        except Exception as write_error:
            # Clean up temp file on failure
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise write_error

    except Exception as e:
        log.error(f"Error writing checkpoints: {e}")


def migrate_old_checkpoint():
    """
    One-time migration from single-file last_run.txt to per-handler last_run.json.
    Safe to run multiple times - only migrates if old file exists and new doesn't.
    """
    old_file = os.path.join(script_dir, 'last_run.txt')
    new_file = os.path.join(script_dir, 'last_run.json')

    if os.path.exists(old_file) and not os.path.exists(new_file):
        try:
            log.info("Found old last_run.txt, migrating to per-handler format...")

            # Read old timestamp using existing function
            old_timestamp = read_last_run(old_file)

            # Apply to all handlers
            checkpoints = {
                item.twitterHandleName: old_timestamp
                for item in main_config.twitterWatch
            }

            # Write new format
            write_last_run_per_handler(checkpoints, new_file)

            log.info(f"✅ Migrated checkpoint to per-handler format for {len(checkpoints)} handlers")

            # Backup old file
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
    return datetime.fromtimestamp(input_time)


def convert_mb_to_bytes(input_mb: int) -> int:
    return input_mb * 1024 * 1024


def convert_bytes_to_mb(input_mb: int) -> float:
    return input_mb / 1024 / 1024


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
    Optional[bytes], bool]:
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

    except asyncio.TimeoutError:
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
    return_string = input_string.replace(' / ', ' (') + str(')')
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
        query_param = str('?') + url_parse_data.query
    return_string = twitter_image_card_link_template.format(url_parse_data.path, query_param).replace('/pic/', '')
    return return_string


def normalize_datetime_to_utc_naive(dt: datetime) -> datetime:
    """Normalize any datetime to UTC timezone-naive format for consistent comparison."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
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


def extract_media_from_description(description: str, twitter_card_template: str) -> tuple[List[TwitterMedia], bool]:
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
    Clean tweet description by:
    1. Removing images and videos
    2. Converting links to Discord Markdown format
    3. Replacing nitter URLs with x.com
    4. Showing full URLs without protocol ONLY for truncated links (containing ...)
    5. Keeping hashtag and mention links with their original text
    6. Keeping protocol for specific domains in the exclusion list
    7.a. Converting blockquotes to Discord multi-line quote format (>>> prefix) < this not work
    7.b. Converting blockquotes to Discord multi-single-line quote format (>{space}) < this work
    """
    soup = BeautifulSoup(html_content, 'lxml')

    # List of domains that should keep the protocol in display text
    keep_protocol_domains = [
        'discord.gg',
        'discord.com',
    ]

    # === CONVERT <br> TO NEWLINES (but remove <br> inside links) ===
    # Remove <br> tags that are inside <a> tags (they break Markdown links)
    for link in soup.find_all('a'):
        for br in link.find_all('br'):
            br.decompose()

    # === PROCESS BLOCKQUOTES FIRST (but keep structure for now) ===
    blockquotes = soup.find_all('blockquote')
    for blockquote in blockquotes:
        # Mark blockquote with a special marker that survives text extraction
        blockquote.insert(0, soup.new_string('\n__QUOTE_START__\n\n'))
        blockquote.append(soup.new_string('\n__QUOTE_END__\n'))

    # Remove images and videos (but keep links!)
    for tag in soup.find_all(['img', 'video', 'source']):
        tag.decompose()

    # Process all links
    for link in soup.find_all('a'):
        href = link.get('href', '')
        # Re-get text after normalization
        link_text = ' '.join(link.get_text().split())
        link_text = replace_nitter_url_to_twitter_url(link_text)

        if not href:
            continue

        # Skip hashtag links - we'll handle them separately
        if link_text.startswith('#'):
            continue

        # Replace nitter URL to twitter URL
        href = replace_nitter_url_to_twitter_url(href)

        # Check if domain should keep protocol
        should_keep_protocol = any(domain in href for domain in keep_protocol_domains)

        # Check if link text is a URL (starts with http:// or https://)
        link_text_is_url = link_text.startswith('http://') or link_text.startswith('https://')

        # Check if link text matches href (normalized comparison)
        link_text_normalized = link_text.replace('http://', '').replace('https://', '').strip()
        href_normalized = href.replace('http://', '').replace('https://', '').strip()
        text_matches_href = link_text_normalized == href_normalized

        if should_keep_protocol:
            link.replace_with(href)
        elif link_text_is_url and text_matches_href:
            # Link text is a URL that matches href - use Markdown with protocol removed
            display_text = link_text.replace('https://', '').replace('http://', '')
            link.replace_with(f'[{display_text}]({href})')
        elif '…' in link_text or '...' in link_text:
            # Truncated link - show full URL without protocol
            display_text = href.replace('https://', '').replace('http://', '')
            link.replace_with(f'[{display_text}]({href})')
        else:
            # Descriptive text - keep as is
            link.replace_with(f'[{link_text}]({href})')

    # Remove all remaining hashtag links (we'll recreate them)
    for link in soup.find_all('a'):
        link_text = link.get_text()
        if link_text.startswith('#'):
            link.replace_with(link_text)

    # Get text directly - this preserves all newlines
    text = soup.get_text()

    # Convert standalone hashtags to clickable links
    import re
    from urllib.parse import quote

    def replace_hashtag(match):
        hashtag_with_hash = match.group(0)
        hashtag_without_hash = hashtag_with_hash[1:]
        # URL encode the hashtag text
        encoded = quote(hashtag_without_hash)
        return f'[{hashtag_with_hash}](https://x.com/hashtag/{encoded})'

    # Match hashtags that are NOT already inside Markdown links
    hashtag_pattern = r'(?<!\[)#\w+(?!\]\()'
    text = re.sub(hashtag_pattern, replace_hashtag, text)

    # === NOW PROCESS THE QUOTE MARKERS ===
    # Split by quote markers and add > prefix to each line of quoted sections (Discord single-line quote)
    if '__QUOTE_START__' in text and '__QUOTE_END__' in text:
        parts = []
        segments = text.split('__QUOTE_START__')

        for i, segment in enumerate(segments):
            if i == 0:
                # First segment is before any quote
                if segment.strip():
                    parts.append(segment.strip())
            else:
                # This segment contains a quote
                if '__QUOTE_END__' in segment:
                    quote_part, after_quote = segment.split('__QUOTE_END__', 1)

                    # Clean up the quote part
                    quote_text = quote_part.strip()

                    if quote_text:
                        # Add "> " prefix to each line instead of ">>> "
                        quoted_lines = [f"> {line}" for line in quote_text.split('\n')]
                        parts.append('\n'.join(quoted_lines))

                    # Add the part after the quote
                    if after_quote.strip():
                        parts.append(after_quote.strip())

        text = '\n\n'.join(parts)

    # Clean up excessive whitespace
    lines = text.split('\n')
    cleaned_lines = []
    prev_empty = False

    for line in lines:
        stripped = line.strip()
        if stripped:
            cleaned_lines.append(line)
            prev_empty = False
        elif not prev_empty:
            # Allow one empty line
            cleaned_lines.append('')
            prev_empty = True

    text = '\n'.join(cleaned_lines)

    # Strip only leading/trailing whitespace
    return text.strip()


@deprecated("Use RandomEmbedColor")
def generate_embed_color() -> int:
    # Generate a random integer between 0 and 0xFFFFFF
    random_color = random.randint(0, 0xFFFFFF)
    return random_color


def generate_discord_embed_data(title: str, payload: WebhookMediaPayload,
                                timestamp: float, author_name: str = None, author_url: str = None,
                                author_icon_url: str = None) -> DiscordEmbed:
    """Create embed object for webhook."""
    embed = DiscordEmbed()

    if author_name and author_url:
        embed.set_author(name=author_name,
                         url=author_url,
                         icon_url=author_icon_url)

    embed.set_color(RandomEmbedColor.random().discord_int)

    if timestamp:
        embed.set_timestamp(timestamp)

    if title:
        embed.set_description(clean_tweet_text(title))

    footer = main_config.config.embedFooterText

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


async def fetch_rss_feed(session: ClientSession, twitter_handle: str, nitter_url: str) -> Optional[FeedParserDict]:
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
    except asyncio.TimeoutError:
        log.error(f"Timeout fetching RSS for {twitter_handle}")
        return None
    except Exception as e:
        log.error(f"Error fetching RSS for {twitter_handle}: {e}")
        return None


async def download_media(session: ClientSession, url: str, max_size: int = convert_mb_to_bytes(25)) -> Optional[bytes]:
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
    except asyncio.TimeoutError:
        log.error(f"Timeout downloading: {url}")
        return None
    except Exception as e:
        log.error(f"Error downloading {url}: {e}")
        return None


async def get_author_icon(session: ClientSession, url: str) -> Optional[bytes]:
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
        log.debug(f"Cached author icon for future use")

    return data


async def fetch_video_from_fxtwitter(session: ClientSession, tweet_link: str) -> Optional[str]:
    """Fetch video URL from fxtwitter meta tags"""
    fx_url = tweet_link.replace(__twitter_url, __fxtwitter_url)

    try:
        log.info(f"Fetching video from fxtwitter: {fx_url}")
        async with session.get(fx_url, timeout=ClientTimeout(total=60)) as response:
            if response.ok:
                html = await response.text()
                soup = BeautifulSoup(html, 'lxml')

                # Try multiple meta tags in priority order
                for tag in ['twitter:player:stream', 'og:video:secure_url', 'og:video']:
                    meta = soup.find('meta', property=tag)
                    if meta and meta.get('content'):
                        video_url = meta.get('content')
                        log.info(f"✅ Found video URL from {tag}: {video_url}")
                        return video_url

                log.warning(f"No video meta tags found in {fx_url}")
                return None
            else:
                log.warning(f"Failed to fetch fxtwitter page: HTTP {response.status}")
                return None
    except asyncio.TimeoutError:
        log.error(f"Timeout fetching fxtwitter page: {fx_url}")
        return None
    except Exception as e:
        log.error(f"Error fetching video from fxtwitter: {e}")
        return None


async def generate_media_webhook(
        session: ClientSession,
        tweet_link: str,
        title: str,
        tweet_media_list: List[TwitterMedia],
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
    video_thumbnails = [m for m in tweet_media_list if m.type == 'video_thumbnail']
    images = [m for m in tweet_media_list if m.type == 'image']

    # === VIDEO HANDLING ===
    if tweet_has_video:
        log.info(f"Tweet have video")
        if videos_from_rss:
            # Use video from RSS
            video_url = videos_from_rss[0].url
            log.info(f"Using video URL from RSS: {video_url}")
        else:
            # Fetch from fxtwitter
            log.info("🎥 Getting video link from fxtwitter...")
            video_url = await fetch_video_from_fxtwitter(session, tweet_link)

            if video_url:
                log.info(f"✅ Got video URL from fxtwitter")
            else:
                log.warning("❌ Failed to get video from fxtwitter, will use thumbnails")
                video_url = None

        if video_url:
            # OPTIMIZED: Use smart download (no separate HEAD request)
            log.info("Downloading video with smart size check...")
            filename = generate_media_filename(video_url)

            video_data, was_too_large = await download_video_smart(session, video_url, max_size=max_file_size)

            if video_data:
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

            payload.video_uploaded = True

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
    if tweet_has_video and not payload.video_uploaded:
        for media in video_thumbnails:
            log.info(f"Queueing video thumbnail download (fallback): {media.url}")
            download_tasks.append(download_media(session, media.url, max_size=max_file_size))
            task_metadata.append((MediaType.VIDEO_THUMBNAIL, media.url))

    # Download all concurrently
    if download_tasks:
        log.info(f"🚀 Starting {len(download_tasks)} concurrent downloads...")
        download_results = await asyncio.gather(*download_tasks, return_exceptions=True)
        log.info(f"✅ All downloads completed")

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
            log.info(f"📹 Sending {len(payload.videos)} video(s)...")
            for video in payload.videos:
                video_webhook = DiscordWebhook(
                    url=webhook_url,
                    rate_limit_retry=True
                )

                if video.data:
                    # Video was downloaded, upload as file
                    log.info(f"Uploading video as file: {video.filename}")
                    video_webhook.content = __video_upload_content
                    video_webhook.add_file(file=video.data, filename=video.filename)
                else:
                    # Video too large or download failed, send URL
                    log.info(f"Sending video URL: {video.original_url}")
                    video_webhook.content = __video_embed_content.format(video.original_url)

                video_response = video_webhook.execute()

                if video_response.ok:
                    log.info(f"✅ Video posted: {video.filename}")
                else:
                    log.error(f"❌ Failed to post video. HTTP {video_response.status_code}")
                    return False

        # === STEP 2: Send content with or without embed ===
        if main_config.config.generateEmbed:
            # Send with embed (original behavior)
            log.info("Sending with embed (generateEmbed=True)")
            webhook = DiscordWebhook(url=webhook_url, content=content, rate_limit_retry=True)

            # Attach author icon
            if payload.author_icon_data:
                webhook.add_file(file=payload.author_icon_data, filename=payload.author_icon_filename)
                author_icon_url = f"attachment://{payload.author_icon_filename}"
            else:
                author_icon_url = twitter_user.icon

            # Attach all images
            for media in payload.all_attachments:
                webhook.add_file(file=media.data, filename=media.filename)

            # Create embeds
            if payload.images:
                for idx, media in enumerate(payload.images):
                    is_first = idx == 0
                    embed = generate_discord_embed_data(
                        title=payload.cleaned_description if is_first else "",
                        payload=payload,
                        timestamp=timestamp if is_first else None,
                        author_name=twitter_user.name if is_first else None,
                        author_url=twitter_user.link if is_first else None,
                        author_icon_url=author_icon_url if is_first else None
                    )
                    embed.set_image(url=f"attachment://{media.filename}")
                    embed.set_url(tweet_link)
                    webhook.add_embed(embed)
            else:
                # No images, just text embed
                embed = generate_discord_embed_data(
                    title=payload.cleaned_description,
                    payload=payload,
                    timestamp=timestamp,
                    author_name=twitter_user.name,
                    author_url=twitter_user.link,
                    author_icon_url=author_icon_url
                )
                embed.set_url(tweet_link)
                webhook.add_embed(embed)

            response = webhook.execute()
            if response.ok:
                log.info(f"✅ Embed posted successfully")
                return True
            else:
                log.error(f"❌ Failed to post embed. HTTP {response.status_code} + {response.reason}")
                return False
        else:
            # Send just content without embed (simple mode)
            log.info("Sending without embed (generateEmbed=False)")
            webhook = DiscordWebhook(url=webhook_url, content=content, rate_limit_retry=True)

            response = webhook.execute()
            if response.ok:
                log.info(f"✅ Content posted successfully")
                return True
            else:
                log.error(f"❌ Failed to post content. HTTP {response.status_code}")
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
            #     colour=RandomEmbedColor.random_pastel_gradient()
            # )
            # webhook.set_masquerade(masquerade)

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
                    is_first = idx == 0
                    content = f'{content} [{__braille_pattern_blank}]({media.original_url})'

            webhook.set_content(content)
            response = await webhook.execute()
            if response.ok:
                log.info(f"✅ Embed posted successfully")
                return True
            else:
                log.error(f"❌ Failed to post embed. HTTP {response.status} + {response.reason}")
                return False
        else:
            # Send just content without embed (simple mode)
            log.info("Sending without embed (generateEmbed=False)")
            webhook = StoatWebhook(webhook_url=webhook_url, rate_limit_retry=True)
            webhook.set_content(content)

            response = await webhook.execute()
            if response.ok:
                log.info(f"✅ Content posted successfully")
                return True
            else:
                log.error(f"❌ Failed to post content. HTTP {response.status_code}")
                return False

    except Exception as webhook_error:
        log.error(f"❌ Error sending to webhook: {webhook_error}")
        return False

        # Create webhook instance (no content here)
        webhook = StoatWebhook(
            webhook_url=webhook_url,
            session=session,
            rate_limit_retry=True
        )

        # forgot to remove, just code try the feature
        # masquerade = StoatMasquerade(
        #     name=twitter_user.name,
        #     avatar=twitter_user.icon,
        #     colour=RandomEmbedColor.random_pastel_gradient()
        # )
        # webhook.set_masquerade(masquerade)

        if main_config.config.generateEmbed:
            log.info("Sending with embed (generateEmbed=True)")

            # Build embed
            embed = generate_stoat_embed_data(
                title=payload.cleaned_description,
                payload=payload,
                timestamp=timestamp,
                author_name=twitter_user.name,
                author_url=twitter_user.link,
                author_icon_url=twitter_user.icon
            )
            webhook.add_embed(embed)

            webhook.set_content(content)

        else:
            log.info("Sending without embed (generateEmbed=False)")
            # Just content (tweet link or whatever)
            webhook.set_content(content)

        # Execute
        response = await webhook.execute(
            remove_embeds=True,
            remove_attachments=True,
            clear_state=True
        )

        if response.ok:
            log.info(f"✅ Posted successfully (HTTP {response.status})")
            return True
        else:
            text = await response.text()
            log.error(f"❌ Failed to post. HTTP {response.status} - {text}")
            return False

    except Exception as e:
        log.error(f"❌ Error sending to webhook {webhook_url[:50]}...: {e}")
        return False


async def send_to_discord_with_media(session: ClientSession,
                                     tweet_link: str,
                                     embed_title: str,
                                     tweet_media_list: List[TwitterMedia],
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
                                   tweet_media_list: List[TwitterMedia],
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

            twitter_user_list: List[TwitterUser] = []
            entry_data: List[EntryData] = []

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
                cutoff_time = checkpoints.get(handler_name, datetime(2000, 1, 1))
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
            # CHANGED: Track latest post per handler
            latest_by_handler: dict[str, datetime] = {}

            post_start = time.time()

            for data in entry_data:
                twitter_user = next((item for item in twitter_user_list if item.key == data.key), None)
                if not twitter_user:
                    log.warning(f"No user found for key {data.key}")
                    continue

                try:
                    log.info(f"Posting tweet from {data.key} at {data.pubdate}: {data.link}")
                    if twitter_user.discordWebhookUrl:
                        success = await send_to_discord_with_media(
                            session=session,
                            tweet_link=data.link,
                            embed_title=data.description,
                            tweet_media_list=data.mediaList,
                            tweet_has_video=data.hasVideo,
                            timestamp=data.timestamp,
                            twitter_user=twitter_user
                        )
                    if twitter_user.stoatWebhookUrl:
                        success = await send_to_stoat_with_media(
                            session=session,
                            tweet_link=data.link,
                            embed_title=data.description,
                            tweet_media_list=data.mediaList,
                            tweet_has_video=data.hasVideo,
                            timestamp=data.timestamp,
                            twitter_user=twitter_user
                        )

                    if success:
                        posted_count += 1

                        # CHANGED: Track latest successful post per handler
                        if data.key not in latest_by_handler or data.pubdate > latest_by_handler[data.key]:
                            latest_by_handler[data.key] = data.pubdate

                        log.info(f"✅ Successfully posted tweet from {data.key} at {data.pubdate}")

                except Exception as post_error:
                    log.error(f"Error posting to Discord: {post_error}")
                    continue

            log.info(f"⏱️ Webhook posting took: {time.time() - post_start:.2f}s")
            log.info(f"Successfully posted {posted_count}/{len(entry_data)} tweets")

            # CHANGED: Update per-handler checkpoints
            if latest_by_handler:
                # Merge with existing checkpoints (don't lose other handlers)
                updated_checkpoints = {**checkpoints, **latest_by_handler}
                write_last_run_per_handler(updated_checkpoints)

                log.info(f"✅ Updated checkpoints for {len(latest_by_handler)} handlers:")
                for handler, timestamp in latest_by_handler.items():
                    log.info(f"  - {handler}: {timestamp}")
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
    asyncio.run(main())
