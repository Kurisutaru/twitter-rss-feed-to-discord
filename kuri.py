import asyncio
import atexit
import calendar as cal
import hashlib
import os
import random
import re
import signal
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from operator import attrgetter
from os.path import isfile
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, unquote, urlparse

import dateutil.parser
import feedparser
from aiohttp import ClientSession, TCPConnector, ClientTimeout
from bs4 import BeautifulSoup
from discord_webhook import DiscordEmbed, DiscordWebhook
from feedparser import FeedParserDict
from loguru import logger as log
from markdownify import markdownify as md
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
                with open(self.lock_file, 'r') as f:
                    old_pid = int(f.read().strip())

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
            with open(self.lock_file, 'w') as f:
                f.write(str(os.getpid()))
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

# Variable
__twitter_url: str = 'https://x.com'
__fxtwitter_url: str = 'https://fxtwitter.com'
__twitter_image_card_link_template: str = 'https://pbs.twimg.com/{}{}'
__post_video_identifier: str = 'ext_tw_video_thumb'
__rss_template: str = '{}/{}/rss'

# JSONFile
__json_file: str = 'kuri.config.json'
__last_run_file: str = os.path.join(script_dir, 'last_run.txt')


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
    useFxTwitter: bool


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
    """Represents media in a tweet"""
    url: str
    type: str  # 'image', 'video', or 'video_thumbnail'


# Check if config file exist, if not abort
if not isfile(__json_file):
    log.error("Config file not found, abort current running script")
    exit(1)

with open(__json_file, 'r') as f:
    mainConfig = TwitterDiscordConfig.from_json(f.read())


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


def generate_rss_url(twitter_handle_name: str, nitter_url: str) -> str:
    """Generate RSS feed URL for a Twitter user"""
    return __rss_template.format(nitter_url, twitter_handle_name)


def replace_url_to_twitter(input_string: str, twitter_url: str) -> str:
    """Convert nitter URL to Twitter URL"""
    return_string = urljoin(twitter_url, urlparse(input_string).path)
    return return_string


def replace_nitter_url_to_twitter_url(input_string: str, twitter_url: str, nitter_url: list[str]) -> str:
    """Replace nitter domain with Twitter domain"""
    return_string = input_string.replace('http://', 'https://').replace('#m', '')
    for serv in nitter_url:
        return_string = return_string.replace(urlparse(serv).netloc, urlparse(twitter_url).netloc)
    return return_string


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


def generate_timestamp(input_time) -> int:
    """Convert time struct to timestamp"""
    return cal.timegm(input_time)


def generate_date_from_timestamp(input_time) -> datetime:
    """Convert timestamp to datetime"""
    return datetime.fromtimestamp(input_time)


def normalize_datetime_to_utc_naive(dt: datetime) -> datetime:
    """Normalize any datetime to UTC timezone-naive format for consistent comparison."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        return dt


def generate_twitter_user_from_rss(feed_data: FeedParserDict, key: str, webhook_url: list[str], discord_mention: bool,
                                   discord_mention_role_id: list[str]) -> TwitterUser:
    """Create TwitterUser object from RSS feed data"""
    return TwitterUser(
        name=generate_twitter_embed_name(feed_data.feed.image.title),
        link=replace_url_to_twitter(feed_data.feed.image.link, __twitter_url),
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
    """Generate unique filename for media"""
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

    video_elements = soup.find_all('video')
    if video_elements:
        video_detected = True
        for video in video_elements:
            source = video.find('source')
            if source and source.get('src'):
                video_url = source.get('src')
                twitter_video_url = video_url.replace('http://', 'https://')
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
    2. Converting links to Discord markdown format
    3. Replacing nitter URLs with x.com
    4. Showing full URLs without protocol ONLY for truncated links (containing ...)
    5. Keeping hashtag and mention links with their original text
    """
    soup = BeautifulSoup(html_content, 'lxml')

    # Remove images and videos first
    for tag in soup.find_all(['img', 'video', 'source']):
        tag.decompose()

    # Replace nitter URLs with x.com
    list_server = mainConfig.nitterServer
    list_server.append('http://nitter.net')

    # Process all links
    for link in soup.find_all('a'):
        href = link.get('href', '')
        link_text = replace_nitter_url_to_twitter_url(link.get_text(), __twitter_url, list_server)

        if not href:
            continue

        # Replace nitter URL to twitter URL
        href = replace_nitter_url_to_twitter_url(href, __twitter_url, list_server)

        # Determine display text
        # Only replace with full URL if the link text contains ellipsis (...)
        if '…' in link_text or '...' in link_text:
            # Show full URL without protocol
            display_text = href.replace('https://', '').replace('http://', '')
        else:
            # Keep original text (for hashtags, mentions, etc)
            display_text = link_text

        # Replace the link with Discord markdown format
        link.replace_with(f'[{display_text}]({href})')

    # Convert <br> to newlines
    for br in soup.find_all('br'):
        br.replace_with('\n')

    # Convert to markdown automatically
    markdown = md(str(soup), heading_style="ATX", escape_underscores=False)

    return markdown.strip()


