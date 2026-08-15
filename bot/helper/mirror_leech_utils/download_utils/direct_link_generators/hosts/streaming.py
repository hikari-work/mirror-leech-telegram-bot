"""Streaming site handlers: doods, streamtape, filelions, streamwish, streamhub, streamvid."""

from re import findall, search
from time import sleep
from urllib.parse import urlparse

from cloudscraper import create_scraper
from lxml.etree import HTML
from requests import Session, get

from .._common import Config, DirectDownloadLinkException, user_agent
from ..registry import register

# --- DoodStream ---

_DOOD_DOMAINS = (
    "dood.watch",
    "doodstream.com",
    "dood.to",
    "dood.so",
    "dood.cx",
    "dood.la",
    "dood.ws",
    "dood.sh",
    "doodstream.co",
    "dood.pm",
    "dood.wf",
    "dood.re",
    "dood.video",
    "dooood.com",
    "dood.yt",
    "doods.yt",
    "dood.stream",
    "doods.pro",
    "ds2play.com",
    "d0o0d.com",
    "ds2video.com",
    "do0od.com",
    "d000d.com",
)


@register(*_DOOD_DOMAINS, order=32)
def doods(url):
    if "/e/" in url:
        url = url.replace("/e/", "/d/")
    parsed_url = urlparse(url)
    with create_scraper() as session:
        try:
            html = HTML(session.get(url).text)
        except Exception as e:
            raise DirectDownloadLinkException(
                f"ERROR: {e.__class__.__name__} While fetching token link"
            ) from e
        if not (link := html.xpath("//div[@class='download-content']//a/@href")):
            raise DirectDownloadLinkException(
                "ERROR: Token Link not found or maybe not allow to download! open in browser."
            )
        link = f"{parsed_url.scheme}://{parsed_url.hostname}{link[0]}"
        sleep(2)
        try:
            _res = session.get(link)
        except Exception as e:
            raise DirectDownloadLinkException(
                f"ERROR: {e.__class__.__name__} While fetching download link"
            ) from e
    if not (link := search(r"window\.open\('(\S+)'", _res.text)):
        raise DirectDownloadLinkException("ERROR: Download link not found try again")
    return (link.group(1), [f"Referer: {parsed_url.scheme}://{parsed_url.hostname}/"])


# --- Streamtape ---

_STREAMTAPE_DOMAINS = (
    "streamtape.com",
    "streamtape.cc",
    "streamtape.net",
    "streamtape.to",
    "tapecontent.net",
    "strtape.tech",
    "strcloud.in",
    "strcloud.club",
    "strcloud.link",
    "shavetape.cash",
    "streamta.pe",
    "strtpe.link",
    "streamadblocker.xyz",
    "strtape.cloud",
    "tapeadvertisement.com",
    "streamta.site",
    "streamtape.xyz",
    "watchadsontape.com",
)


@register(*_STREAMTAPE_DOMAINS, order=33)
def streamtape(url):
    gateway_base = (getattr(Config, "GATEWAY_URL", "") or "https://api.piyann.me").rstrip("/")
    api_url = f"{gateway_base}/api/v1/scrape/streamtape"
    headers = {"accept": "application/json"}
    if token := getattr(Config, "GATEWAY_TOKEN", ""):
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = get(api_url, params={"q": url}, headers=headers, timeout=60)
        result = resp.json()
    except Exception as e:
        raise DirectDownloadLinkException(f"ERROR: {e.__class__.__name__}") from e
    if not result.get("success"):
        raise DirectDownloadLinkException(
            f"ERROR: {result.get('error', 'unknown error')}"
        )
    data = result.get("data") or {}
    download_url = data.get("download_url", "")
    if not download_url.startswith("http"):
        raise DirectDownloadLinkException("ERROR: Download link not found")
    return download_url


# --- FileLions & StreamWish ---

_FILELIONS_DOMAINS = (
    "filelions.co",
    "filelions.site",
    "filelions.live",
    "filelions.to",
    "mycloudz.cc",
    "cabecabean.lol",
    "filelions.online",
    "embedwish.com",
    "kitabmarkaz.xyz",
    "wishfast.top",
    "streamwish.to",
    "kissmovies.net",
)


@register(*_FILELIONS_DOMAINS, order=38)
def filelions_and_streamwish(url):
    parsed_url = urlparse(url)
    hostname = parsed_url.hostname
    scheme = parsed_url.scheme
    if any(
        x in hostname
        for x in [
            "filelions.co",
            "filelions.live",
            "filelions.to",
            "filelions.site",
            "cabecabean.lol",
            "filelions.online",
            "mycloudz.cc",
        ]
    ):
        apiKey = Config.FILELION_API
        apiUrl = "https://vidhideapi.com"
    elif any(
        x in hostname
        for x in [
            "embedwish.com",
            "kissmovies.net",
            "kitabmarkaz.xyz",
            "wishfast.top",
            "streamwish.to",
        ]
    ):
        apiKey = Config.STREAMWISH_API
        apiUrl = "https://api.streamwish.com"
    if not apiKey:
        raise DirectDownloadLinkException(
            f"ERROR: API is not provided get it from {scheme}://{hostname}"
        )
    file_code = url.split("/")[-1]
    quality = ""
    if bool(file_code.strip().endswith(("_o", "_h", "_n", "_l"))):
        spited_file_code = file_code.rsplit("_", 1)
        quality = spited_file_code[1]
        file_code = spited_file_code[0]
    url = f"{scheme}://{hostname}/{file_code}"
    try:
        _res = get(
            f"{apiUrl}/api/file/direct_link",
            params={"key": apiKey, "file_code": file_code, "hls": "1"},
        ).json()
    except Exception as e:
        raise DirectDownloadLinkException(f"ERROR: {e.__class__.__name__}") from e
    if _res["status"] != 200:
        raise DirectDownloadLinkException(f"ERROR: {_res['msg']}")
    result = _res["result"]
    if not result["versions"]:
        raise DirectDownloadLinkException("ERROR: File Not Found")
    error = "\nProvide a quality to download the video\nAvailable Quality:"
    for version in result["versions"]:
        if quality == version["name"]:
            return version["url"]
        elif version["name"] == "l":
            error += "\nLow"
        elif version["name"] == "n":
            error += "\nNormal"
        elif version["name"] == "o":
            error += "\nOriginal"
        elif version["name"] == "h":
            error += "\nHD"
        error += f" <code>{url}_{version['name']}</code>"
    raise DirectDownloadLinkException(f"ERROR: {error}")


