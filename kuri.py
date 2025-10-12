import calendar as cal
import json
import os
import random
import re
import sys
import time
import zipfile
import hashlib
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone
from operator import attrgetter
from os.path import isfile
from pathlib import Path
from urllib.parse import urljoin, unquote, urlparse
from typing import List, Optional, Union
from loguru import logger as log

import dateutil.parser
import feedparser
from bs4 import BeautifulSoup
from discord_webhook import DiscordEmbed, DiscordWebhook

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
    zip_filename = os.path.join(directory, f"app.{today}.log.zip")

    with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(file_path, f"app.{today}.log")

    os.remove(file_path)


log.add(sys.stdout, format=log_format, colorize=True)
log.add(log_file, rotation="sunday", compression=kuri_zip_compression,
        encoding="utf-8", level="INFO", format=log_format)

# Variable
__twitter_url: str = 'https://x.com'
__twitter_image_card_link_template: str = 'https://pbs.twimg.com/{}{}'
__post_video_identifier: str = 'ext_tw_video_thumb'
__rss_template: str = '{}/{}/rss'

# JSONFile
__json_file: str = 'kuri.config.json'
__last_run_file: str = os.path.join(script_dir, 'last_run.txt')


# Dataclasses (replacing namedtuples)
@dataclass
class TwitterDbData:
    """Configuration for a Twitter account to watch"""
    twitterHandleName: str
    webhookUrl: Union[str, List[str]]  # Can be single URL or list of URLs
    discordMention: bool = False
    discordMentionRoleId: Union[str, List[str]] = field(default_factory=list)  # Can be string or list

    def __post_init__(self):
        """Normalize webhookUrl and discordMentionRoleId to lists"""
        # Convert single webhook URL to list
        if isinstance(self.webhookUrl, str):
            self.webhookUrl = [self.webhookUrl]

        # Convert single role ID to list
        if isinstance(self.discordMentionRoleId, str):
            if self.discordMentionRoleId:  # Only if not empty string
                self.discordMentionRoleId = [self.discordMentionRoleId]
            else:
                self.discordMentionRoleId = []
        elif self.discordMentionRoleId is None:
            self.discordMentionRoleId = []


@dataclass
class TwitterUser:
    """Twitter user information for Discord embeds"""
    name: str
    link: str
    icon: str
    key: str
    webhookUrl: list  # Changed to list to support multiple webhooks
    discordMention: bool
    discordMentionRoleId: list  # Changed to list to support multiple role mentions


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


twitter_pull_rss_data_list: List[TwitterDbData] = []
twitter_user_list: List[TwitterUser] = []


def read_last_run(filename: str = __last_run_file) -> datetime:
    """Read the last run timestamp from file."""
    try:
        if os.path.exists(filename):
            with open(filename, 'r') as f:
                timestamp_str = f.read().strip()
                last_run = datetime.fromisoformat(timestamp_str)
                # Ensure timezone-naive for comparison
                if last_run.tzinfo is not None:
                    last_run = last_run.replace(tzinfo=None)
                log.info(f"Last run timestamp: {last_run}")
                return last_run
        else:
            log.info("No last_run.txt found, fetching all entries")
            return datetime(2000, 1, 1)
    except Exception as read_error:
        log.error(f"Error reading last run: {read_error}, using default")
        return datetime(2000, 1, 1)


def write_last_run(timestamp: Optional[datetime] = None, filename: str = __last_run_file):
    """Write timestamp to file."""
    try:
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        # Ensure timezone-naive before saving
        if isinstance(timestamp, datetime) and timestamp.tzinfo is not None:
            timestamp = timestamp.replace(tzinfo=None)
        with open(filename, 'w') as f:
            f.write(timestamp.isoformat())
        log.info(f"Updated last run time: {timestamp}")
    except Exception as write_error:
        log.error(f"Error writing last run: {write_error}")


# Check if config file exist, if not abort
if not isfile(__json_file):
    log.error("Config file not found, abort current running script")
    exit(1)

with open(__json_file, 'r', encoding='UTF-8') as jsonFile:
    jsonConfig = json.load(jsonFile)

