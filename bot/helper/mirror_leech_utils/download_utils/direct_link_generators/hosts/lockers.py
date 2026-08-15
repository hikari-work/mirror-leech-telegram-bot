"""One-off file hosts that each need their own scrape dance: 1fichier,
buzzheavier, devuploads, uploadhaven, easyupload.

Split out of ``cloud.py`` to keep both files inside the size target; these
are download lockers rather than cloud storage.
"""

from re import match, search
from time import sleep

from cloudscraper import create_scraper
from lxml.etree import HTML
from requests import Session, get, post

from .._common import (
    PASSWORD_ERROR_MESSAGE,
    DirectDownloadLinkException,
    get_captcha_token,
    speed_string_to_bytes,
)
from ..registry import register


@register("1fichier.com", order=16)
def fichier(link):
    """1Fichier direct link generator
    Based on https://github.com/Maujar
    """
    regex = r"^([http:\/\/|https:\/\/]+)?.*1fichier\.com\/\?.+"
    gan = match(regex, link)
    if not gan:
        raise DirectDownloadLinkException("ERROR: The link you entered is wrong!")
    if "::" in link:
        pswd = link.split("::")[-1]
        url = link.split("::")[-2]
    else:
        pswd = None
        url = link
    cget = create_scraper().request
    try:
        if pswd is None:
            req = cget("post", url)
        else:
            pw = {"pass": pswd}
            req = cget("post", url, data=pw)
    except Exception as e:
        raise DirectDownloadLinkException(f"ERROR: {e.__class__.__name__}") from e
    if req.status_code == 404:
        raise DirectDownloadLinkException(
            "ERROR: File not found/The link you entered is wrong!"
        )
    html = HTML(req.text)
    if dl_url := html.xpath('//a[@class="ok btn-general btn-orange"]/@href'):
        return dl_url[0]
    if not (ct_warn := html.xpath('//div[@class="ct_warn"]')):
        raise DirectDownloadLinkException(
            "ERROR: Error trying to generate Direct Link from 1fichier!"
        )
    if len(ct_warn) == 3:
        str_2 = ct_warn[-1].text
        if "you must wait" in str_2.lower():
            if numbers := [int(word) for word in str_2.split() if word.isdigit()]:
                raise DirectDownloadLinkException(
                    f"ERROR: 1fichier is on a limit. Please wait {numbers[0]} minute."
                )
            else:
                raise DirectDownloadLinkException(
                    "ERROR: 1fichier is on a limit. Please wait a few minutes/hour."
                )
        elif "protect access" in str_2.lower():
            raise DirectDownloadLinkException(
                f"ERROR:\n{PASSWORD_ERROR_MESSAGE.format(link)}"
            )
        else:
            raise DirectDownloadLinkException(
                "ERROR: Failed to generate Direct Link from 1fichier!"
            )
    elif len(ct_warn) == 4:
        str_1 = ct_warn[-2].text
        str_3 = ct_warn[-1].text
        if "you must wait" in str_1.lower():
            if numbers := [int(word) for word in str_1.split() if word.isdigit()]:
                raise DirectDownloadLinkException(
                    f"ERROR: 1fichier is on a limit. Please wait {numbers[0]} minute."
                )
            else:
                raise DirectDownloadLinkException(
                    "ERROR: 1fichier is on a limit. Please wait a few minutes/hour."
                )
        elif "bad password" in str_3.lower():
            raise DirectDownloadLinkException(
                "ERROR: The password you entered is wrong!"
            )
    raise DirectDownloadLinkException(
        "ERROR: Error trying to generate Direct Link from 1fichier!"
    )


@register("buzzheavier.com", order=2)
def buzzheavier(url):
    """
    Generate a direct download link for buzzheavier URLs.
    @param link: URL from buzzheavier
    @return: Direct download link
    """
    pattern = r"^https?://buzzheavier\.com/[a-zA-Z0-9]+$"
    if not match(pattern, url):
        return url

    def _bhscraper(url, folder=False):
        session = Session()
        if "/download" not in url:
            url += "/download"
        url = url.strip()
        session.headers.update(
            {
                "referer": url.split("/download")[0],
                "hx-current-url": url.split("/download")[0],
                "hx-request": "true",
                "priority": "u=1, i",
            }
        )
        try:
            response = session.get(url)
            d_url = response.headers.get("Hx-Redirect")
            if not d_url:
                if not folder:
                    raise DirectDownloadLinkException("ERROR: Gagal mendapatkan data")
                return
            return d_url
        except Exception as e:
            raise DirectDownloadLinkException(f"ERROR: {str(e)}") from e

    with Session() as session:
        tree = HTML(session.get(url).text)
        if link := tree.xpath(
            "//a[contains(@class, 'link-button') and contains(@class, 'gay-button')]/@hx-get"
        ):
            return _bhscraper(f"https://buzzheavier.com{link[0]}")
        elif folders := tree.xpath("//tbody[@id='tbody']/tr"):
            details = {"contents": [], "title": "", "total_size": 0}
            for data in folders:
                try:
                    filename = data.xpath(".//a")[0].text.strip()
                    _id = data.xpath(".//a")[0].attrib.get("href", "").strip()
                    size = data.xpath(".//td[@class='text-center']/text()")[0].strip()
                    url = _bhscraper(f"https://buzzheavier.com{_id}", True)
                    item = {
                        "path": "",
                        "filename": filename,
                        "url": url,
                    }
                    details["contents"].append(item)
                    size = speed_string_to_bytes(size)
                    details["total_size"] += size
                except Exception:
                    continue
            details["title"] = tree.xpath("//span/text()")[0].strip()
            return details
        else:
            raise DirectDownloadLinkException("ERROR: No download link found")


