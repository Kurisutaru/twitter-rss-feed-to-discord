import random
from collections import namedtuple
from datetime import datetime
from dateutil.parser import *
from discord_webhook import DiscordWebhook, DiscordEmbed
from operator import attrgetter
from urllib.parse import urlparse
from urllib.parse import urljoin
import re
import sqlite3
import calendar as cal
import feedparser

# Variable
__nitterUrl = ['https://nitter.projectsegfau.lt', 'https://twitter.moe.ngo', 'https://singapore.unofficialbird.com', 'https://nitter.moomoo.me']
__twitterUrl = "https://twitter.com"
__postTemplate = """
Tweet URL : {}

{}
"""
__twitterImageURl = "https://i.imgur.com/5tkPAcG.png"
__purikoneKey = "PCRD"
__blueprotocolKey = "BLPL"
__purikoneGWKey = "PCGW"

__rssTemplate = "{}/{}/rss"

# SQLite
__sqliteDB = "./kuri.db"

# Purikone
__purikoneReDiveTwitterHandleName = "priconne_redive"
__purikoneGWTwitterHandleName = "pricone_GW"
__purikoneDiscordWebhookURL = "https://discord.com/api/webhooks/482038691343106058/si3m9P2vh5xGTKbksS_UVErquKwVlJ1YxNcaVv6V8PNBreFkFcnzBmpH7uUKzR3TwxmM"

# BlueProtocol
__blueProtocolTwitterHandleName = "BLUEPROTOCOL_JP"
__blueprotocolDiscordWebhookURL = "https://discord.com/api/webhooks/1126817494833836112/Mxz4QOwVEvjhhFEhDEmu8zuwzuBpJx94Z-8QSn9VFOKblrL0cOPqWBPFJAIVvZyGMqyp"

# Tuplestuff
TwitterUser = namedtuple('TwitterUser', 'name link icon')
EntryData = namedtuple('EntryData', 'title link pubdate timestamp key')
Post = namedtuple('Post', 'title link key timestamp')


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


def generateTwitterUserFromRSS(feedData: feedparser):
    return TwitterUser(name=generateTwitterEmbedName(feedData.feed.image.title),
                       link=replaceUrlToTwitter(feedData.feed.image.link),
                       icon=generateTwitterProfilePicture(feedData.feed.image.href),
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
    embed.set_footer(text="Kurisutaru Twitter to Discord", icon_url="https://i.imgur.com/tPYHqkU.png")

    # set timestamp (default is now) accepted types are int, float and datetime
    embed.set_timestamp(timestamp)

    return embed


purikoneReDiveFeed = feedparser.parse(generateRssUrl(__purikoneReDiveTwitterHandleName))
purikoneGWFeed = feedparser.parse(generateRssUrl(__purikoneGWTwitterHandleName))
blueprotocolFeed = feedparser.parse(generateRssUrl(__blueProtocolTwitterHandleName))

purikoneReDiveFeedData = purikoneReDiveFeed.entries
purikoneGWFeedData = purikoneGWFeed.entries
blueprotocolFeedData = blueprotocolFeed.entries

# <link>https://nitter.projectsegfau.lt/pricone_GW</link>
# <url>https://nitter.projectsegfau.lt/pic/pbs.twimg.com%2Fprofile_images%2F966599149217923072%2FgRr_MrRO_400x400.jpg</url>
# <title>プリコネR攻略@GameWith / @pricone_GW</title>

purikonePCRDTwitter = generateTwitterUserFromRSS(purikoneReDiveFeed)
purikoneGWTwitter = generateTwitterUserFromRSS(purikoneGWFeed)
blueprotocolTwitter = generateTwitterUserFromRSS(blueprotocolFeed)

con = sqlite3.connect(__sqliteDB)
cur = con.cursor()

entryData = []

for data in purikoneReDiveFeedData:
    entryData.append(EntryData(data.title,
                               replaceUrlToTwitter(data.link),
                               parse(timestr=data.published),
                               generateTimestamp(data.published_parsed),
                               __purikoneKey
                               )
                     )

for data in purikoneGWFeedData:
    entryData.append(EntryData(data.title,
                               replaceUrlToTwitter(data.link),
                               parse(timestr=data.published),
                               generateTimestamp(data.published_parsed),
                               __purikoneGWKey
                               )
                     )

for data in blueprotocolFeedData:
    entryData.append(EntryData(data.title,
                               replaceUrlToTwitter(data.link),
                               parse(timestr=data.published),
                               generateTimestamp(data.published_parsed),
                               __blueprotocolKey
                               )
                     )

entryData = sorted(entryData, key=attrgetter('pubdate'))

cur.executemany("INSERT OR IGNORE INTO post (title, link, pub_date, timestamp, key) VALUES(?, ?, ?, ?, ?)", entryData)
con.commit()

# Reset Entry Data for reuseable
entryData = []

cur.execute("SELECT title, link, key, timestamp FROM post WHERE is_send = 0 ORDER BY timestamp ASC")
rows = cur.fetchall()
for row in rows:
    entryData.append(Post(row[0], row[1], row[2], row[3]))

# for data in entryData:
#     if data.key == __purikoneKey:
#         webhook = DiscordWebhook(url=__purikoneDiscordWebhookURL,
#                                  content=data.link
#                                  )
#         webhook.add_embed(generateEmbedData(title=data.title,
#                                             timestamp=data.timestamp,
#                                             authorName=purikonePCRDTwitter.name,
#                                             authorUrl=purikonePCRDTwitter.link,
#                                             authorIconUrl=purikonePCRDTwitter.icon
#                                             )
#                           )
#         webhook.execute()
#     elif data.key == __purikoneGWKey:
#         webhook = DiscordWebhook(url=__purikoneDiscordWebhookURL,
#                                  content=data.link
#                                  )
#         webhook.add_embed(generateEmbedData(title=data.title,
#                                             timestamp=data.timestamp,
#                                             authorName=purikoneGWTwitter.name,
#                                             authorUrl=purikoneGWTwitter.link,
#                                             authorIconUrl=purikoneGWTwitter.icon
#                                             )
#                           )
#         webhook.execute()
#     elif data.key == __blueprotocolKey:
#         webhook = DiscordWebhook(url=__blueprotocolDiscordWebhookURL,
#                                  content=data.link
#                                  )
#         webhook.add_embed(generateEmbedData(title=data.title,
#                                             timestamp=data.timestamp,
#                                             authorName=blueprotocolTwitter.name,
#                                             authorUrl=blueprotocolTwitter.link,
#                                             authorIconUrl=blueprotocolTwitter.icon
#                                             )
#                           )
#         webhook.execute()

updateData = [[item.link] for item in entryData]
cur.executemany("UPDATE post SET is_send = 1 WHERE link = ?", updateData)
con.commit()