# --- StreamVid ---


@register("streamvid.net", order=24)
def streamvid(url: str):
    file_code = url.split("/")[-1]
    parsed_url = urlparse(url)
    url = f"{parsed_url.scheme}://{parsed_url.hostname}/d/{file_code}"
    quality_defined = bool(url.strip().endswith(("_o", "_h", "_n", "_l")))
    with create_scraper() as session:
        try:
            html = HTML(session.get(url).text)
        except Exception as e:
            raise DirectDownloadLinkException(f"ERROR: {e.__class__.__name__}") from e
        if quality_defined:
            data = {}
            if not (inputs := html.xpath('//form[@id="F1"]//input')):
                raise DirectDownloadLinkException("ERROR: No inputs found")
            for i in inputs:
                if key := i.get("name"):
                    data[key] = i.get("value")
            try:
                html = HTML(session.post(url, data=data).text)
            except Exception as e:
                raise DirectDownloadLinkException(
                    f"ERROR: {e.__class__.__name__}"
                ) from e
            if not (
                script := html.xpath(
                    '//script[contains(text(),"document.location.href")]/text()'
                )
            ):
                if error := html.xpath(
                    '//div[@class="alert alert-danger"][1]/text()[2]'
                ):
                    raise DirectDownloadLinkException(f"ERROR: {error[0]}")
                raise DirectDownloadLinkException(
                    "ERROR: direct link script not found!"
                )
            if directLink := findall(r'document\.location\.href="(.*)"', script[0]):
                return directLink[0]
            raise DirectDownloadLinkException(
                "ERROR: direct link not found! in the script"
            )
        elif (qualities_urls := html.xpath('//div[@id="dl_versions"]/a/@href')) and (
            qualities := html.xpath('//div[@id="dl_versions"]/a/text()[2]')
        ):
            error = "\nProvide a quality to download the video\nAvailable Quality:"
            for quality_url, quality in zip(qualities_urls, qualities):
                error += f"\n{quality.strip()} <code>{quality_url}</code>"
            raise DirectDownloadLinkException(f"ERROR: {error}")
        elif error := html.xpath('//div[@class="not-found-text"]/text()'):
            raise DirectDownloadLinkException(f"ERROR: {error[0]}")
        raise DirectDownloadLinkException("ERROR: Something went wrong")


# --- StreamHub ---


@register("streamhub.ink", "streamhub.to", order=39)
def streamhub(url):
    file_code = url.split("/")[-1]
    parsed_url = urlparse(url)
    url = f"{parsed_url.scheme}://{parsed_url.hostname}/d/{file_code}"
    with create_scraper() as session:
        try:
            html = HTML(session.get(url).text)
        except Exception as e:
            raise DirectDownloadLinkException(f"ERROR: {e.__class__.__name__}") from e
        if not (inputs := html.xpath('//form[@name="F1"]//input')):
            raise DirectDownloadLinkException("ERROR: No inputs found")
        data = {}
        for i in inputs:
            if key := i.get("name"):
                data[key] = i.get("value")
        session.headers.update({"referer": url})
        sleep(1)
        try:
            html = HTML(session.post(url, data=data).text)
        except Exception as e:
            raise DirectDownloadLinkException(f"ERROR: {e.__class__.__name__}") from e
        if directLink := html.xpath(
            '//a[@class="btn btn-primary btn-go downloadbtn"]/@href'
        ):
            return directLink[0]
        if error := html.xpath('//div[@class="alert alert-danger"]/text()[2]'):
            raise DirectDownloadLinkException(f"ERROR: {error[0]}")
        raise DirectDownloadLinkException("ERROR: direct link not found!")


# --- mp4upload ---


@register("mp4upload.com", order=28)
def mp4upload(url):
    with Session() as session:
        try:
            url = url.replace("embed-", "")
            req = session.get(url).text
            tree = HTML(req)
            inputs = tree.xpath("//input")
            header = ["Referer: https://www.mp4upload.com/"]
            data = {input.get("name"): input.get("value") for input in inputs}
            if not data:
                raise DirectDownloadLinkException("ERROR: File Not Found!")
            post = session.post(
                url,
                data=data,
                headers={
                    "User-Agent": user_agent,
                    "Referer": "https://www.mp4upload.com/",
                },
            ).text
            tree = HTML(post)
            inputs = tree.xpath('//form[@name="F1"]//input')
            data = {
                input.get("name"): input.get("value").replace(" ", "")
                for input in inputs
            }
            if not data:
                raise DirectDownloadLinkException("ERROR: File Not Found!")
            data["referer"] = url
            direct_link = session.post(url, data=data).url
            return direct_link, header
        except Exception:
            raise DirectDownloadLinkException("ERROR: File Not Found!")