@register("devuploads", order=3)
def devuploads(url):
    """
    Generate a direct download link for devuploads.com URLs.
    @param url: URL from devuploads.com
    @return: Direct download link
    """
    with Session() as session:
        res = session.get(url)
        html = HTML(res.text)
        if not html.xpath("//input[@name]"):
            raise DirectDownloadLinkException("ERROR: Unable to find link data")
        data = {i.get("name"): i.get("value") for i in html.xpath("//input[@name]")}
        res = session.post("https://gujjukhabar.in/", data=data)
        html = HTML(res.text)
        if not html.xpath("//input[@name]"):
            raise DirectDownloadLinkException("ERROR: Unable to find link data")
        data = {i.get("name"): i.get("value") for i in html.xpath("//input[@name]")}
        resp = session.get(
            "https://du2.devuploads.com/dlhash.php",
            headers={
                "Origin": "https://gujjukhabar.in",
                "Referer": "https://gujjukhabar.in/",
            },
        )
        if not resp.text:
            raise DirectDownloadLinkException("ERROR: Unable to find ipp value")
        data["ipp"] = resp.text.strip()
        if not data.get("rand"):
            raise DirectDownloadLinkException("ERROR: Unable to find rand value")
        randpost = session.post(
            "https://devuploads.com/token/token.php",
            data={"rand": data["rand"], "msg": ""},
            headers={
                "Origin": "https://gujjukhabar.in",
                "Referer": "https://gujjukhabar.in/",
            },
        )
        if not randpost:
            raise DirectDownloadLinkException("ERROR: Unable to find xd value")
        data["xd"] = randpost.text.strip()
        res = session.post(url, data=data)
        html = HTML(res.text)
        if not html.xpath("//input[@name='orilink']/@value"):
            raise DirectDownloadLinkException("ERROR: Unable to find Direct Link")
        direct_link = html.xpath("//input[@name='orilink']/@value")
        return direct_link[0]


@register("uploadhaven", order=5)
def uploadhaven(url):
    """
    Generate a direct download link for uploadhaven.com URLs.
    @param url: URL from uploadhaven.com
    @return: Direct download link
    """
    try:
        res = get(url, headers={"Referer": "http://steamunlocked.net/"})
        html = HTML(res.text)
        if not html.xpath('//form[@method="POST"]//input'):
            raise DirectDownloadLinkException("ERROR: Unable to find link data")
        data = {
            i.get("name"): i.get("value")
            for i in html.xpath('//form[@method="POST"]//input')
        }
        sleep(15)
        res = post(url, data=data, headers={"Referer": url}, cookies=res.cookies)
        html = HTML(res.text)
        if not html.xpath('//div[@class="alert alert-success mb-0"]//a'):
            raise DirectDownloadLinkException("ERROR: Unable to find link data")
        a = html.xpath('//div[@class="alert alert-success mb-0"]//a')[0]
        return a.get("href")
    except Exception as e:
        raise DirectDownloadLinkException(f"ERROR: {str(e)}") from e


@register("easyupload.io", order=23)
def easyupload(url):
    if "::" in url:
        _password = url.split("::")[-1]
        url = url.split("::")[-2]
    else:
        _password = ""
    file_id = url.split("/")[-1]
    with create_scraper() as session:
        try:
            _res = session.get(url)
        except Exception as e:
            raise DirectDownloadLinkException(f"ERROR: {e.__class__.__name__}")
        first_page_html = HTML(_res.text)
        if (
            first_page_html.xpath("//h6[contains(text(),'Password Protected')]")
            and not _password
        ):
            raise DirectDownloadLinkException(
                f"ERROR:\n{PASSWORD_ERROR_MESSAGE.format(url)}"
            )
        if not (
            match := search(
                r"https://eu(?:[1-9][0-9]?|100)\.easyupload\.io/action\.php", _res.text
            )
        ):
            raise DirectDownloadLinkException(
                "ERROR: Failed to get server for EasyUpload Link"
            )
        action_url = match.group()
        session.headers.update({"referer": "https://easyupload.io/"})
        recaptcha_params = {
            "k": "6LfWajMdAAAAAGLXz_nxz2tHnuqa-abQqC97DIZ3",
            "ar": "1",
            "co": "aHR0cHM6Ly9lYXN5dXBsb2FkLmlvOjQ0Mw..",
            "hl": "en",
            "v": "0hCdE87LyjzAkFO5Ff-v7Hj1",
            "size": "invisible",
            "cb": "c3o1vbaxbmwe",
        }
        if not (captcha_token := get_captcha_token(session, recaptcha_params)):
            raise DirectDownloadLinkException("ERROR: Captcha token not found")
        try:
            data = {
                "type": "download-token",
                "url": file_id,
                "value": _password,
                "captchatoken": captcha_token,
                "method": "regular",
            }
            json_resp = session.post(url=action_url, data=data).json()
        except Exception as e:
            raise DirectDownloadLinkException(f"ERROR: {e.__class__.__name__}") from e
    if "download_link" in json_resp:
        return json_resp["download_link"]
    elif "data" in json_resp:
        raise DirectDownloadLinkException(
            f"ERROR: Failed to generate direct link due to {json_resp['data']}"
        )
    raise DirectDownloadLinkException(
        "ERROR: Failed to generate direct link from EasyUpload."
    )
