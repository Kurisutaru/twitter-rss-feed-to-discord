import calendar as cal
import json
import random
import re
import sqlite3
from collections import namedtuple
from datetime import datetime
from operator import attrgetter
from urllib.parse import urljoin, unquote, urlparse

import dateutil.parser
import feedparser
from bs4 import BeautifulSoup
from discord_webhook import DiscordEmbed, DiscordWebhook

# Variable
__twitterUrl: str = 'https://twitter.com'
__twitterImageCardLinkTemplate: str = 'https://pbs.twimg.com/{}{}'
__postVideoIdentifier: str = 'ext_tw_video_thumb'
__postVideoTemplate: str = '''
{}

*[tweet has video]*
'''
__rssTemplate: str = '{}/{}/rss'

# SQLite
__sqliteDB: str = 'kuri.db'

# JSONFile
__jsonFile: str = 'kuri.config.json'

# Tuplestuff
TwitterDbData = namedtuple('TwitterDbData', 'twitterHandleName twitterDbCode webhookUrl')
TwitterUser = namedtuple('TwitterUser', 'name link icon key webhookUrl')
EntryData = namedtuple('EntryData', 'title description link pubdate timestamp key')
Post = namedtuple('Post', 'title description link key timestamp')

twitterPullRssDataList = []
twitterUserList = []

with open(__jsonFile, 'r', encoding='UTF-8') as jsonFile:
    jsonConfig = json.load(jsonFile)

__nitterUrl = jsonConfig['nitterServer']
__footerEmbedText = jsonConfig['config']['footerTextForEmbed']
__footerEmbedImageUrl = jsonConfig['config']['footerImageUrlForEmbed']
__twitterEmbedColor = jsonConfig['config']['footerColorForEmbed']

for item in jsonConfig['twitterWatch']:
    twitterPullRssDataList.append(TwitterDbData(twitterHandleName=item['twitterHandleName'], twitterDbCode=item['twitterDbCode'], webhookUrl=item['webhookUrl']))


def generateRssUrl(twitter_handle_name: str, nitter_url: str):
    return __rssTemplate.format(nitter_url, twitter_handle_name)


def replaceUrlToTwitter(inputString: str, twitterUrl: str):
    returnString = urljoin(twitterUrl, urlparse(inputString).path)
    return returnString


def replaceNitterUrlToTwitterUrl(inputString: str, twitterUrl: str, nitterUrl: []):
    returnString = inputString.replace('http://', 'https://').replace('#m', '')
    for serv in nitterUrl:
        returnString = returnString.replace(urlparse(serv).netloc, urlparse(twitterUrl).netloc)
    return returnString


def cleaningRssDescription(inputString: str, twitterUrl: str, nitterUrl: []):
    returnString = inputString.replace('<![CDATA[', '').replace(']]>', '').replace(' />', '/>')
    for serv in nitterUrl:
        returnString = returnString.replace(urlparse(serv).netloc, urlparse(twitterUrl).netloc)
    return returnString


def generateTwitterEmbedName(inputString: str):
    returnString = inputString.replace(' / ', ' (') + str(')')
    return returnString


def generateTwitterProfilePictureLink(inputString: str):
    returnString = urljoin('https://', unquote(urlparse(inputString).path)).replace('/pic/', '')
    return returnString


def generateTwitterPictureLink(inputString: str, twitterImageCardLinkTemplate: str):
    urlParseData = urlparse(unquote(inputString))
    queryParam = ""
    if bool(urlParseData.query):
        queryParam = str('?') + urlParseData.query
    returnString = twitterImageCardLinkTemplate.format(urlParseData.path, queryParam).replace('/pic/', '')
    return returnString


def generateTimestamp(inputTime):
    return cal.timegm(inputTime)


def generateDateFromTimestamp(inputTime):
    return datetime.fromtimestamp(inputTime)


def generateTwitterUserFromRSS(feedData: feedparser, key: str, webhookUrl: str):
    return TwitterUser(name=generateTwitterEmbedName(feedData.feed.image.title),
                       link=replaceUrlToTwitter(feedData.feed.image.link, __twitterUrl),
                       icon=generateTwitterProfilePictureLink(feedData.feed.image.href),
                       key=key,
                       webhookUrl=webhookUrl
                       )


def removeHttpSFromString(inputString: str):
    returnString = re.sub(r'http\S+', '', inputString)
    return returnString


def generateEmbedColor():
    return random.choice(__twitterEmbedColor)


def generateEmbedData(title: str, description: str, timestamp: any, authorName: str, authorUrl: str, authorIconUrl: str, twitterCardLinkTemplate: str):
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
        embed.set_image(generateTwitterPictureLink(imgUrl['src'], twitterCardLinkTemplate))

    # set footer
    embed.set_footer(text=__footerEmbedText, icon_url=__footerEmbedImageUrl)

    # set timestamp (default is now) accepted types are int, float and datetime
    embed.set_timestamp(timestamp)

    if __postVideoIdentifier in str(imgUrl):
        embed.set_description(__postVideoTemplate.format(title))
    else:
        embed.set_description(title)

    return embed


# Save Twitter to SQLiteDB
entryData = []
nitterServerDistributionList = random.choices(__nitterUrl, k=len(twitterPullRssDataList))

for item, nitter in zip(twitterPullRssDataList, nitterServerDistributionList):
    feedParse = feedparser.parse(generateRssUrl(item.twitterHandleName, nitter))
    twitterUserList.append(generateTwitterUserFromRSS(feedParse, item.twitterDbCode, item.webhookUrl))

    for data in feedParse.entries:
        entryData.append(EntryData(title=data.title,
                                   description=cleaningRssDescription(data.description, __twitterUrl, __nitterUrl),
                                   link=replaceNitterUrlToTwitterUrl(data.link, __twitterUrl, __nitterUrl),
                                   pubdate=dateutil.parser.parse(timestr=data.published),
                                   timestamp=generateTimestamp(data.published_parsed),
                                   key=item.twitterDbCode
                                   )
                         )
entryData = sorted(entryData, key=attrgetter('pubdate'))

conn = sqlite3.connect(__sqliteDB)
conn.executemany('INSERT OR IGNORE INTO post (title, description, link, pub_date, timestamp, key) VALUES(?, ?, ?, ?, ?, ?)', entryData)
conn.commit()

# Post Twitter to Discord Embed
postData = []

for row in conn.execute('SELECT title, description, link, key, timestamp FROM post WHERE is_send = 0 ORDER BY timestamp'):
    postData.append(Post(row[0], row[1], row[2], row[3],  row[4]))

for data in postData:
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
                                            authorIconUrl=twitterUser.icon,
                                            twitterCardLinkTemplate=__twitterImageCardLinkTemplate
                                            )
                          )
        webhook.execute()

updateData = [[item.link] for item in postData]
conn.executemany('UPDATE post SET is_send = 1 WHERE link = ?', updateData)
conn.commit()
conn.close()