__nitter_url = jsonConfig['nitterServer']
__footer_embed_text = jsonConfig['config']['footerTextForEmbed']
__footer_embed_image_url = jsonConfig['config']['footerImageUrlForEmbed']
__twitter_embed_color = jsonConfig['config']['footerColorForEmbed']
__include_re_tweet = jsonConfig['config']['includeReTweet']

for item in jsonConfig['twitterWatch']:
    if item.get('twitterHandleName') and item.get('webhookUrl'):
        twitter_pull_rss_data_list.append(
            TwitterDbData(
                twitterHandleName=item['twitterHandleName'],
                webhookUrl=item['webhookUrl'],  # Can be string or list
                discordMention=item.get('discordMention', False),
                discordMentionRoleId=item.get('discordMentionRoleId', [])  # Can be string, list, or empty
            )
        )


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


def generate_twitter_user_from_rss(feed_data: feedparser, key: str, webhook_url: list[str], discord_mention: bool,
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

    # Get extension from path
    ext = Path(path).suffix
    if ext:
        return ext.lstrip('.')

    # Default extensions based on common patterns
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

    # Default to jpg for images
    return 'jpg'


def generate_media_filename(url: str, index: int = 0) -> str:
    """Generate unique filename for media"""
    # Use hash of URL to create unique but consistent filename
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    ext = detect_extension(url)

    if index > 0:
        return f"twitter_media_{url_hash}_{index}.{ext}"
    return f"twitter_media_{url_hash}.{ext}"


@dataclass
class TwitterMedia:
    """Represents media in a tweet"""
    url: str
    type: str  # 'image', 'video', or 'video_thumbnail'


def extract_media_from_description(description: str, twitter_card_template: str) -> tuple[List[TwitterMedia], bool]:
    """
    Extract all media URLs from RSS description.
    Returns (list of TwitterMedia objects, has_video boolean)
    """
    extracted_media_list = []
    global video_detected
    video_detected = False
    soup = BeautifulSoup(description, 'html.parser')

    # Check for video elements first (embedded videos with source)
    video_elements = soup.find_all('video')
    if video_elements:
        video_detected = True
        for video in video_elements:
            # Get video source URL
            source = video.find('source')
            if source and source.get('src'):
                video_url = source.get('src')
                # Convert nitter video URL to Twitter CDN URL
                twitter_video_url = video_url.replace('http://', 'https://')  # noqa: S104
                extracted_media_list.append(TwitterMedia(url=twitter_video_url, type='video'))

            # Also get poster as fallback
            poster = video.get('poster', '')
            if poster:
                twitter_img_url = generate_twitter_picture_link(poster, twitter_card_template)
                extracted_media_list.append(TwitterMedia(url=twitter_img_url, type='video_thumbnail'))

    # Find all images
    for img in soup.find_all('img'):
        img_src = img.get('src', '')
        if not img_src:
            continue

        # Convert nitter image URL to Twitter CDN URL
        twitter_img_url = generate_twitter_picture_link(img_src, twitter_card_template)

        # Check if it's a video thumbnail (for videos without <video> tag)
        # These are videos that nitter RSS doesn't provide the video source for
        if __post_video_identifier in img_src or 'amplify_video_thumb' in img_src:
            video_detected = True
            # Mark as video_thumbnail, not regular image
            # This will be displayed when video isn't available
            extracted_media_list.append(TwitterMedia(url=twitter_img_url, type='video_thumbnail'))
        else:
            extracted_media_list.append(TwitterMedia(url=twitter_img_url, type='image'))

    return extracted_media_list, video_detected


def clean_tweet_text(title: str) -> str:
    """
    Clean tweet title by removing RT prefix
    """
    # Remove "R to @username: " prefix (for retweets in nitter)
    text = re.sub(r'^R to @\w+:\s*', '', title)
    return text


def generate_embed_color() -> str:
    """Get random embed color from config"""
    return random.choice(__twitter_embed_color)


def generate_embed_data(title: str, media_count: int, has_video: bool, video_uploaded: bool,
                        timestamp: float, author_name: str, author_url: str, author_icon_url: str) -> DiscordEmbed:
    """
    Create embed object for webhook.
    Retains the original embed style.
    Author icon can be either a URL or attachment:// reference.
    """
    embed = DiscordEmbed()

    # Set author with icon (can be URL or attachment://)
    embed.set_author(name=author_name,
                     url=author_url,
                     icon_url=author_icon_url)

    # set Color
    embed.set_color(generate_embed_color())

    # set footer
    embed.set_footer(text=__footer_embed_text, icon_url=__footer_embed_image_url)

    # set timestamp
    embed.set_timestamp(timestamp)

    # Clean the tweet text
    post_data = clean_tweet_text(title)

    # Only add video indicator if video was successfully uploaded
    # (Other video messages are handled in send_to_discord_with_media)
    if has_video and video_uploaded:
        post_data = f'{post_data}\n\n🎥 *[tweet has video]*'

    # Add media count if multiple
    if media_count > 1:
        post_data = f'{post_data}\n\n📎 *Contains {media_count} media files*'

    # set Description
    embed.set_description(post_data)

    return embed


def send_to_discord_with_media(tweet_link: str, title: str, tweet_media_list: List[TwitterMedia], tweet_has_video: bool,
                               timestamp: float, twitter_user: TwitterUser) -> bool:
    """
    Send tweet to Discord with media uploaded as attachments.
    Supports multiple webhooks and multiple role mentions.
    Priority: Videos first, then images.
    Also uploads author profile picture as attachment.
    Returns True if all webhooks successful, False otherwise.
    """
    # Discord file size limits (in bytes)
    max_file_size = 25 * 1024 * 1024  # 25MB for regular servers
    max_video_size = 10 * 1024 * 1024  # 10MB soft limit for videos (faster upload)

    # Build base content with role mentions
    content = tweet_link
    if twitter_user.discordMention and twitter_user.discordMentionRoleId:
        # Loop through role IDs and mention each
        mentions = ' '.join([f'<@&{role_id}>' for role_id in twitter_user.discordMentionRoleId])
        content = f'{content}\n{mentions}'

    # Prepare media data (download once, use for all webhooks)
    media_data = []
    author_icon_data = None
    author_icon_filename = None

    # Download author profile picture (if available)
    if twitter_user.icon:
        try:
            log.info(f"Downloading author icon: {twitter_user.icon}")
            with requests.get(twitter_user.icon, stream=True, timeout=5) as r:
                r.raise_for_status()
                author_icon_filename = f"profile_{hashlib.md5(twitter_user.icon.encode()).hexdigest()[:8]}.jpg"
                author_icon_data = r.content
                log.info(f"✅ Author icon downloaded: {author_icon_filename}")
        except Exception as icon_error:
            log.warning(f"Failed to download author icon: {icon_error}, using direct URL")

    # Separate media by type and prioritize
    videos = [m for m in tweet_media_list if m.type == 'video']
    video_thumbnails = [m for m in tweet_media_list if m.type == 'video_thumbnail']
    images = [m for m in tweet_media_list if m.type == 'image']

    video_uploaded = False
    video_too_large = False

    # Priority 1: Try to upload videos first
    for i, media in enumerate(videos):
        try:
            filename = generate_media_filename(media.url, len(media_data))

            # Check video size first
            log.info(f"Checking video size: {media.url}")
            try:
                head_response = requests.head(media.url, timeout=5, allow_redirects=True)
                content_length = int(head_response.headers.get('content-length', 0))

                if content_length == 0:
                    # HEAD request didn't return size, try GET with stream
                    log.warning("HEAD request returned 0 size, trying GET...")
                    with requests.get(media.url, stream=True, timeout=5) as r:
                        content_length = int(r.headers.get('content-length', 0))
                        if content_length == 0:
                            # Still no size, download and check actual size
                            log.warning("Content-Length header missing, downloading to check size...")
                            video_content = r.content
                            content_length = len(video_content)

                            # Check if too large
                            if content_length > max_file_size:
                                log.warning(
                                    f"Video too large ({content_length / 1024 / 1024:.2f}MB) after download. Max: 25MB")
                                video_too_large = True
                                continue

                            # Size OK, save downloaded data
                            log.info(f"Video size: {content_length / 1024 / 1024:.2f}MB (checked after download)")
                            media_data.append((filename, video_content, 'video'))
                            video_uploaded = True
                            log.info(f"✅ Video ready for upload: {filename}")
                            continue  # Skip the normal download below

                if content_length > max_file_size:
                    log.warning(
                        f"Video too large ({content_length / 1024 / 1024:.2f}MB), will use thumbnail. Max: 25MB")
                    video_too_large = True
                    continue
                elif content_length > max_video_size:
                    log.warning(f"Video large ({content_length / 1024 / 1024:.2f}MB), but within limit. Downloading...")
                else:
                    log.info(f"Video size: {content_length / 1024 / 1024:.2f}MB")

            except Exception as size_check_error:
                log.error(f"Error checking video size: {size_check_error}, attempting download anyway...")

            # Download the video
            log.info(f"Downloading video: {media.url}")
            with requests.get(media.url, stream=True, timeout=30) as r:
                r.raise_for_status()
                video_content = r.content

                # Final size check after download
                actual_size = len(video_content)
                if actual_size > max_file_size:
                    log.warning(f"Video too large ({actual_size / 1024 / 1024:.2f}MB) after download. Max: 25MB")
                    video_too_large = True
                    continue

                # Save media data
                log.info(f"Final video size: {actual_size / 1024 / 1024:.2f}MB")
                media_data.append((filename, video_content, 'video'))
                video_uploaded = True
                log.info(f"✅ Video ready for upload: {filename}")

        except requests.exceptions.Timeout:
            log.error(f"Timeout downloading video {media.url}")
            video_too_large = True
            continue
        except Exception as video_error:
            log.error(f"Failed to download video {media.url}: {video_error}")
            video_too_large = True
            continue

    # Priority 2: Upload images (skip video thumbnails if video was uploaded)
    for i, media in enumerate(images):
        try:
            filename = generate_media_filename(media.url, len(media_data))

            log.info(f"Downloading image: {media.url}")
            with requests.get(media.url, stream=True, timeout=10) as r:
                r.raise_for_status()
                media_data.append((filename, r.content, 'image'))

        except requests.exceptions.Timeout:
            log.error(f"Timeout downloading image {media.url}")
            continue
        except Exception as image_error:
            log.error(f"Failed to download image {media.url}: {image_error}")
            continue

    # Priority 3: Upload video thumbnails ONLY if video upload failed or no video source available
    if (video_too_large or (tweet_has_video and not video_uploaded)) and not video_uploaded:
        if video_too_large:
            log.info("Video too large, using thumbnail as fallback")
        else:
            log.info("Video source not available in RSS, using thumbnail")

        for i, media in enumerate(video_thumbnails):
            try:
                filename = generate_media_filename(media.url, len(media_data))

                log.info(f"Downloading video thumbnail: {media.url}")
                with requests.get(media.url, stream=True, timeout=10) as r:
                    r.raise_for_status()
                    media_data.append((filename, r.content, 'image'))

            except Exception as thumbnail_error:
                log.error(f"Failed to download video thumbnail {media.url}: {thumbnail_error}")
                continue

    # Update title based on video status
    if tweet_has_video and not video_uploaded:
        if video_too_large:
            title = f"{title}\n\n🎥 *[Video too large for Discord (>25MB), click link to watch]*"
        else:
            # Video exists but RSS doesn't provide the video file
            title = f"{title}\n\n🎥 *[This tweet has video - click link to watch]*"

    # Now send to all webhooks
    all_success = True
    for webhook_url in twitter_user.webhookUrl:
        try:
            log.info(f"Sending to webhook: {webhook_url[:50]}...")
            webhook = DiscordWebhook(url=webhook_url, content=content, rate_limit_retry=True)

            # Add author icon if available
            if author_icon_data:
                webhook.add_file(file=author_icon_data, filename=author_icon_filename)
                author_icon_url = f"attachment://{author_icon_filename}"
            else:
                author_icon_url = twitter_user.icon

            # Add all media files
            for filename, file_data, file_type in media_data:
                webhook.add_file(file=file_data, filename=filename)

            # Create embed
            embed = generate_embed_data(
                title=title,
                media_count=len(media_data),
                has_video=tweet_has_video,
                video_uploaded=video_uploaded,
                timestamp=timestamp,
                author_name=twitter_user.name,
                author_url=twitter_user.link,
                author_icon_url=author_icon_url
            )

            # Set first non-video file as embed image
            for filename, _, file_type in media_data:
                if file_type != 'video':
                    embed.set_image(url=f"attachment://{filename}")
                    break

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

    if all_success:
        log.info(f"✅ Posted tweet with {len(media_data)} media files to {len(twitter_user.webhookUrl)} webhook(s)")

    return all_success


try:
    # Read last run timestamp
    cutoff_time = read_last_run()
    log.info(f"Filtering entries published after: {cutoff_time}")

    # Collect entry data
    entryData: List[EntryData] = []
    nitterServerDistributionList = random.choices(__nitter_url, k=len(twitter_pull_rss_data_list))

    for item, nitter in zip(twitter_pull_rss_data_list, nitterServerDistributionList):
        try:
            feedParse = feedparser.parse(generate_rss_url(item.twitterHandleName, nitter))

            if not hasattr(feedParse, 'feed') or not feedParse.entries:
                log.warning(f"Invalid or empty feed for {item.twitterHandleName}, skipping")
                continue

            # Only add to twitter_user_list if feed has image data
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
                log.warning(f"Feed for {item.twitterHandleName} has no image data, using fallback")
                # Create TwitterUser with fallback values
                twitter_user_list.append(
                    TwitterUser(
                        name=item.twitterHandleName,
                        link=f"{__twitter_url}/{item.twitterHandleName}",
                        icon="",  # No icon available
                        key=item.twitterHandleName,
                        webhookUrl=item.webhookUrl,  # Already a list
                        discordMention=item.discordMention,
                        discordMentionRoleId=item.discordMentionRoleId  # Already a list
                    )
                )

            for data in feedParse.entries:
                # Parse the published date
                pub_date = dateutil.parser.parse(timestr=data.published)

                # Make timezone-naive for comparison
                if pub_date.tzinfo is not None:
                    pub_date = pub_date.replace(tzinfo=None)

                # Only process entries newer than cutoff time
                if pub_date <= cutoff_time:
                    continue

                # Extract media from description
                extracted_media, video_detected = extract_media_from_description(data.description,
                                                                                 __twitter_image_card_link_template)

                tempData = EntryData(
                    title=data.title,
                    description=data.description,
                    link=replace_url_to_twitter(data.link, __twitter_url),
                    pubdate=pub_date,
                    timestamp=generate_timestamp(data.published_parsed),
                    key=item.twitterHandleName,  # Using twitterHandleName as key
                    mediaList=extracted_media,
                    hasVideo=video_detected
                )

                # Filter retweets using the method
                if tempData.is_retweet():
                    if __include_re_tweet:
                        entryData.append(tempData)
                else:
                    entryData.append(tempData)

        except Exception as feed_error:
            log.error(f"Error processing feed for {item.twitterHandleName}: {feed_error}")
            continue

    # Sort by publication date
    entryData = sorted(entryData, key=attrgetter('pubdate'))

    log.info(f"Found {len(entryData)} new entries to post")

    # Post to Discord
    posted_count = 0
    for data in entryData:
        # Find matching twitter user
        twitterUser = next((item for item in twitter_user_list if item.key == data.key), None)
        if not twitterUser:
            log.warning(f"No user found for key {data.key}")
            continue

        try:
            success = send_to_discord_with_media(
                tweet_link=data.link,
                title=data.title,
                tweet_media_list=data.mediaList,
                tweet_has_video=data.hasVideo,
                timestamp=data.timestamp,
                twitter_user=twitterUser
            )

            if success:
                posted_count += 1

            # Rate limit: 1 post per second
            time.sleep(1)

        except Exception as post_error:
            log.error(f"Error posting to Discord: {post_error}")
            continue

    log.info(f"Successfully posted {posted_count}/{len(entryData)} tweets")

    # Update last run timestamp
    if entryData:
        latest_entry = entryData[-1]
        write_last_run(latest_entry.pubdate)
    else:
        # Update to current time if no new entries
        write_last_run()

    log.info("Script completed successfully")

except Exception as main_error:
    log.exception(f"Caught an exception: {main_error}")
    exit(1)