This Telegram Bot, based on [python-aria-mirror-bot](https://github.com/lzzy12/python-aria-mirror-bot), has undergone
substantial modifications and is designed for efficiently leeching files from the Internet to Telegram. It is built
using asynchronous programming in Python.

- **TELEGRAM CHANNEL:** https://t.me/mltb_official_channel
- **TELEGRAM GROUP:** https://t.me/mltb_official_support

<details>
  <summary><h1>Features</h1></summary>

<details>
  <summary><h5>QBittorrent</h5></summary>

- External access to webui, so you can remove files or edit settings. Then you can sync settings in database with sync button in bsetting
- Select files from a Torrent before and during download using mltb file selector (Requires Base URL) (task option)
- Seed torrents to a specific ratio and time (task option)
- Edit Global Options while the bot is running from bot settings (global option)

</details>

<details>
  <summary><h5>Aria2c</h5></summary>

- Select files from a Torrent before and during download (Requires Base URL) (task option)
- Seed torrents to a specific ratio and time (task option)
- Netrc support (global option)
- Direct link authentication for a specific link while using the bot (it will work even if only the username or password
  is provided) (task option)
- Edit Global Options while the bot is running from bot settings (global option)

</details>

<details>
  <summary><h5>TG Upload/Download</h5></summary>

- Split size (global, user, and task option)
- Thumbnail (user and task option)
- Leech filename prefix (user option)
- Set upload as a document or as media (global, user and task option)
- Upload all files to a specific chat (superGroup/channel/private/topic) (global, user, and task option)
- Equal split size settings (global and user option)
- Ability to leech split file parts in a media group (global and user option)
- Download restricted messages (document or link) by tg private/public/super links (task option)
- Choose transfer by bot or user session in case you have a premium plan (global, user option and task option)
- Mix upload between user and bot session with respect to file size (global, user option and task option)
- Upload with custom layout multiple thumbnail (global, user option and task option)
- Topics support

</details>



<details>
  <summary><h5>Status</h5></summary>

- Download/Upload/Extract/Archive/Seed Status
- Status Pages for an unlimited number of tasks, view a specific number of tasks in a message (global option)
- Interval message update (global option)
- Next/Previous buttons to get different pages (global and user option)
- Status buttons to get specific tasks for the chosen status regarding transfer type if the number of tasks is more than
  30 (global and user option)
- Steps buttons for how much next/previous buttons should step backward/forward (global and user option)
- Status for each user (no auto refresh)

</details>

<details>
  <summary><h5>Yt-dlp</h5></summary>

- Yt-dlp quality buttons (task option)
- Ability to use a specific yt-dlp option (global, user, and task option)
- Netrc support (global option)
- Cookies support (global option)
- Embed the original thumbnail and add it for leech
- All supported audio formats

</details>

<details>
  <summary><h5>Mongo Database</h5></summary>

- Store bot settings
- Store user settings including thumbnails and all private files
- Store RSS data
- Store incomplete task messages
- Store config.py file on first build and in case any change occurred to it, then next build it will define variables
  from config.py instead of database

</details>

<details>
  <summary><h5>Torrents Search</h5></summary>

- Search on torrents with Torrent Search API
- Search on torrents with variable plugins using qBittorrent search engine

</details>

<details>
  <summary><h5>Archives</h5></summary>

- Extract splits with or without password
- Zip file/folder with or without password and splits in case of leech
- Using 7z package to extract with or without password all supported types

</details>

<details>
  <summary><h5>RSS</h5></summary>

- Based on this repository [rss-chan](https://github.com/hyPnOtICDo0g/rss-chan)
- Rss feed (user option)
- Title Filters (feed option)
- Edit any feed while running: pause, resume, edit command and edit filters (feed option)
- Sudo settings to control users feeds
- All functions have been improved using buttons from one command.

</details>

<details>
  <summary><h5>Overall</h5></summary>

- Docker image support for linux `amd64, arm64/v8, arm/v7`
- Edit variables and overwrite the private files while bot running (bot, user settings)
- Update bot at startup and with restart command using `UPSTREAM_REPO`
- Telegraph. Based on [Sreeraj](https://github.com/SVR666) loaderX-bot
- Leech/Watch by reply
- Leech multi links/files with one command
- Custom name for all links except torrents. For files you should add extension except yt-dlp links (global and user
  option)
- Exclude files with specific extensions from being uploaded (global and user option)
- Queueing System for all tasks (global option)
- Ability to zip/unzip multi links in same directory. Mostly helpful in unzipping tg file parts (task option)
- Bulk download from telegram txt file or text message contains links separated by new line (task option)
- Join split files that were split before by split(linux pkg) (task option)
- Sample video Generator (task option)
- Screenshots Generator (task option)
- Ability to cancel upload/archive/extract/split/queue (task option)
- Cancel all buttons for choosing specific tasks status to cancel (global option)
- Convert videos and audios to specific format with filter (task option)
- Force start to upload or download or both from queue using cmds or args once you add the download (task option)
- Shell and Executor
- Add sudo users
- Name Substitution to rename the files before upload
- FFmpeg commands to execute it after download (task option)
- Supported Direct links Generators:

> mediafire (file/folders), hxfile.co (need cookies txt with name) [hxfile.txt], streamtape.com, streamsb.net, streamhub.ink,
> streamvid.net, doodstream.com,
> feurl.com, upload.ee, pixeldrain.com, racaty.net, 1fichier.com, 1drv.ms (Only works for file not folder or business
> account), filelions.com, streamwish.com, send.cm (file/folders), solidfiles.com, linkbox.to (file/folders),
> shrdsk.me (
> sharedisk.io), akmfiles.com, wetransfer.com, pcloud.link, gofile.io (file/folders), easyupload.io, mdisk.me (with
> ytdl),
> tmpsend.com, qiwi.gg, berkasdrive.com, mp4upload.com, terabox.com (videos only file/folders),
> mega.nz / mega.co.nz (folder shares and single files).

</details>
</details>

<details>
  <summary><h1>How to deploy?</h1></summary>

<details>
  <summary><h2>Prerequisites</h2></summary>

<details>
  <summary><h5>1. Installing requirements</h5></summary>

- Clone this repo:

```
git clone https://github.com/anasty17/mirror-leech-telegram-bot mirrorbot/ && cd mirrorbot
```

- For Debian based distros

```
sudo apt install python3 python3-pip
```

Install Docker by following the [official Docker docs](https://docs.docker.com/engine/install/debian/)

- For Arch and its derivatives:

```
sudo pacman -S docker python
```

</details>

<details>
  <summary><h5>2. Setting up config file</h5></summary>

```
cp config_sample.py config.py
```

Fill up rest of the fields. Meaning of each field is discussed below.

**1. Required Fields**

- `BOT_TOKEN` (`Str`):  The Telegram Bot Token that you got from [@BotFather](https://t.me/BotFather).

- `OWNER_ID` (`Int`):  The Telegram User ID (not username) of the Owner of the bot.

- `TELEGRAM_API` (`Int`): This is to authenticate your Telegram account for downloading Telegram files. You can get this
  from <https://my.telegram.org>.

- `TELEGRAM_HASH` (`Str`):  This is to authenticate your Telegram account for downloading Telegram files. You can get this
  from <https://my.telegram.org>.

**2. Optional Fields**
- `TG_PROXY` (`Dict`): The Proxy settings as dict. Ex: {"scheme": "socks5", "hostname": "11.22.33.44", "port": 1234, "username": "user", "password": "pass"}. The username and password can be omitted if the proxy doesn’t require authorization.

- `USER_SESSION_STRING` (`Str`): To download/upload from your telegram account if user is `PREMIUM` and to send rss. To generate session string use this command `python3 generate_string_session.py` after mounting repo folder for sure. **NOTE**: You can't use bot with private message. Use it with superGroup.

- `DATABASE_URL` (`Str`): Your PostgreSQL URL (Connection string), e.g. `postgresql://user:pass@host:5432/dbname`. The schema is created automatically on first boot. Data saved in the database: bot settings, users settings, rss data, incomplete tasks, copy records and (encrypted) private files. **NOTE**: unlike the old Mongo-backed version there is no hosted web console to browse collections; `psql` is the equivalent. Installations that already ran on Mongo can be carried over with `tools/migrate_mongo_to_pg.py` (see the migration section below).

- `DATABASE_NAME` (`Str`): Optional. Overrides the database name from the URL, exactly like Mongo's database argument did. Leave it empty when the URL already names the database. Default is `mltb`.

- `CMD_SUFFIX` (`Str`|`Int`): Commands index number. This number will be added to the end of all commands.

- `AUTHORIZED_CHATS` (`Str`): Fill user_id and chat_id of groups/users you want to authorize. To auth only specific topic(s) write it in this format `chat_id|thread_id` Ex:-100XXXXXXXXXXX or -100XXXXXXXXXXX|10 or -100XXXXXXXXXXX|10|12. Separate them by spaces.

- `SUDO_USERS` (`Str`):  Fill user_id of users whom you want to give sudo permission. Separate them by spaces.

- `STATUS_UPDATE_INTERVAL` (`Int`): Time in seconds after which the progress/status message will be updated. Recommended `10` seconds at least.

- `STATUS_LIMIT` (`Int`): Limit the no. of tasks shown in status message with buttons. Default is `4`. **NOTE**: Recommended limit is `4` tasks.

- `EXCLUDED_EXTENSIONS` (`Str`): File extensions that won't be uploaded. Separate them by spaces.

- `INCLUDED_EXTENSIONS` (`Str`): File extensions to be uploaded. `EXCLUDED_EXTENSIONS` will be ignored if you filled this! Separate them by spaces.

- `INCOMPLETE_TASK_NOTIFIER` (`Bool`): Get incomplete task messages after restart. Require database and superGroup. Default
is `False`.

- `FILELION_API` (`Str`): Filelion api key to leech Filelion links. Get it
from [Filelion](https://vidhide.com/?op=my_account).

- `STREAMWISH_API` (`Str`): Streamwish api key to leech Streamwish links. Get it
from [Streamwish](https://streamwish.com/?op=my_account).

- `YT_DLP_OPTIONS` (`Dict`): Dict of yt-dlp options. Check all possible
options [HERE](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/YoutubeDL.py#L184) or use this [script](https://t.me/mltb_official_channel/177) to convert cli arguments to api options. Format: {key: value, key: value, key: value}.
  - Example: {"format": "bv*+mergeall[vcodec=none]", "nocheckcertificate": True, "playliststart": 10, "fragment_retries": float("inf"), "matchtitle": "S13", "writesubtitles": True, "live_from_start": True, "postprocessor_args": {"ffmpeg": ["-threads", "4"]}, "wait_for_video": (5, 100), "download_ranges": [{"start_time": 0, "end_time": 10}]}

- `ALLDEBRID_API_KEY` (`Str`): Alldebrid api key.

- `WARP_ENABLED` (`Bool`): Route Mega downloads through Cloudflare WARP's SOCKS5 proxy, and restart the tunnel for a fresh IP when Mega reports its per-IP bandwidth quota spent. Needs `warp-cli` installed and registered on the host. Note that enabling proxy mode takes the whole host off a full WARP tunnel if it was on one. Default is `True`; with `False` the downloads use the host's own IP and a quota error just fails the task.

- `WARP_PROXY_PORT` (`Int`): Port WARP's SOCKS5 listener is bound to. Default is `40000`.

- `MEGA_PROXY_URL` (`Str`): An explicit proxy for Mega traffic, e.g. `socks5://127.0.0.1:1080`, overriding WARP's own listener. Leave empty to use WARP.

- `MEGA_CONNECTIONS` (`Int`): Parallel ranged connections per Mega file. Default is `4`; more invites a rate limit.

- `MEGA_MAX_RESTARTS` (`Int`): How many times one file may rotate the egress IP before giving up. Default is `3`.

- `FFMPEG_CMDS` (`Dict`): Dict of list values of ffmpeg commands. You can set multiple ffmpeg commands for all files before upload. Don't write ffmpeg at beginning, start directly with the arguments. `Dict`
  - Examples: {"subtitle": ["-i mltb.mkv -c copy -c:s srt mltb.mkv", "-i mltb.video -c copy -c:s srt mltb"], "convert": ["-i mltb.m4a -c:a libmp3lame -q:a 2 mltb.mp3", "-i mltb.audio -c:a libmp3lame -q:a 2 mltb.mp3"], extract: ["-i mltb -map 0:a -c copy mltb.mka -map 0:s -c copy mltb.srt"], "metadata": ["-i mltb.mkv -map 0 -map -0:v:1 -map -0:s -map 0:s:0 -map -0:v:m:attachment -c copy -metadata:s:v:0 title={title} -metadata:s:a:0 title={title} -metadata:s:a:1 title={title2} -metadata:s:a:2 title={title2} -c:s srt -metadata:s:s:0 title={title3} mltb -y -del"], "watermark": ["-i mltb -i tg://openmessage?user_id=5272663208&message_id=322801 -filter_complex 'overlay=W-w-10:H-h-10' -c:a copy mltb"]}
  **Notes**:
  - Don't add ffmpeg at the beginning!
  - Add `-del` to the list which you want from the bot to delete the original files after command run complete!
  - To execute one of those lists in bot for example, you must use -ff subtitle (list key) or -ff convert (list key)
  **Example**:
  - Here I will explain how to use mltb.* which is reference to files you want to work on.
  1. First cmd: the input is mltb.mkv so this cmd will work only on mkv videos and the output is mltb.mkv also so all outputs is mkv. `-del` will delete the original media after complete run of the cmd.
  2. Second cmd: the input is mltb.video so this cmd will work on all videos and the output is only mltb so the extension is same as input files.
  3. Third cmd: the input is mltb.m4a so this cmd will work only on m4a audios and the output is mltb.mp3 so the output extension is mp3.
  4. Fourth cmd: the input is mltb.audio so this cmd will work on all audios and the output is mltb.mp3 so the output extension is mp3.
  5. FFmpeg Variables in last cmd which is metadata ({title}, {title2}, etc...), you can edit them in usetting
  6. Telegram link for small size inputs like photo to set watermark.

- `NAME_SUBSTITUTE` (`Str`): Add word/letter/character/sentence/pattern to remove or replace with other words with sensitive case or without. 
  **Notes**:
    - Before any character you must add `\BACKSLASH`, those are the characters: `\^$.|?*+()[]{}-`
    * Example: script/code/s | mirror/leech | tea/ /s | clone | cpu/ | \[mltb\]/mltb | \\text\\/text/s
    - script will get replaced by code with sensitive case
    - mirror will get replaced by leech
    - tea will get replaced by space with sensitive case
    - clone will get removed
    - cpu will get replaced by space
    - [mltb] will get replaced by mltb
    - \text\ will get replaced by text with sensitive case

**3. Update**

- `UPSTREAM_REPO` (`Str`): Your github repository link, if your repo is private add `https://username:{githubtoken}@github.com/{username}/{reponame}` format. Get token from [Github settings](https://github.com/settings/tokens). So you can update your bot from filled repository on each restart.
  - **NOTE**: Any change in docker or requirements you need to deploy/build again with updated repo to take effect. DON'T delete .gitignore file. For more information read [THIS](https://github.com/anasty17/mirror-leech-telegram-bot/tree/master#upstream-repo-recommended).

- `UPSTREAM_BRANCH` (`Str`): Upstream branch for update. Default is `master`.

**4. Leech**

- `LEECH_SPLIT_SIZE` (`Int`): Size of split in bytes. Default is `~2GB` (2000 MiB). Default is `~4GB` (4000 MiB) if your account is premium.

- `AS_DOCUMENT` (`Bool`): Default type of Telegram file upload. Default is `False` mean as media.

- `EQUAL_SPLITS` (`Bool`): Split files larger than **LEECH_SPLIT_SIZE** into equal parts size (Not working with zip cmd). Default is `False`.

- `MEDIA_GROUP` (`Bool`): View Uploaded split file parts in media group. Default is `False`.

- `USER_TRANSMISSION` (`Bool`): Upload/Download by user session. Only in superChat. Default is `False`.

- `HYBRID_LEECH` (`Bool`): Upload by user and bot session with respect to file size. Only in superChat. Default is `False`.

- `LEECH_FILENAME_PREFIX` (`Str`): Add custom word to leeched file name.

- `LEECH_DUMP_CHAT` (`Int`|`Str`): ID or USERNAME or PM(private message) to where files would be uploaded. Add `-100` before channel/superGroup id. To use only specific topic write it in this format `chat_id|thread_id`. Ex: -100XXXXXXXXXXX or "-100XXXXXXXXXXX|10" or "pm" or "@xxxxxxx" or "@xxxxxxx|10".

- `CLONE_DUMP_CHATS` (`List`|`Int`|`Str`): LIST of ID|USERNAME or ID or USERNAME or PM(private message) to where leeched files would be copied. Add `-100` before channel/superGroup id. To use only specific topic write it in this format `chat_id|thread_id`. Ex: ["pm", -100xxxx555, "@username", "@username|8", "-100xxx8886|2"] or -100xxx5555 or "-100xx555566|6" or "@username" or "pm". **Note**: Add `chat_id` inside `quotation marks` only if you will add thread_id with it.

- `THUMBNAIL_LAYOUT` (`Str`): Thumbnail layout (widthxheight, 2x2, 3x3, 2x4, 4x4, ...) of how many photo arranged for the thumbnail.

- `FILES_LINKS` (`Bool`): Enable files link after Leech complete, those link(s) will be sent in the same chat where you sent the cmd. Default is `False`.

**5. qBittorrent/Aria2c**

- `TORRENT_TIMEOUT` (`Int`): Timeout of dead torrents downloading with qBittorrent and Aria2c in seconds.

- `BASE_URL` (`Str`): Valid BASE URL where the bot is deployed to use torrent web files selection. Format of URL should be `http://myip`, where `myip` is the IP/Domain(public) of your bot or if you have chosen port other than `80` so write it in this format `http://myip:port` (`http` and not `https`).

- `BASE_URL_PORT` (`Int`): Which is the **BASE_URL** Port. Default is `80`.

- `WEB_PINCODE` (`Bool`): Whether to ask for pincode before selecting files from torrent in web or not. Default is `False`.
    - **Qbittorrent NOTE**: If you're facing RAM issues then set limit for `MaxConnections`, decrease `AsyncIOThreadsCount`, set limit of `DiskWriteCacheSize` to `32` and decrease `MemoryWorkingSetLimit` from qbittorrent.conf or bsetting command.
    - Open port 8090 in your vps to access webui from any device. username: mltb, password: mltbmltb

**6. RSS**

- `RSS_DELAY` (`Int`): Time in seconds for rss refresh interval. Recommended `600` second at least. Default is `600` in sec.

- `RSS_SIZE_LIMIT` (`Int`): Item size limit in bytes. Default is `0`.

- `RSS_CHAT`: The RSS monitor requires a chat to post results and notifications. The bot must be a member of this chat. When a command is configured for a subscription (e.g., `-c ql -doc`), downloads start automatically and results are posted here. Without a command, only feed info (name, link, size) is posted.
    - **Setup**: Run `/rss` in the desired chat and click **Use This Chat** (sudo only). This auto-detects the chat ID and topic. For channels, set the chat ID manually via config/env/bot settings using the format: `ID or USERNAME or ID|TOPIC_ID or USERNAME|TOPIC_ID`.
    - **Note**: Without `DATABASE_URL`, feeds received while the bot is offline will be missed.

**7. Queue System**

- `QUEUE_ALL` (`Int`): Number of parallel tasks of downloads and uploads. For example if 20 task added and `QUEUE_ALL` is `8`, then the summation of uploading and downloading tasks are 8 and the rest in queue. **NOTE**: if you want to fill `QUEUE_DOWNLOAD` or `QUEUE_UPLOAD`, then `QUEUE_ALL` value must be greater than or equal to the greatest one and less than or equal to summation of `QUEUE_UPLOAD` and `QUEUE_DOWNLOAD`.

- `QUEUE_DOWNLOAD` (`Int`): Number of all parallel downloading tasks.

- `QUEUE_UPLOAD` (`Int`): Max parallel uploading tasks **per destination chat** (leech dump / origin chat). Each chat is limited independently instead of globally.

**8. Torrent Search**

- `SEARCH_API_LINK` (`Str`): Search api app link. Get your api from deploying this [repository](https://github.com/Ryuk-me/Torrent-Api-py).
    - Supported Sites:
  > 1337x, Piratebay, Nyaasi, Torlock, Torrent Galaxy, Zooqle, Kickass, Bitsearch, MagnetDL, Libgen, YTS, Limetorrent,
  TorrentFunk, Glodls, TorrentProject and YourBittorrent

- `SEARCH_LIMIT` (`Int`): Search limit for search api, limit for each site and not overall result limit. Default is zero (Default api limit for each site).

- `SEARCH_PLUGINS` (`List`): List of qBittorrent search plugins (github raw links). I have added some plugins, you can remove/add plugins as you want. Main Source: [qBittorrent Search Plugins (Official/Unofficial)](https://github.com/qbittorrent/search-plugins).

------

</details>
</details>

<details>
  <summary><h2>Build And Run</h2></summary>

Make sure you still mount the repo folder and installed the docker from official documentation.

- There are two methods to build and run the docker:
    1. Using official docker commands.
    2. Using docker compose plugin. (Recommended)

------

<details>
  <summary><h3>Using Official Docker Commands</h3></summary>

- Build Docker image:

```
sudo docker build . -t mltb
```

- Run the image:

```
sudo docker run --network host mltb
```

- To stop the running image:

```
sudo docker ps
```

```
sudo docker stop id
```

----

</details>

<details>
  <summary><h3>Using Docker Compose Plugin</h3></summary>

- Install docker compose plugin

```
sudo apt install docker-compose-plugin
```

- Build and run Docker image:

```
sudo docker compose up
```

- After editing files with nano, for example (nano start.sh) or git pull you must use --build to edit container files:

```
sudo docker compose up --build
```

- To stop the running container:

```
sudo docker compose stop
```

- To run the container:

```
sudo docker compose start
```

- To get log from already running container (after mounting the folder):

```
sudo docker compose logs --follow
```

------

</details>

**IMPORTANT NOTES**:
1. Flush your machine iptables to use your opened ports with docker from the host network. 

```
# Flush All Rules (Reset iptables)
sudo iptables -F
sudo iptables -X
sudo iptables -t nat -F
sudo iptables -t nat -X
sudo iptables -t mangle -F
sudo iptables -t mangle -X

sudo ip6tables -F
sudo ip6tables -X
sudo ip6tables -t nat -F
sudo ip6tables -t nat -X
sudo ip6tables -t mangle -F
sudo ip6tables -t mangle -X

# Set Default Policies
sudo iptables -P INPUT ACCEPT
sudo iptables -P FORWARD ACCEPT
sudo iptables -P OUTPUT ACCEPT

sudo ip6tables -P INPUT ACCEPT
sudo ip6tables -P FORWARD ACCEPT
sudo ip6tables -P OUTPUT ACCEPT

# save
sudo iptables-save | sudo tee /etc/iptables/rules.v4
sudo ip6tables-save | sudo tee /etc/iptables/rules.v6
```

2. Set `BASE_URL_PORT` variable to any port you want to use. Default is `80`.

3. Check the number of processing units of your machine with `nproc` cmd and times it by 4, then
   edit `AsyncIOThreadsCount` in qBittorrent.conf or while bot working from bsetting->qbittorrent settings.

------

</details>
</details>

<details>
  <summary><h1>Extras</h1></summary>

<details>
  <summary><h5>Bot commands to be set in <a href="https://t.me/BotFather">@BotFather</a></h5></summary>

```
leech - or /l Upload to telegram
qbleech - or /ql Leech torrent using qBittorrent
ytdlleech - or /yl Leech yt-dlp supported links
bypass - or /bp Bypass and get the direct link
usetting - or /us User settings
bsetting - or /bs Bot settings
status - Get Leech Status message
sel - Select files from torrent
rss - Rss menu
search - Search for torrents with API
cancel - or /c Cancel a task
cancelall - Cancel all tasks
forcestart - or /fs to start task from queue
login - Login with your telegram account
logout - Revoke your saved session
users - Show users settings
addsudo - Add sudo user
rmsudo - Remove sudo user
log - Get the Bot Log
auth - Authorize user or chat
unauth - Unauthorize user or chat
shell - Run commands in Shell
aexec - Execute async function
exec - Execute sync function
clearlocals - Clear exec locals
restart - Restart the Bot
stats - Bot Usage Stats
ping - Ping the Bot
help - All cmds with description
```

------

</details>

<details>
  <summary><h5>UPSTREAM REPO (Recommended)</h5></summary>

- `UPSTREAM_REPO` variable can be used for edit/add any file in repository.
- You can add private/public repository link to grab/overwrite all files from it.
- You can skip adding the private files like cookies.txt or .netrc before deploying, simply
  fill `UPSTREAM_REPO` private one in case you want to grab all files including private files.
- If you added private files while deploying and you have added private `UPSTREAM_REPO` and your private files in this
  private repository, so your private files will be overwritten from this repository. Also if you are using database for
  private files, then all files from database will override the private files that added before deploying or from
  private `UPSTREAM_REPO`.
- If you filled `UPSTREAM_REPO` with the official repository link, then be careful in case any change in
  requirements.txt your bot will not start after restart. In this case you need to deploy again with updated code to
  install the new requirements or simply by changing the `UPSTREAM_REPO` to your fork link with that old updates.
- In case you filled `UPSTREAM_REPO` with your fork link be careful also if you fetched the commits from the
  official repository.
- The changes in your `UPSTREAM_REPO` will take effect only after restart.

------

</details>

<details>
  <summary><h5>Bittorrent Seed</h5></summary>

- Using `-d` argument alone will lead to use global options for aria2c or qbittorrent.

<details>
  <summary><h3>QBittorrent</h3></summary>

- Global options: `GlobalMaxRatio` and `GlobalMaxSeedingMinutes` in qbittorrent.conf, `-1` means no limit, but you can
  cancel manually.
    - **NOTE**: Don't change `MaxRatioAction`.

</details>

<details>
  <summary><h3>Aria2c</h3></summary>

- Global options: `--seed-ratio` (0 means no limit) in aria-nox.sh.

------

</details>
</details>

<details>
  <summary><h5>Create Database</h5></summary>

1. Provision a PostgreSQL server (a managed one or a container). Any recent
   version works; the schema uses `jsonb`, `bytea` and ordinary constraints.
2. Create a database and a user, e.g. with a throwaway container:

   ```
   docker run -d --name mltb-pg \
     -e POSTGRES_USER=mltb -e POSTGRES_PASSWORD=mltb -e POSTGRES_DB=mltb \
     -p 5432:5432 postgres:16
   ```

3. Set `DATABASE_URL` to the connection string, e.g.
   `postgresql://mltb:mltb@localhost:5432/mltb`. Leave `DATABASE_NAME` empty
   when the URL already names the database. Tables are created automatically on
   the bot's first boot; nothing needs to be initialised by hand.

------

</details>

<details>
  <summary><h5>Migrating from a Mongo Installation</h5></summary>

A one-shot script reads the old Mongo layout and writes it into PostgreSQL:

```
pip install pymongo                      # migration-time only
python tools/migrate_mongo_to_pg.py \
    --mongo-uri "$MONGO_URL" --mongo-db mltb \
    --pg-url "$DATABASE_URL"
```

Run the bot once against an empty PostgreSQL first so the schema exists, then
migrate, then start the bot for real. Settings, users, RSS feeds, unfinished
tasks, copy records and stored private files are all carried over. The script
is idempotent (every write is an upsert), so a re-run after an interruption
only fills the gaps.

------

</details>

<details>
  <summary><h5>Yt-dlp and Aria2c Authentication Using .netrc File</h5></summary>

For using your premium accounts in yt-dlp or for protected Index Links, create .netrc file according to following
format:

**Note**: Create .netrc and not netrc, this file will be hidden, so view hidden files to edit it after creation.

Format:

```
machine host login username password my_password
```

Using Aria2c you can also use built in feature from bot with or without username. Here example for index link without
username.

```
machine example.workers.dev password index_password
```
Where host is the name of extractor (eg. instagram, Twitch). Multiple accounts of different hosts can be added each
separated by a new line.

**Yt-dlp**: 
Authentication using [cookies.txt](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies) file. CREATE IT IN INCOGNITO TAB.


-----

</details>
</details>


# All Thanks To Our Contributors

<a href="https://github.com/anasty17/mirror-leech-telegram-bot/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=anasty17/mirror-leech-telegram-bot" />
</a>

# Donations

<p> If you feel like showing your appreciation for this project, then how about buying me a coffee.</p>

[!["Buy Me A Coffee"](https://storage.ko-fi.com/cdn/kofi2.png)](https://ko-fi.com/anasty17)

Binance ID:

```
52187862
```

USDT Address:

```
TEzjjfkxLKQqndpsdpkA7jgiX7QQCL5p4f
```

Network:

```
TRC20
```
TRX Address:

```
TEzjjfkxLKQqndpsdpkA7jgiX7QQCL5p4f
```

Network:

```
TRC20
```

BTC Address:

```
17dkvxjqdc3yiaTs6dpjUB1TjV3tD7ScWe
```

ETH Address:

```
0xf798a8a1c72d593e16d8f3bb619ebd1a093c7309
```

-----
