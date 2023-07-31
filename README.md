[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

# Twitter RSS Feed to Discord

This project enables the pooling of data from a Twitter RSS feed sourced from a public Nitter Instance and forwards it to a Discord Webhook Embed. By utilizing this system, you can keep your Discord community updated with the latest tweets from a specific Twitter account without directly interacting with the platform.

## Table of Contents

- [Introduction](#introduction)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Contributing](#contributing)
- [License](#license)

## Introduction

Twitter provides RSS feeds for user timelines, but these feeds have been deprecated. Instead, this project leverages Nitter, a privacy-friendly and open-source alternative front-end for Twitter. The Nitter Instance offers RSS feeds for Twitter user timelines, which allows us to collect and process tweets.

The integration with Discord Webhook Embeds ensures that tweets are presented in an attractive and user-friendly format within your Discord server. This setup is particularly useful for community managers, content curators, or anyone interested in tracking specific Twitter accounts within their Discord community.

## Requirements

Before setting up this project, you need to have the following prerequisites:

- Python (version 3.7 or higher)
- SQLite 3
- Discord account and access to a Discord server with the "Manage Webhooks" permission
- Twitter account (for the target user timeline you want to track)
- Nitter Instance URL (public instance or self-hosted)
- Internet connectivity to fetch data from Twitter RSS feeds and post to Discord Webhook

## Installation

To get started, follow these steps:

1. Clone this repository to your local machine.
2. Install the required Python dependencies by running `pip install -r requirements.txt`.

## Configuration

Before running the script, you need to configure some settings:

1. Open `kuri.config.json` in your preferred text editor.
2. Set the Nitter Instance URL in the `nitterServer` variable. Ensure that the Nitter server serves RSS Feeds.
3. Enter the respective Twitter on URL in the `twitterWatch` variable.
4. Specify the Twitter handle of the user whose timeline you want to track in the `twitterHandleName` variable..
5. Adjust any other optional settings to customize the behavior of the script.

```
{
  "config": {
    "footerTextForEmbed": "Enter any text you want to display at the bottom-left of the embed.",
    "footerImageUrlForEmbed": "Provide the URL of any image you want to include at the bottom-left of the embed."
  },
  "nitterServer": [
    "https://nitter.net",
  ],
  "twitterWatch": [
    {
      "twitterHandleName": "{Twitter Handle Name without @, e.g., @Varenchinusu becomes Varenchinusu}",
      "twitterDbCode": "Enter a unique code for DB identifier.",
      "webhookUrl": "https://{Discord Webhook URL}"
    }
  ]
}
```



## Usage

Once you have completed the installation and configuration, you can run the script:

```bash
python kuri.py
```
The script will start fetching tweets from the specified Twitter user's timeline RSS feed through the Nitter Instance and post them to the configured Discord Webhook. Each tweet will be displayed as an attractive Embed, providing essential information like the tweet content, date, and user details.

It is recommended to automate the script execution using tools like cron (Linux) or Task Scheduler (Windows) to keep the Discord channel updated regularly.

## Contributing

We welcome and appreciate contributions to this project! If you want to contribute, please follow these steps:

1. Fork the repository on GitHub.
2. Create a new branch from the main branch to work on your changes.
3. Make your changes, whether they are bug fixes, feature enhancements, or documentation improvements.
4. Commit your changes with descriptive commit messages.
5. Push the changes to your forked repository.
6. Create a pull request (PR) to the original repository, detailing your changes and explaining the purpose of the PR.

By contributing to this project, you agree to license your contributions under the same [MIT License](LICENSE) as the rest of the project.

We appreciate your efforts and will review your contributions as soon as possible. Thank you for making this project better!

## License

The MIT License (MIT)

Copyright (c) 2023 Kurisutaru

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.