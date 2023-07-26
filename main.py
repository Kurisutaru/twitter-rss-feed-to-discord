import re
import sqlite3
import calendar as cal
from collections import namedtuple

import feedparser
from discord_webhook import DiscordWebhook

# Variable
__nitterUrl = "https://nitter.projectsegfau.lt"
__twitterUrl = "https://twitter.com"
__postTemplate = """
Tweet URL : {}

{}
"""
__purikoneKey = "PCRD"
__blueprotocolKey = "BP"

# SQLite
__sqliteDB = "./kuri.db"

# Purikone
__purikoneReDiveUrlRss = "https://nitter.projectsegfau.lt/priconne_redive/rss"
__purikoneGWUrlRss = "https://nitter.projectsegfau.lt/priconne_GW/rss"
__purikoneDiscordWebhookURL = "https://discord.com/api/webhooks/482038691343106058/si3m9P2vh5xGTKbksS_UVErquKwVlJ1YxNcaVv6V8PNBreFkFcnzBmpH7uUKzR3TwxmM"

# BlueProtocol
__blueProtocolUrlRss = "https://nitter.projectsegfau.lt/BLUEPROTOCOL_JP/rss"
__blueprotocolDiscordWebhookURL = "https://discord.com/api/webhooks/1126817494833836112/Mxz4QOwVEvjhhFEhDEmu8zuwzuBpJx94Z-8QSn9VFOKblrL0cOPqWBPFJAIVvZyGMqyp"

purikoneReDiveFeedData = feedparser.parse(__purikoneReDiveUrlRss).entries
purikoneGWFeedData = feedparser.parse(__purikoneGWUrlRss).entries
blueprotocolFeedData = feedparser.parse(__blueProtocolUrlRss).entries

con = sqlite3.connect(__sqliteDB)
cur = con.cursor()

entryData = []


def replaceUrlToTwitter(inputString: str):
    returnString = inputString.replace(__nitterUrl, __twitterUrl)
    return returnString.replace('#m', '')


def generateTimestamp(inputTime):
    return cal.timegm(inputTime)


for data in purikoneReDiveFeedData:
    entryData.append((replaceUrlToTwitter(data.title), replaceUrlToTwitter(data.link), generateTimestamp(data.published_parsed), __purikoneKey))

for data in purikoneGWFeedData:
    entryData.append((replaceUrlToTwitter(data.title), replaceUrlToTwitter(data.link), generateTimestamp(data.published_parsed), __purikoneKey))

for data in blueprotocolFeedData:
    entryData.append((replaceUrlToTwitter(data.title), replaceUrlToTwitter(data.link), generateTimestamp(data.published_parsed), __blueprotocolKey))

cur.executemany("INSERT OR IGNORE INTO post (title, link, pub_date, key) VALUES(?, ?, ?, ?)", entryData)
con.commit()

# Reset Entry Data for reuseable
entryData = []
Post = namedtuple('Post', 'title link key')

cur.execute("SELECT title, link, key FROM post WHERE is_send = 1 ORDER BY pub_date DESC")
rows = cur.fetchall()
for row in rows:
    entryData.append(Post(re.sub(r"http\S+", "", row[0]), row[1], row[2]))

for data in entryData:
    if data.key == __purikoneKey:
        DiscordWebhook(url=__purikoneDiscordWebhookURL, content=__postTemplate.format(data.link, data.title)).execute()
    elif data.key == __blueprotocolKey:
        DiscordWebhook(url=__blueprotocolDiscordWebhookURL, content=__postTemplate.format(data.link, data.title)).execute()

cur.executemany("UPDATE post SET is_send = 1 WHERE link = ?", [item.link for item in entryData])
con.commit()