def generate_embed_color() -> int:
    # Generate a random integer between 0 and 0xFFFFFF
    random_color = random.randint(0, 0xFFFFFF)
    return random_color


def generate_embed_data(title: str, media_count: int, has_video: bool, video_uploaded: bool,
                        timestamp: float, author_name: str = None, author_url: str = None,
                        author_icon_url: str = None) -> DiscordEmbed:
    """Create embed object for webhook."""
    embed = DiscordEmbed()

    if author_name and author_url:
        embed.set_author(name=author_name,
                         url=author_url,
                         icon_url=author_icon_url)

    embed.set_color(generate_embed_color())

    if author_name:
        embed.set_footer(text=mainConfig.config.embedFooterText, icon_url=mainConfig.config.embedFooterImageUrl)

    if timestamp:
        embed.set_timestamp(timestamp)

    if title:
        post_data = clean_tweet_text(title)

        if has_video and video_uploaded:
            post_data = f'{post_data}\n\n🎥 *[tweet has video]*'

        if media_count > 1:
            post_data = f'{post_data}\n\n🔎 *Contains {media_count} media files*'

        embed.set_description(post_data)

    return embed


async def fetch_rss_feed(session: ClientSession, twitter_handle: str, nitter_url: str) -> Optional[FeedParserDict]:
    """Fetch RSS feed asynchronously"""
    rss_url = generate_rss_url(twitter_handle, nitter_url)
    try:
        async with session.get(rss_url, timeout=ClientTimeout(total=30)) as response:
            if response.status == 200:
                content = await response.text()
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


async def download_media(session: ClientSession, url: str, max_size: int = 25 * 1024 * 1024) -> Optional[bytes]:
    """Download media file asynchronously with size limit"""
    try:
        async with session.get(url, timeout=ClientTimeout(total=30)) as response:
            if response.status == 200:
                content = await response.read()
                if len(content) > max_size:
                    log.warning(f"Media too large ({len(content) / 1024 / 1024:.2f}MB): {url}")
                    return None
                return content
            else:
                log.warning(f"Failed to download media: HTTP {response.status}")
                return None
    except asyncio.TimeoutError:
        log.error(f"Timeout downloading: {url}")
        return None
    except Exception as e:
        log.error(f"Error downloading {url}: {e}")
        return None


