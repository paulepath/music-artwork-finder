"""LAST-RESORT Google Images scrape via headless Chromium (Playwright).

Only ever invoked for a group the user has explicitly opted in (``google_enabled``).
Never produces anything above ``Tier.review``; results must be picked by a human in
the UI. Downloading + perceptual-hash clustering happens in ``consensus.py``.

Google markup changes regularly; this is intentionally isolated so a breakage here
does not affect the trusted-source pipeline.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
from urllib.parse import quote_plus

from ..matching import GroupMeta, ReleaseMeta
from .base import CandidateData

# matches ["http(s)://....(jpg|jpeg|png|webp)",<h>,<w>] blobs in the page scripts
_IMG_RE = re.compile(
    r'\["(https?://[^"]+?\.(?:jpg|jpeg|png|webp))",(\d{2,4}),(\d{2,4})\]',
    re.IGNORECASE,
)
_BAD_HOSTS = ("gstatic.com", "google.com", "googleusercontent.com/a/")


def _build_query(group: GroupMeta) -> str:
    artist = "" if group.album_artist in ("", "?") else group.album_artist
    return f'{artist} {group.album} album cover art'.strip()


class GoogleImagesSource:
    name = "google"

    def __init__(self, max_results: int = 18, headless: bool = True):
        self.max_results = max_results
        self.headless = headless

    async def find(self, client, group: GroupMeta) -> list[CandidateData]:  # noqa: ARG002
        query = _build_query(group)
        html = await self._fetch_html(query)
        if not html:
            return []

        seen: set[str] = set()
        out: list[CandidateData] = []
        for m in _IMG_RE.finditer(html):
            url, h, w = m.group(1), int(m.group(2)), int(m.group(3))
            if url in seen or any(b in url for b in _BAD_HOSTS):
                continue
            if min(h, w) < 200:
                continue
            seen.add(url)
            out.append(CandidateData(
                source="google",
                image_url=url,
                provenance_url=f"https://www.google.com/search?tbm=isch&q={quote_plus(query)}",
                width=w, height=h,
                release=ReleaseMeta(source="google", title=group.album,
                                    artist=group.album_artist),
                extra={"query": query},
            ))
            if len(out) >= self.max_results:
                break
        return out

    async def _fetch_html(self, query: str) -> str | None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:  # pragma: no cover
            return None

        url = f"https://www.google.com/search?tbm=isch&hl=en&q={quote_plus(query)}"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            try:
                ctx = await browser.new_context(
                    locale="en-US",
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 2000},
                )
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await self._dismiss_consent(page)
                for _ in range(4):
                    await page.mouse.wheel(0, 6000)
                    await asyncio.sleep(0.8)
                return await page.content()
            except Exception:  # noqa: BLE001
                return None
            finally:
                await browser.close()

    @staticmethod
    async def _dismiss_consent(page) -> None:
        for sel in (
            'button:has-text("Accept all")',
            'button:has-text("Reject all")',
            'button:has-text("I agree")',
            '#L2AGLb',
        ):
            with contextlib.suppress(Exception):
                btn = page.locator(sel)
                if await btn.count():
                    await btn.first.click(timeout=2000)
                    await asyncio.sleep(0.5)
                    return
