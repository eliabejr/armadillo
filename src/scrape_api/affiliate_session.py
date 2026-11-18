"""Narrow authenticated-browser feed; run as the profile owner, never as scrape-api."""
from __future__ import annotations

import asyncio
import hmac
import os
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import async_playwright

FEED = "https://lista.mercadolivre.com.br/informatica/_Container_affiliates-mlb-ganhos-extras_SALE%2AFORMAT_1359391_promotion%2Atype_lightning"
PROFILE = os.environ.get("OFFERAGENTS_PROFILE", "/data/home/.offeragents-mercadolivre-browser")
KEYS = tuple(k.strip() for k in os.environ.get("SCRAPE_API_KEYS", "").split(",") if k.strip())
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
lock = asyncio.Lock()

EXTRACT = r"""els => els.slice(0,80).map(el => {
 const a=el.querySelector('a.poly-component__title'), p=el.querySelector('.poly-component__price');
 const money=x=>{const f=x?.querySelector('.andes-money-amount__fraction')?.textContent?.replace(/\./g,''), c=x?.querySelector('.andes-money-amount__cents')?.textContent||'00'; return /^\d+$/.test(f||'') && /^\d{2}$/.test(c) ? `${f}.${c}` : null;};
 return {title:a?.innerText?.trim().slice(0,300),url:a?.href?.split('?')[0].slice(0,2000),price:money(p?.querySelector('.poly-price__current .andes-money-amount')),originalPrice:money(p?.querySelector('.andes-money-amount--previous')),priceType:/\b(?:no|via|com)\s+pix\b/i.test(p?.innerText||'')?'pix':'regular',imageUrl:el.querySelector('img.poly-component__picture')?.src?.split('?')[0].slice(0,2000)};
})"""


@app.post('/feed')
async def feed(authorization: str = Header(default="")):
    token = authorization.removeprefix('Bearer ').strip() if authorization.startswith('Bearer ') else ''
    if not KEYS or not any(hmac.compare_digest(token, k) for k in KEYS):
        raise HTTPException(status_code=401, detail='unauthorized')
    if lock.locked():
        raise HTTPException(status_code=429, detail='busy')
    async with lock:
        try:
            async with async_playwright() as p:
                context = await p.chromium.launch_persistent_context(PROFILE, executable_path='/usr/bin/chromium', headless=False, chromium_sandbox=True, locale='pt-BR')
                try:
                    async def allowed_request(route):
                        parsed = urlsplit(route.request.url)
                        host = (parsed.hostname or '').lower()
                        domains = ('mercadolivre.com.br', 'mercadolibre.com', 'mlstatic.com')
                        allowed = parsed.scheme == 'https' and not parsed.username and not parsed.password and parsed.port in (None, 443) and any(host == d or host.endswith('.' + d) for d in domains)
                        if allowed:
                            await route.continue_()
                        else:
                            await route.abort()
                    await context.route('**/*', allowed_request)
                    page = context.pages[0] if context.pages else await context.new_page()
                    robots = await page.goto('https://lista.mercadolivre.com.br/robots.txt', wait_until='domcontentloaded', timeout=15000)
                    if not robots or robots.status != 200 or page.url != 'https://lista.mercadolivre.com.br/robots.txt':
                        raise HTTPException(status_code=409, detail='robots_unavailable')
                    rules = await robots.text()
                    if len(rules) > 128_000:
                        raise HTTPException(status_code=409, detail='robots_unavailable')
                    parser = RobotFileParser()
                    parser.parse(rules.splitlines())
                    if not parser.can_fetch('*', FEED):
                        raise HTTPException(status_code=409, detail='robots_disallowed')
                    response = await page.goto(FEED, wait_until='domcontentloaded', timeout=30000)
                    await page.wait_for_timeout(3000)
                    final = urlsplit(page.url)
                    if final.hostname != 'lista.mercadolivre.com.br' or final.path != urlsplit(FEED).path:
                        raise HTTPException(status_code=409, detail='session_verification_required')
                    if not response or response.status != 200:
                        raise HTTPException(status_code=502, detail='source_unavailable')
                    items = await page.locator('.poly-card').evaluate_all(EXTRACT)
                    if not items:
                        raise HTTPException(status_code=502, detail='empty_feed')
                    return {'items': items}
                finally:
                    await context.close()
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=503, detail='session_unavailable') from None
