import calendar as cal
import json
import random
import re
import sqlite3
from collections import namedtuple
from datetime import datetime
from operator import attrgetter
from os.path import isfile
from urllib.parse import urljoin, unquote, urlparse

import dateutil.parser
import feedparser
from bs4 import BeautifulSoup
from discord_webhook import DiscordEmbed, DiscordWebhook

# Variable
__twitter_url: str = 'https://twitter.com'
__twitter_image_card_link_template: str = 'https://pbs.twimg.com/{}{}'
__post_video_identifier: str = 'ext_tw_video_thumb'
__rss_template: str = '{}/{}/rss'

# SQLite
__sqlite_db: str = 'kuri.db'

# JSONFile
__json_file: str = 'kuri.config.json'

# Tuplestuff
TwitterDbData = namedtuple('TwitterDbData',
                           'twitterHandleName twitterDbCode webhookUrl discordMention discordMentionRoleId')
TwitterUser = namedtuple('TwitterUser', 'name link icon key webhookUrl discordMention discordMentionRoleId')
EntryData = namedtuple('EntryData', 'title description link pubdate timestamp key')
Post = namedtuple('Post', 'title description link key timestamp')

twitter_pull_rss_data_list = []
twitter_user_list = []

# Check if config file exist, if not abort
if not isfile(__json_file):
    print("Config file not found, abort current running script")
    exit(1)

with open(__json_file, 'r', encoding='UTF-8') as jsonFile:
    jsonConfig = json.load(jsonFile)

__nitter_url = jsonConfig['nitterServer']
__footer_embed_text = jsonConfig['config']['footerTextForEmbed']
__footer_embed_image_url = jsonConfig['config']['footerImageUrlForEmbed']
__twitter_embed_color = jsonConfig['config']['footerColorForEmbed']
__include_re_tweet = jsonConfig['config']['includeReTweet']

for item in jsonConfig['twitterWatch']:
    if item['twitterHandleName'] is not None and item['twitterDbCode'] is not None and item[
        'webhookUrl'] is not None and item['discordMention'] is not None:
        twitter_pull_rss_data_list.append(
            TwitterDbData(twitterHandleName=item['twitterHandleName'],
                          twitterDbCode=item['twitterDbCode'],
                          webhookUrl=item['webhookUrl'],
                          discordMention=item['discordMention'] if item['discordMention'] is not None else False,
                          discordMentionRoleId=item['discordMentionRoleId'] if item[
                                                                                   'discordMentionRoleId'] is not None else "")
        )


def generate_rss_url(twitter_handle_name: str, nitter_url: str):
    return __rss_template.format(nitter_url, twitter_handle_name)


def replace_url_to_twitter(input_string: str, twitter_url: str):
    return_string = urljoin(twitter_url, urlparse(input_string).path)
    return return_string


def replace_nitter_url_to_twitter_url(input_string: str, twitter_url: str, nitter_url: []):
    return_string = input_string.replace('http://', 'https://').replace('#m', '')
    for serv in nitter_url:
        return_string = return_string.replace(urlparse(serv).netloc, urlparse(twitter_url).netloc)
    return return_string


def cleaning_rss_description(input_string: str, twitter_url: str, twitter_card_link_template: str, nitter_url: []):
    return_string = input_string.replace('<![CDATA[', '').replace(']]>', '').replace(' />', '/>')
    return_string = unquote(return_string)
    soup = BeautifulSoup(return_string, 'html.parser')
    a_url = soup.find_all('a')
    if a_url is not None:
        for a_item in a_url:
            return_string = return_string.replace(a_item.get('href'), replace_nitter_url_to_twitter_url(a_item.get('href'), twitter_url, nitter_url))
            return_string = return_string.replace(a_item.getText(), replace_nitter_url_to_twitter_url(a_item.getText(), twitter_url, nitter_url))

    img_url = soup.find_all('img')
    if img_url is not None:
        for img_item in img_url:
            return_string = return_string.replace(img_item.get('src'), generate_twitter_picture_link(img_item.get('src'), twitter_card_link_template))
    return return_string


def generate_twitter_embed_name(input_string: str):
    return_string = input_string.replace(' / ', ' (') + str(')')
    return return_string


def generate_twitter_profile_picture_link(input_string: str):
    return_string = urljoin('https://', unquote(urlparse(input_string).path)).replace('/pic/', '')
    return return_string


def generate_twitter_picture_link(input_string: str, twitter_image_card_link_template: str):
    url_parse_data = urlparse(unquote(input_string))
    query_param = ""
    if bool(url_parse_data.query):
        query_param = str('?') + url_parse_data.query
    return_string = twitter_image_card_link_template.format(url_parse_data.path, query_param).replace('/pic/', '')
    return return_string


def generate_timestamp(input_time):
    return cal.timegm(input_time)


def generate_date_from_timestamp(input_time):
    return datetime.fromtimestamp(input_time)