async def generate_media_webhook(session: ClientSession, title: str, tweet_media_list: List[TwitterMedia],
                                 tweet_has_video: bool, twitter_user: TwitterUser) -> tuple:
    """Generate media webhook data with async downloads - ALL downloads happen concurrently"""
    max_file_size = 25 * 1024 * 1024
    max_video_size = 10 * 1024 * 1024

    author_icon_data = None
    author_icon_filename = None
    embed_media = []
    media_data = []
    updated_title = clean_tweet_description(title)

    # Separate media by type
    videos = [m for m in tweet_media_list if m.type == 'video']
    video_thumbnails = [m for m in tweet_media_list if m.type == 'video_thumbnail']
    images = [m for m in tweet_media_list if m.type == 'image']

    # ===== DOWNLOAD EVERYTHING CONCURRENTLY =====
    download_tasks = []
    task_metadata = []  # Track what each task is for

    # Task 0: Author icon (if exists)
    if twitter_user.icon:
        log.info(f"Queueing author icon download: {twitter_user.icon}")
        download_tasks.append(download_media(session, twitter_user.icon, max_size=5 * 1024 * 1024))
        task_metadata.append(('author_icon', twitter_user.icon, None))

    # Tasks 1+: Videos
    for media in videos:
        log.info(f"Queueing video download: {media.url}")
        download_tasks.append(download_media(session, media.url, max_size=max_file_size))
        task_metadata.append(('video', media.url, media))

    # Tasks N+: Images
    for media in images:
        log.info(f"Queueing image download: {media.url}")
        download_tasks.append(download_media(session, media.url, max_size=max_file_size))
        task_metadata.append(('image', media.url, media))

    # Tasks M+: Video thumbnails (always queue them, we'll decide later if we need them)
    for media in video_thumbnails:
        log.info(f"Queueing video thumbnail download: {media.url}")
        download_tasks.append(download_media(session, media.url, max_size=max_file_size))
        task_metadata.append(('video_thumbnail', media.url, media))

    # Download ALL media concurrently
    log.info(f"🚀 Starting {len(download_tasks)} concurrent downloads...")
    download_results = await asyncio.gather(*download_tasks, return_exceptions=True)
    log.info(f"✅ All downloads completed")

    # Process results
    video_uploaded = False
    video_too_large = False
    downloaded_thumbnails = []  # Store thumbnails separately

    for (media_type, url, media_obj), result in zip(task_metadata, download_results):
        if isinstance(result, Exception):
            log.error(f"Failed to download {media_type} from {url}: {result}")
            if media_type == 'video':
                video_too_large = True
            continue

        if result is None:
            log.warning(f"Download returned None for {media_type}: {url}")
            if media_type == 'video':
                video_too_large = True
            continue

        # Process based on type
        if media_type == 'author_icon':
            author_icon_filename = f"profile_{hashlib.md5(url.encode()).hexdigest()[:8]}.jpg"
            author_icon_data = result
            log.info(f"✅ Author icon ready: {author_icon_filename}")

        elif media_type == 'video':
            filename = generate_media_filename(url, len(media_data))
            media_data.append((filename, result, 'video'))
            video_uploaded = True
            log.info(f"✅ Video ready: {filename} ({len(result) / 1024 / 1024:.2f}MB)")

        elif media_type == 'image':
            filename = generate_media_filename(url, len(media_data))
            media_data.append((filename, result, 'image'))
            log.info(f"✅ Image ready: {filename}")

        elif media_type == 'video_thumbnail':
            # Store thumbnails for later decision
            downloaded_thumbnails.append((url, result))
            log.info(f"✅ Video thumbnail ready (cached)")

    # Decide if we need to use video thumbnails
    need_thumbnails = (video_too_large or (tweet_has_video and not video_uploaded)) and not video_uploaded

    if need_thumbnails:
        if video_too_large:
            log.info("Video too large, using downloaded thumbnails as fallback")
        else:
            log.info("Video source not available, using downloaded thumbnails")

        for url, content in downloaded_thumbnails:
            filename = generate_media_filename(url, len(media_data))
            media_data.append((filename, content, 'image'))
            log.info(f"✅ Using thumbnail: {filename}")

    # Update title based on video status
    if tweet_has_video and not video_uploaded:
        if video_too_large:
            updated_title = f"{title}\n\n🎥 *[Video too large for Discord (>25MB), click link to watch]*"
        else:
            updated_title = f"{title}\n\n🎥 *[This tweet has video - click link to watch]*"

    # Filter embed media
    for filename, file_data, file_type in media_data:
        if file_type == 'video':
            continue
        elif file_type == 'image':
            is_video_thumb = 'tweet_video_thumb' in filename or 'video_thumb' in filename
            if is_video_thumb and video_uploaded:
                log.info(f"Skipping video thumbnail {filename} (video was uploaded)")
                continue
            else:
                embed_media.append((filename, file_data, file_type))
        else:
            embed_media.append((filename, file_data, file_type))

    log.info(f"Embed will display {len(embed_media)} images (filtered from {len(media_data)} total media)")

    return author_icon_data, author_icon_filename, updated_title, video_uploaded, embed_media, media_data


