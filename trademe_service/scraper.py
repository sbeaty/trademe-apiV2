
import asyncio, json, re, textwrap
from datetime import datetime
from typing import List

from playwright.async_api import async_playwright, Response, TimeoutError

SEARCH_PAGE = (
    "https://www.trademe.co.nz/a/property/residential/sale/auckland/waitakere-city"
)
SEARCH_API = "https://api.trademe.co.nz/v1/search/property/residential.json"

BODY_SEL = (
    "body > tm-root > div:nth-child(1) > main > div > "
    "tm-property-listing > div > div:nth-child(4) > tg-row:nth-child(1) > "
    "tg-col > tm-property-listing-body"
)

DATE_RE = re.compile(r"/Date\((\d+)\)/")
FULL_RE = re.compile(r"/photoserver/[^/]+/")


def _parse_date(ds: str) -> datetime:
    m = DATE_RE.search(ds or "")
    return datetime.fromtimestamp(int(m.group(1)) / 1000) if m else datetime.min


def _thumb_to_full(url: str) -> str:
    return FULL_RE.sub("/photoserver/full/", url)


def _money(txt: str) -> str:
    m = re.search(r"\$\s?[0-9][\d,]*(?:\s?[MK]?)?", txt)
    return m.group(0) if m else ""


def _line(lines: List[str], kw: str) -> str:
    kw = kw.lower()
    for ln in lines:
        if kw in ln.lower():
            return ln.strip()
    return ""


async def _child_blocks(page):
    return await page.evaluate(
        f"""
        () => {{
          const b=document.querySelector({BODY_SEL!r});
          return b?[...b.children].map((e,i)=>({{
            idx:i,text:(e.innerText||"").trim().slice(0,800)
          }})):[];
        }}
    """
    )


# ────────────────────────────────────────────────────────────────────
async def _enrich(page, listing):
    url = listing.get("ListingUrl") or f"https://www.trademe.co.nz/a/property/listing/{listing['ListingId']}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=5_000)
        await page.wait_for_selector(BODY_SEL, timeout=5_000)
        await page.wait_for_timeout(1200)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1200)
        await page.evaluate("window.scrollTo(0, 0)")

        blocks = await _child_blocks(page)
        summary = blocks[0]["text"] if blocks else ""
        lines = summary.splitlines()

        listing["address"] = lines[0] if lines else ""
        listing["price_line"] = _line(lines, "$") or _money(summary)

        badges = await page.locator(
            "ul.tm-property-details-summary-attribute-icons__features "
            "span.tm-property-details-summary-attribute-icons__metric-value"
        ).all_inner_texts()
        nums = [re.search(r"\d+", b).group(0) if re.search(r"\d+", b) else "" for b in badges]
        nums += ["", "", ""]
        listing["beds"], listing["baths"], listing["parks"] = nums[:3]

        # insights
        if len(blocks) > 1:
            est_lines = blocks[1]["text"].splitlines()
            he_parts = [_money(x) for x in est_lines[0].split("–")] if est_lines else []
            listing["homes_estimate"] = " – ".join(he_parts) if he_parts else ""
            listing["homes_updated"] = _line(est_lines, "Updated")
            listing["rent_estimate"] = _line(est_lines, "/ week")
            listing["rent_updated"] = _line(est_lines, "Updated")
            listing["rent_yield"] = _line(est_lines, "%")
        else:
            for k in (
                "homes_estimate",
                "homes_updated",
                "rent_estimate",
                "rent_updated",
                "rent_yield",
            ):
                listing[k] = ""

        # CV
        if len(blocks) > 2 and "Capital Value" in blocks[2]["text"]:
            listing["capital_value"] = _money(" ".join(blocks[2]["text"].splitlines()[1:]))
        else:
            listing["capital_value"] = ""

        # description
        if await page.locator("tm-property-listing-description tm-markdown").count():
            listing["description"] = (
                await page.locator("tm-property-listing-description tm-markdown").inner_text()
            ).strip()
        else:
            fb = blocks[3]["text"] if len(blocks) > 3 else "\n".join(b["text"] for b in blocks)
            listing["description"] = "\n".join(textwrap.wrap(fb, 120)[:30])

    except Exception as e:
        print(f"⚠️  {url} — {type(e).__name__}: {e}")
        for k in (
            "address",
            "price_line",
            "beds",
            "baths",
            "parks",
            "homes_estimate",
            "homes_updated",
            "rent_estimate",
            "rent_updated",
            "rent_yield",
            "capital_value",
            "description",
        ):
            listing.setdefault(k, "")


# ────────────────────────────────────────────────────────────────────
async def run_scrape(pages: int = 1) -> list[dict]:
    """
    Return listings from the specified number of pages of residential listings for Waitakere City
    enriched with extra fields. `pages` must be between 1 and 10.
    """
    if not 1 <= pages <= 10:
        raise ValueError("pages must be between 1 and 10")

    all_listings = []
    
    # fetch search JSON for each page
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        for page_num in range(1, pages + 1):
            page = await browser.new_page()
            fut = asyncio.get_event_loop().create_future()
            
            # Navigate to the specific page in pagination
            page_url = f"{SEARCH_PAGE}?page={page_num}"
            
            page.on(
                "response",
                lambda r: fut.set_result(r)
                if r.url.startswith(SEARCH_API) and r.request.method == "GET" and not fut.done()
                else None,
            )
            await page.goto(page_url, wait_until="domcontentloaded")
            resp: Response = await asyncio.wait_for(fut, timeout=60_000)
            data = await resp.json()
            await page.close()
            
            cards = data.get("List", [])
            cards.sort(key=lambda c: _parse_date(c.get("StartDate")), reverse=True)
            all_listings.extend(cards)
        
        await browser.close()

    # scrape detail pages
    sem = asyncio.Semaphore(5)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def worker(card):
            async with sem:
                pg = await browser.new_page()
                pg.set_default_timeout(5_000)
                pg.set_default_navigation_timeout(5_000)
                await _enrich(pg, card)
                await pg.close()

        await asyncio.gather(*(worker(c) for c in all_listings))
        await browser.close()

    # convert thumbnails and prune unwanted keys
    for l in all_listings:
        l["image_urls"] = [_thumb_to_full(u) for u in l.get("PhotoUrls", [])]
        l.pop("PhotoUrls", None)
        l.pop("Agency", None)

    return all_listings
# 