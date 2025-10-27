import asyncio
import atexit
import calendar
import hashlib
import os
import random
import re
import signal
import sys
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

import dateutil.parser
import feedparser
from aiohttp import ClientSession, TCPConnector, ClientTimeout
from bs4 import BeautifulSoup
from discord_webhook import DiscordEmbed, DiscordWebhook
from feedparser import FeedParserDict
from loguru import logger as log
# Keep lxml import – used in RSS patching
# noinspection PyUnresolvedReferences
from lxml import etree
from mashumaro.mixins.json import DataClassJSONMixin

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

# JSONFile
__json_file: str = 'kuri.config.json'
__last_run_file: str = os.path.join(script_dir, 'last_run.txt')

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
    webhookUrl: list[str]
    discordMention: bool
    discordMentionRoleId: list[str]


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
    webhookUrl: list[str] = field(default_factory=list)
    discordMention: bool = False
    discordMentionRoleId: list[str] = field(default_factory=list)


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

    Input: http://nitter.net/pic/video.twimg.com%2Ftweet_video%2Ffile.mp4
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
        return actual_url.replace('http://', 'https://')

    # Fallback: try to clean it manually
    cleaned = unquoted.replace('http://', 'https://').replace('/pic/', '')

    # Remove nitter domains
    for domain in nitter_url_list:
        cleaned = cleaned.replace(domain, '')

    # Clean up any double slashes (except after https:)
    cleaned = re.sub(r'(?<!:)//+', '/', cleaned)

    return cleaned


def generate_twitter_embed_name(input_string: str) -> str:
    """Format Twitter username for embed"""
    return_string = input_string.replace(' / ', ' (') + str(')')
    return return_string


def generate_twitter_profile_picture_link(input_string: str) -> str:
    """Extract Twitter profile picture URL"""
    return_string = urljoin('https://', unquote(urlparse(input_string).path)).replace('/pic/', '')
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