def generate_twitter_user_from_rss(feed_data: feedparser, key: str, webhook_url: str, discord_mention: bool,
                                   discord_mention_role_id: str):
    return TwitterUser(
        name=generate_twitter_embed_name(feed_data.feed.image.title),
        link=replace_url_to_twitter(feed_data.feed.image.link, __twitter_url),
        icon=generate_twitter_profile_picture_link(feed_data.feed.image.href),
        key=key,
        webhookUrl=webhook_url,
        discordMention=discord_mention,
        discordMentionRoleId=discord_mention_role_id
    )


def remove_https_from_string(input_string: str):
    return_string = re.sub(r'http\S+', '', input_string)
    return return_string


def generate_embed_color():
    return random.choice(__twitter_embed_color)


def is_re_tweet(input_string: str):
    return "RT by" in input_string


def generate_embed_data(title: str, description: str, timestamp: any, author_name: str, author_url: str,
                        author_icon_url: str,
                        twitter_card_link_template: str):
    # create embed object for webhook
    embed = DiscordEmbed()

    embed.set_author(name=author_name,
                     url=author_url,
                     icon_url=author_icon_url)

    # set Color
    embed.set_color(generate_embed_color())

    # set thumbnail
    # embed.set_thumbnail(url='your thumbnail url')

    # set image

    soup = BeautifulSoup(description, 'html.parser')
    img_url = soup.find('img')
    if img_url is not None:
        embed.set_image(img_url.get('src'))

    # set footer
    embed.set_footer(text=__footer_embed_text, icon_url=__footer_embed_image_url)

    # set timestamp (default is now) accepted types are int, float and datetime
    embed.set_timestamp(timestamp)

    post_data = title

    # if post have video append tweet has video stuff
    if __post_video_identifier in str(img_url):
        post_data = f'{post_data}\n\n*[tweet has video] *'

    # set Description that compiled with some stuff
    embed.set_description(post_data)

    return embed


try:
    # Save Twitter to SQLiteDB
    entryData = []
    nitterServerDistributionList = random.choices(__nitter_url, k=len(twitter_pull_rss_data_list))

    for item, nitter in zip(twitter_pull_rss_data_list, nitterServerDistributionList):
        feedParse = feedparser.parse(generate_rss_url(item.twitterHandleName, nitter))
        twitter_user_list.append(
            generate_twitter_user_from_rss(feed_data=feedParse, key=item.twitterDbCode, webhook_url=item.webhookUrl,
                                           discord_mention=item.discordMention,
                                           discord_mention_role_id=item.discordMentionRoleId))

        for data in feedParse.entries:
            tempData = EntryData(title=data.title,
                                 description=cleaning_rss_description(data.description, __twitter_url, __twitter_image_card_link_template, __nitter_url),
                                 link=replace_url_to_twitter(data.link, __twitter_url),
                                 pubdate=dateutil.parser.parse(timestr=data.published),
                                 timestamp=generate_timestamp(data.published_parsed),
                                 key=item.twitterDbCode
                                 )
            test = is_re_tweet(data.title)
            if is_re_tweet(data.title):
                if __include_re_tweet:
                    entryData.append(tempData)
            else:
                entryData.append(tempData)

    entryData = sorted(entryData, key=attrgetter('pubdate'))

    conn = sqlite3.connect(__sqlite_db)
    conn.executemany(
        'INSERT OR IGNORE INTO post (title, description, link, pub_date, timestamp, key) VALUES(?, ?, ?, ?, ?, ?)',
        entryData)
    conn.commit()

    # Post Twitter to Discord Embed
    postData = []

    for row in conn.execute(
            'SELECT title, description, link, key, timestamp FROM post WHERE is_send = 0 ORDER BY timestamp'):
        postData.append(Post(row[0], row[1], row[2], row[3], row[4]))

    for data in postData:
        twitterUser = [item for item in twitter_user_list if item.key == data.key][0]
        if twitterUser is not None:
            # Adding mention if needed
            linkData = data.link
            if twitterUser.discordMention:
                linkData = f'{linkData}\n\n<@&{twitterUser.discordMentionRoleId}>'

            webhook = DiscordWebhook(url=twitterUser.webhookUrl,
                                     content=linkData
                                     )
            webhook.add_embed(generate_embed_data(title=data.title,
                                                  description=data.description,
                                                  timestamp=data.timestamp,
                                                  author_name=twitterUser.name,
                                                  author_url=twitterUser.link,
                                                  author_icon_url=twitterUser.icon,
                                                  twitter_card_link_template=__twitter_image_card_link_template,
                                                  )
                              )
            webhook.execute()

    updateData = [[item.link] for item in postData]
    conn.executemany('UPDATE post SET is_send = 1 WHERE link = ?', updateData)
    conn.commit()
    conn.close()
except Exception as e:
    print(f"Caught an exception: {e}")
    exit(1)