async def send_to_discord_with_media(session: ClientSession, tweet_link: str, embed_title: str,
                                     tweet_media_list: List[TwitterMedia], tweet_has_video: bool,
                                     timestamp: float, twitter_user: TwitterUser) -> bool:
    """Send tweet to Discord with media uploaded as attachments (async)."""
    content = tweet_link
    if mainConfig.config.useFxTwitter:
        content = content.replace(__twitter_url, __fxtwitter_url)

    if twitter_user.discordMention and twitter_user.discordMentionRoleId:
        mentions = ' '.join([f'<@&{role_id}>' for role_id in twitter_user.discordMentionRoleId])
        content = f'{content}\n{mentions}'

    all_success = True
    for webhook_url in twitter_user.webhookUrl:
        try:
            log.info(f"Sending to webhook: {webhook_url[:50]}...")

            webhook = DiscordWebhook(url=webhook_url, content=content, rate_limit_retry=True)

            if mainConfig.config.generateEmbed:
                author_icon_data, author_icon_filename, updated_title, video_uploaded, embed_media, media_data = (
                    await generate_media_webhook(session, embed_title, tweet_media_list, tweet_has_video, twitter_user))

                if author_icon_data:
                    webhook.add_file(file=author_icon_data, filename=author_icon_filename)
                    author_icon_url = f"attachment://{author_icon_filename}"
                else:
                    author_icon_url = twitter_user.icon

                for filename, file_data, file_type in media_data:
                    webhook.add_file(file=file_data, filename=filename)

                if embed_media:
                    for idx, (filename, _, file_type) in enumerate(embed_media):
                        is_first_index = idx == 0
                        embed = generate_embed_data(
                            title=updated_title if is_first_index else "",
                            media_count=len(embed_media),
                            has_video=tweet_has_video,
                            video_uploaded=video_uploaded,
                            timestamp=timestamp if is_first_index else None,
                            author_name=twitter_user.name if is_first_index else None,
                            author_url=twitter_user.link if is_first_index else None,
                            author_icon_url=author_icon_url if is_first_index else None
                        )

                        embed.set_image(url=f"attachment://{filename}")
                        embed.set_url(tweet_link)
                        webhook.add_embed(embed)
                else:
                    embed = generate_embed_data(
                        title=updated_title,
                        media_count=len(media_data),
                        has_video=tweet_has_video,
                        video_uploaded=video_uploaded,
                        timestamp=timestamp,
                        author_name=twitter_user.name,
                        author_url=twitter_user.link,
                        author_icon_url=author_icon_url
                    )
                    webhook.add_embed(embed)

            response = webhook.execute()
            if response.ok:
                log.info(f"✅ Posted to webhook successfully")
            else:
                log.error(f"❌ Failed to post to webhook. HTTP {response.status_code}")
                all_success = False

        except Exception as webhook_error:
            log.error(f"❌ Error sending to webhook: {webhook_error}")
            all_success = False
            continue

    return all_success


async def main():
    """Main async function"""
    try:
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
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept-Encoding': 'gzip, deflate',
                    'Connection': 'keep-alive',
                }
        ) as session:

            # Fetch all RSS feeds concurrently
            twitter_user_list: List[TwitterUser] = []
            entry_data: List[EntryData] = []

            nitter_server_distribution_list = random.choices(mainConfig.nitterServer, k=len(mainConfig.twitterWatch))

            # Create tasks for fetching all RSS feeds
            feed_tasks = []
            for item, nitter in zip(mainConfig.twitterWatch, nitter_server_distribution_list):
                feed_tasks.append(fetch_rss_feed(session, item.twitterHandleName, nitter))

            # Fetch all feeds concurrently
            log.info(f"Fetching {len(feed_tasks)} RSS feeds concurrently...")
            feed_results = await asyncio.gather(*feed_tasks, return_exceptions=True)

            # Process feed results
            for item, feedParse in zip(mainConfig.twitterWatch, feed_results):
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
                        if mainConfig.config.includeReTweet:
                            entry_data.append(temp_data)
                    else:
                        entry_data.append(temp_data)

            # Sort by publication date (oldest first)
            entry_data = sorted(entry_data, key=attrgetter('pubdate'))
            log.info(f"Found {len(entry_data)} new entries to post")

            # Post to Discord
            posted_count = 0
            latest_successful_pubdate = None

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
                        embed_title=data.title,
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

            log.info(f"Successfully posted {posted_count}/{len(entry_data)} tweets")

            # Update last_run.txt
            if latest_successful_pubdate:
                write_last_run(latest_successful_pubdate)
                log.info(f"✅ Updated checkpoint to latest posted tweet: {latest_successful_pubdate}")
            elif entry_data and posted_count == 0:
                log.warning("⚠️ Had entries but failed to post any - NOT updating checkpoint")
            else:
                log.info("No new entries found, checkpoint unchanged")

            log.info("Script completed successfully")

    except Exception as main_error:
        log.exception(f"Caught an exception: {main_error}")
        exit(1)


if __name__ == "__main__":
    # Run the async main function
    asyncio.run(main())