def generate_twitter_user_from_rss(feed_data: FeedParserDict, key: str, webhook_url: list[str],
                                   discord_mention: bool,
                                   discord_mention_role_id: list[str]) -> TwitterUser:
    """Create TwitterUser object from RSS feed data"""
    return TwitterUser(
        name=generate_twitter_embed_name(feed_data.feed.title),
        link=replace_url_to_twitter(feed_data.feed.link, __twitter_url),
        icon=generate_twitter_profile_picture_link(feed_data.feed.image.href),
        key=key,
        webhookUrl=webhook_url,
        discordMention=discord_mention,
        discordMentionRoleId=discord_mention_role_id
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
    """Extract all assets URLs from RSS description."""
    extracted_media_list = []
    video_detected = False
    soup = BeautifulSoup(description, 'lxml')

    video_elements = soup.find_all('video')
    if video_elements:
        video_detected = True
        for video in video_elements:
            source = video.find('source')
            if source and source.get('src'):
                video_url = source.get('src')

                # Use new function to properly extract video URL
                twitter_video_url = extract_video_url_from_nitter(video_url)
                extracted_media_list.append(TwitterMedia(url=twitter_video_url, type='video'))

            poster = video.get('poster', '')
            if poster:
                twitter_img_url = generate_twitter_picture_link(poster, twitter_card_template)
                extracted_media_list.append(TwitterMedia(url=twitter_img_url, type='video_thumbnail'))

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
    """
    soup = BeautifulSoup(html_content, 'lxml')

    # Remove images and videos first
    for tag in soup.find_all(['img', 'video', 'source']):
        tag.decompose()

    # Process all links
    for link in soup.find_all('a'):
        href = link.get('href', '')
        link_text = replace_nitter_url_to_twitter_url(link.get_text())

        if not href:
            continue

        # Skip hashtag links - we'll handle them separately
        if link_text.startswith('#'):
            continue

        # Replace nitter URL to twitter URL
        href = replace_nitter_url_to_twitter_url(href)

        # Determine display text
        # Only replace with full URL if the link text contains ellipsis (...)
        if '…' in link_text or '...' in link_text:
            # Show full URL without protocol
            display_text = href.replace('https://', '').replace('http://', '')
        else:
            # Keep original text (for hashtags, mentions, etc.)
            display_text = link_text

        # Replace the link with Discord Markdown format
        link.replace_with(f'[{display_text}]({href})')

    # Convert <br> to newlines
    # for br in soup.find_all('br'):
    #     br.replace_with('\n')

    # Remove all remaining hashtag links (we'll recreate them)
    for link in soup.find_all('a'):
        link_text = link.get_text()
        if link_text.startswith('#'):
            link.replace_with(link_text)  # Replace with just the text

    # Get text directly - this preserves all newlines
    text = soup.get_text()

    # Convert standalone hashtags (not already in links) to clickable links
    import re
    from urllib.parse import quote

    def replace_hashtag(match):
        hashtag_with_hash = match.group(0)  # e.g., #Trickcal or #トリッカル
        hashtag_without_hash = hashtag_with_hash[1:]  # Remove the #
        # URL encode the hashtag text
        encoded = quote(hashtag_without_hash)
        return f'[{hashtag_with_hash}](https://x.com/hashtag/{encoded})'

    # Match hashtags that are NOT already inside Markdown links
    # Negative lookbehind: (?<!\[) - not preceded by [
    # Negative lookahead: (?!\]\() - not followed by ](
    # Match: # followed by word characters (including Unicode)
    hashtag_pattern = r'(?<!\[)#\w+(?!\]\()'
    text = re.sub(hashtag_pattern, replace_hashtag, text)

    # Strip only leading/trailing whitespace, preserve internal newlines
    return text.strip()


def generate_embed_color() -> int:
    # Generate a random integer between 0 and 0xFFFFFF
    random_color = random.randint(0, 0xFFFFFF)
    return random_color


def generate_embed_data(title: str, payload: WebhookMediaPayload,
                        timestamp: float, author_name: str = None, author_url: str = None,
                        author_icon_url: str = None) -> DiscordEmbed:
    """Create embed object for webhook."""
    embed = DiscordEmbed()

    if author_name and author_url:
        embed.set_author(name=author_name,
                         url=author_url,
                         icon_url=author_icon_url)

    embed.set_color(generate_embed_color())

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
    payload.cleaned_description = clean_tweet_description(title)

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


async def post_to_single_webhook(
        session: ClientSession,
        webhook_url: str,
        content: str,
        tweet_link: str,
        payload: WebhookMediaPayload,
        timestamp: float,
        twitter_user: TwitterUser
) -> bool:
    """
    OPTIMIZED: Post to a single webhook (videos + embed).
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
                    embed = generate_embed_data(
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
                embed = generate_embed_data(
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
                log.error(f"❌ Failed to post embed. HTTP {response.status_code}")
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
    if len(twitter_user.webhookUrl) > 1:
        log.info(f"🚀 Posting to {len(twitter_user.webhookUrl)} webhooks in parallel...")
        webhook_tasks = [
            post_to_single_webhook(
                session, webhook_url, content, tweet_link,
                payload, timestamp, twitter_user
            )
            for webhook_url in twitter_user.webhookUrl
        ]

        results = await asyncio.gather(*webhook_tasks, return_exceptions=True)

        # Check if all succeeded
        success_count = sum(1 for r in results if r is True)
        log.info(f"✅ Posted to {success_count}/{len(twitter_user.webhookUrl)} webhooks successfully")

        return success_count > 0  # Return True if at least one webhook succeeded
    else:
        # Single webhook, post directly
        return await post_to_single_webhook(
            session, twitter_user.webhookUrl[0], content, tweet_link,
            payload, timestamp, twitter_user
        )


async def main():
    """Main async function with performance timing"""
    try:
        script_start = time.time()

        # Read last run timestamp
        cutoff_time = read_last_run()
        log.info(f"Filtering entries published AFTER: {cutoff_time} (UTC)")

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
                    # needed fetch_video_from_fxtwitter, if I put browser agent, it just redirects
                    'User-Agent': 'curl/8.16.0',
                    'Accept-Encoding': 'gzip, deflate',
                    'Connection': 'keep-alive',
                }
        ) as session:

            # Fetch all RSS feeds concurrently
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

                # Add to twitter_user_list
                if hasattr(feedParse.feed, 'image'):
                    twitter_user_list.append(
                        generate_twitter_user_from_rss(
                            feed_data=feedParse,
                            key=item.twitterHandleName,
                            webhook_url=item.webhookUrl,
                            discord_mention=item.discordMention,
                            discord_mention_role_id=item.discordMentionRoleId
                        )
                    )
                else:
                    twitter_user_list.append(
                        TwitterUser(
                            name=item.twitterHandleName,
                            link=f"{__twitter_url}/{item.twitterHandleName}",
                            icon="",
                            key=item.twitterHandleName,
                            webhookUrl=item.webhookUrl,
                            discordMention=item.discordMention,
                            discordMentionRoleId=item.discordMentionRoleId
                        )
                    )

                # Process entries
                for data in feedParse.entries:
                    pub_date = dateutil.parser.parse(timestr=data.published)
                    pub_date = normalize_datetime_to_utc_naive(pub_date)

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
                        key=item.twitterHandleName,
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

            # Post to Discord
            posted_count = 0
            latest_successful_pubdate = None

            post_start = time.time()

            for data in entry_data:
                twitter_user = next((item for item in twitter_user_list if item.key == data.key), None)
                if not twitter_user:
                    log.warning(f"No user found for key {data.key}")
                    continue

                try:
                    log.info(f"Posting tweet from {data.pubdate}: {data.link}")
                    success = await send_to_discord_with_media(
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
                        latest_successful_pubdate = data.pubdate
                        log.info(f"✅ Successfully posted tweet from {data.pubdate}")

                except Exception as post_error:
                    log.error(f"Error posting to Discord: {post_error}")
                    continue

            log.info(f"⏱️ Discord posting took: {time.time() - post_start:.2f}s")
            log.info(f"Successfully posted {posted_count}/{len(entry_data)} tweets")

            # Update last_run.txt
            if latest_successful_pubdate:
                write_last_run(latest_successful_pubdate)
                log.info(f"✅ Updated checkpoint to latest posted tweet: {latest_successful_pubdate}")
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