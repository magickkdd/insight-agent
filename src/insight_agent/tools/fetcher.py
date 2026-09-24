"""网页抓取器：URL → 清洗正文。永不抛异常，失败进 status（规格 §1.2）。

降级链：抓取失败 → 重试 1 次（换 UA）→ 仍败标记 failed_*。
正文 <200 字视为反爬空壳页，判失败。
"""

import hashlib
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura

SourceStatus = Literal[
    "fetched", "failed_403", "failed_timeout",
    "failed_paywall", "failed_decode", "degraded_snippet",
]

_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

_MIN_BODY_CHARS = 200
_robots_cache: dict[str, RobotFileParser | None] = {}


def _domain(url: str) -> str:
    return urlparse(url).netloc


def _robots_allows(url: str) -> bool:
    """尊重 robots.txt。robots 本身 403/异常/不可达 → 默认放行（无法得知即不假设禁止）。"""
    domain = _domain(url)
    if domain in _robots_cache:
        rp = _robots_cache[domain]
    else:
        rp = None
        try:
            resp = httpx.get(f"https://{domain}/robots.txt", timeout=5.0, follow_redirects=True)
            if resp.status_code == 200 and resp.text.strip():
                rp = RobotFileParser()
                rp.parse(resp.text.splitlines())
            # 404/403/空 → 视为无 robots，放行
        except Exception:  # noqa: BLE001 - robots 不可达默认放行
            rp = None
        _robots_cache[domain] = rp
    return rp is None or rp.can_fetch("*", url)


@dataclass
class SourceDoc:
    url: str
    title: str
    status: SourceStatus
    text: str | None
    fetched_at: str


def fetch_readable(url: str, *, timeout: float = 15.0, respect_robots: bool = True) -> SourceDoc:
    """抓取并抽取正文。任何失败都返回带 status 的 SourceDoc，不抛异常。"""
    now = time.strftime("%Y-%m-%d %H:%M")
    if respect_robots and not _robots_allows(url):
        return SourceDoc(url, "", "failed_403", None, now)

    last_status: SourceStatus = "failed_timeout"
    for ua in _UAS:  # 重试 1 次，换 UA
        try:
            resp = httpx.get(
                url, timeout=timeout, follow_redirects=True,
                headers={"User-Agent": ua}, verify=True,
            )
            if resp.status_code == 403 or resp.status_code == 401:
                last_status = "failed_403"
                continue
            if resp.status_code in (402,):
                last_status = "failed_paywall"
                continue
            resp.raise_for_status()
            html = resp.text
            text = trafilatura.extract(html, include_comments=False) or ""
            try:
                # trafilatura 2.x：位置参数 filecontent；title 抽取失败不影响正文
                meta = trafilatura.extract_metadata(html)
                title = meta.title if meta and meta.title else url
            except Exception:  # noqa: BLE001 - 标题是锦上添花
                title = url
            if len(text) < _MIN_BODY_CHARS:
                last_status = "failed_decode"  # 空壳页/JS 渲染页
                continue
            return SourceDoc(url, title, "fetched", text, now)
        except httpx.TimeoutException:
            last_status = "failed_timeout"
        except httpx.HTTPStatusError as e:
            last_status = "failed_403" if e.response.status_code in (401, 403) else "failed_decode"
        except Exception:  # noqa: BLE001 - 任何意外都归为解析失败
            last_status = "failed_decode"
    return SourceDoc(url, "", last_status, None, now)


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:10]
