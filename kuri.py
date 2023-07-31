import calendar as cal
import json
import random
import re
import sqlite3
from collections import namedtuple
from datetime import datetime
from operator import attrgetter
from urllib.parse import urljoin, unquote
from urllib.parse import urlparse

import feedparser
from dateutil.parser import *
from discord_webhook import DiscordEmbed, DiscordWebhook
from bs4 import BeautifulSoup

# Variable
__twitterUrl = 'https://twitter.com'
__twitterImageCardLinkTemplate = 'https://pbs.twimg.com/{}{}'
__postVideoIdentifier = 'ext_tw_video_thumb'
__postVideoTemplate = '''
{}

*[tweet has video]*
'''
__rssTemplate = '{}/{}/rss'

# SQLite
__sqliteDB = 'kuri.db'

# JSONFile
__jsonFile = 'kuri.config.json'

# Tuplestuff
TwitterDbData = namedtuple('TwitterDbData', 'twitterHandleName twitterDbCode webhookUrl')
TwitterUser = namedtuple('TwitterUser', 'name link icon key webhookUrl')
EntryData = namedtuple('EntryData', 'title description link pubdate timestamp key')
Post = namedtuple('Post', 'title description link key timestamp')

twitterPullRssDataList = []
twitterUserList = []

with open(__jsonFile, 'r') as jsonFile:
    jsonConfig = json.load(jsonFile)

__nitterUrl = jsonConfig['nitterServer']
__footerEmbedText = jsonConfig['config']['footerTextForEmbed']
__footerEmbedImageUrl = jsonConfig['config']['footerImageUrlForEmbed']
__twitterEmbedColor = jsonConfig['config']['footerColorForEmbed']

for item in jsonConfig['twitterWatch']:
    twitterPullRssDataList.append(TwitterDbData(twitterHandleName=item['twitterHandleName'], twitterDbCode=item['twitterDbCode'], webhookUrl=item['webhookUrl']))


def generateRssUrl(twitterHandleName: str):
    twitterUrl = random.choice(__nitterUrl)
    return __rssTemplate.format(twitterUrl, twitterHandleName)


def replaceUrlToTwitter(inputString: str):
    returnString = urljoin(__twitterUrl, urlparse(inputString).path)
    return returnString


def replaceNitterUrlToTwitterUrl(inputString: str):
    returnString = inputString.replace('http://', 'https://').replace('#m', '')
    for serv in __nitterUrl:
        returnString = returnString.replace(urlparse(serv).netloc, urlparse(__twitterUrl).netloc)
    return returnString


def cleaningRssDescription(inputString: str):
    returnString = inputString.replace('<![CDATA[', '').replace(']]>', '').replace(' />', '/>')
    returnString = replaceNitterUrlToTwitterUrl(returnString)
    return returnString


def generateTwitterEmbedName(inputString: str):
    returnString = inputString.replace(' / ', ' (') + str(')')
    return returnString


def generateTwitterProfilePictureLink(inputString: str):
    returnString = urljoin('https://', unquote(urlparse(inputString).path)).replace('/pic/', '')
    return returnString


def generateTwitterPictureLink(inputString: str):
    urlParseData = urlparse(unquote(inputString))
    queryParam = ""
    if bool(urlParseData.query):
        queryParam = str('?') + urlParseData.query
    returnString = __twitterImageCardLinkTemplate.format(urlParseData.path, queryParam).replace('/pic/', '')
    return returnString


def generateTimestamp(inputTime):
    return cal.timegm(inputTime)


def generateDateFromTimestamp(inputTime):
    return datetime.fromtimestamp(inputTime)


def generateTwitterUserFromRSS(feedData: feedparser, key: str, webhookUrl: str):
    return TwitterUser(name=generateTwitterEmbedName(feedData.feed.image.title),
                       link=replaceUrlToTwitter(feedData.feed.image.link),
                       icon=generateTwitterProfilePictureLink(feedData.feed.image.href),
                       key=key,
                       webhookUrl=webhookUrl
                       )


def removeHttpSFromString(inputString: str):
    returnString = re.sub(r'http\S+', '', inputString)
    return returnString


def generateEmbedColor():
    return random.choice(__twitterEmbedColor)


def generateEmbedData(title: str, description: str, timestamp: any, authorName: str, authorUrl: str, authorIconUrl: str):
    # create embed object for webhook
    embed = DiscordEmbed()

    embed.set_author(name=authorName,
                     url=authorUrl,
                     icon_url=authorIconUrl)

    # set Color
    embed.set_color(generateEmbedColor())

    # set thumbnail
    # embed.set_thumbnail(url='your thumbnail url')

    # set image

    soup = BeautifulSoup(description, 'html.parser')
    imgUrl = soup.find('img')
    if imgUrl is not None:
        embed.set_image(generateTwitterPictureLink(imgUrl['src']))

    # set footer
    embed.set_footer(text=__footerEmbedText, icon_url=__footerEmbedImageUrl)

    # set timestamp (default is now) accepted types are int, float and datetime
    embed.set_timestamp(timestamp)

    if __postVideoIdentifier in str(imgUrl):
        embed.set_description(__postVideoTemplate.format(title))
    else:
        embed.set_description(title)

    return embed


entryData = []

for item in twitterPullRssDataList:
    feedParse = feedparser.parse(generateRssUrl(item.twitterHandleName))
    twitterUserList.append(generateTwitterUserFromRSS(feedParse, item.twitterDbCode, item.webhookUrl))

    for data in feedParse.entries:
        entryData.append(EntryData(title=data.title,
                                   description=cleaningRssDescription(data.description),
                                   link=replaceNitterUrlToTwitterUrl(data.link),
                                   pubdate=parse(timestr=data.published),
                                   timestamp=generateTimestamp(data.published_parsed),
                                   key=item.twitterDbCode
                                   )
                         )

con = sqlite3.connect(__sqliteDB)
cur = con.cursor()

entryData = sorted(entryData, key=attrgetter('pubdate'))

cur.executemany('INSERT OR IGNORE INTO post (title, description, link, pub_date, timestamp, key) VALUES(?, ?, ?, ?, ?, ?)', entryData)
con.commit()

# Reset Entry Data for reuseable
entryData = []

cur.execute('SELECT title, description, link, key, timestamp FROM post WHERE is_send = 0 ORDER BY timestamp')
rows = cur.fetchall()
for row in rows:
    entryData.append(Post(row[0], row[1], row[2], row[3],  row[4]))

for data in entryData:
    twitterUser = [item for item in twitterUserList if item.key == data.key][0]
    if twitterUser is not None:
        webhook = DiscordWebhook(url=twitterUser.webhookUrl,
                                 content=data.link
                                 )
        webhook.add_embed(generateEmbedData(title=data.title,
                                            description=data.description,
                                            timestamp=data.timestamp,
                                            authorName=twitterUser.name,
                                            authorUrl=twitterUser.link,
                                            authorIconUrl=twitterUser.icon
                                            )
                          )
        webhook.execute()

updateData = [[item.link] for item in entryData]
cur.executemany('UPDATE post SET is_send = 1 WHERE link = ?', updateData)
con.commit()
