import calendar as cal
import json
import random
import re
import sqlite3
from collections import namedtuple
from datetime import datetime
from operator import attrgetter
from urllib.parse import urljoin
from urllib.parse import urlparse

import feedparser
from dateutil.parser import *
from discord_webhook import DiscordEmbed, DiscordWebhook

# Variable
__twitterUrl = "https://twitter.com"
__postTemplate = """
Tweet URL : {}

{}
"""
__rssTemplate = "{}/{}/rss"

# SQLite
__sqliteDB = "kuri.db"

# JSONFile
__jsonFile = "kuri.config.json"

# Tuplestuff
TwitterDbData = namedtuple('TwitterDbData', 'twitterHandleName twitterDbCode webhookUrl')
TwitterUser = namedtuple('TwitterUser', 'name link icon key webhookUrl')
EntryData = namedtuple('EntryData', 'title link pubdate timestamp key')
Post = namedtuple('Post', 'title link key timestamp')

twitterPullRssDataList = []
twitterUserList = []

with open(__jsonFile, 'r') as jsonFile:
    jsonConfig = json.load(jsonFile)

__nitterUrl = jsonConfig['nitterServer']
__footerEmbedText = jsonConfig['config']['footerTextForEmbed']
__footerEmbedImageUrl = jsonConfig['config']['footerImageUrlForEmbed']

for item in jsonConfig['twitterWatch']:
    twitterPullRssDataList.append(TwitterDbData(twitterHandleName=item['twitterHandleName'], twitterDbCode=item['twitterDbCode'], webhookUrl=item['webhookUrl']))


def generateRssUrl(twitterHandleName: str):
    twitterUrl = random.choice(__nitterUrl)
    return __rssTemplate.format(twitterUrl, twitterHandleName)


def replaceUrlToTwitter(inputString: str):
    returnString = urljoin(__twitterUrl, urlparse(inputString).path)
    return returnString


def generateTwitterEmbedName(inputString: str):
    returnString = inputString.replace(' / ', ' (') + str(")")
    return returnString


def generateTwitterProfilePicture(inputString: str):
    returnString = urljoin('https://', urlparse(inputString).path).replace('/pic/', '').replace('%2F', '/')
    return returnString


def generateTimestamp(inputTime):
    return cal.timegm(inputTime)


def generateDateFromTimestamp(inputTime):
    return datetime.fromtimestamp(inputTime)


def generateTwitterUserFromRSS(feedData: feedparser, key: str, webhookUrl: str):
    return TwitterUser(name=generateTwitterEmbedName(feedData.feed.image.title),
                       link=replaceUrlToTwitter(feedData.feed.image.link),
                       icon=generateTwitterProfilePicture(feedData.feed.image.href),
                       key=key,
                       webhookUrl=webhookUrl
                       )


def removeHttpSFromString(inputString: str):
    returnString = re.sub(r"http\S+", "", inputString)
    return returnString


def generateEmbedData(title: str, timestamp: any, authorName: str, authorUrl: str, authorIconUrl: str):
    # create embed object for webhook
    embed = DiscordEmbed(description=title)

    embed.set_author(name=authorName,
                     url=authorUrl,
                     icon_url=authorIconUrl)

    # set thumbnail
    # embed.set_thumbnail(url="your thumbnail url")

    # set footer
    embed.set_footer(text=__footerEmbedText, icon_url=__footerEmbedImageUrl)

    # set timestamp (default is now) accepted types are int, float and datetime
    embed.set_timestamp(timestamp)

    return embed


entryData = []

for item in twitterPullRssDataList:
    feedParse = feedparser.parse(generateRssUrl(item.twitterHandleName))
    twitterUserList.append(generateTwitterUserFromRSS(feedParse, item.twitterDbCode, item.webhookUrl))

    for data in feedParse.entries:
        entryData.append(EntryData(data.title,
                                   replaceUrlToTwitter(data.link),
                                   parse(timestr=data.published),
                                   generateTimestamp(data.published_parsed),
                                   item.twitterDbCode
                                   )
                         )

con = sqlite3.connect(__sqliteDB)
cur = con.cursor()

entryData = sorted(entryData, key=attrgetter('pubdate'))

cur.executemany("INSERT OR IGNORE INTO post (title, link, pub_date, timestamp, key) VALUES(?, ?, ?, ?, ?)", entryData)
con.commit()

# Reset Entry Data for reuseable
entryData = []

cur.execute("SELECT title, link, key, timestamp FROM post WHERE is_send = 0 ORDER BY timestamp ASC")
rows = cur.fetchall()
for row in rows:
    entryData.append(Post(row[0], row[1], row[2], row[3]))

for data in entryData:
    twitterUser = [item for item in twitterUserList if item.key == data.key][0]
    if twitterUser is not None:
        webhook = DiscordWebhook(url=twitterUser.webhookUrl,
                                 content=data.link
                                 )
        webhook.add_embed(generateEmbedData(title=data.title,
                                            timestamp=data.timestamp,
                                            authorName=twitterUser.name,
                                            authorUrl=twitterUser.link,
                                            authorIconUrl=twitterUser.icon
                                            )
                          )
        webhook.execute()

updateData = [[item.link] for item in entryData]
cur.executemany("UPDATE post SET is_send = 1 WHERE link = ?", updateData)
con.commit()
