"""Bounded, non-rendering transport for OfferAgents' public feeds."""
import asyncio
import httpx

ALLOWED = frozenset({
    "https://www.amazon.com.br/deals?ref_=nav_cs_gb",
    "https://www.amazon.com.br/blackfriday?ref_=nav_cs_gb",
    "https://lista.mercadolivre.com.br/ofertas",
})
MAX_BYTES = 4_000_000
_limit = asyncio.Semaphore(4)

async def fetch_feed(url: str) -> dict:
    if url not in ALLOWED:
        raise ValueError("unsupported feed")
    async with _limit, httpx.AsyncClient(follow_redirects=False, timeout=30, trust_env=False) as client:
        async with client.stream("GET", url, headers={"accept": "text/html", "accept-language": "pt-BR,pt;q=0.9", "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"}) as response:
            status = response.status_code
            location = response.headers.get("location")
            content_type = response.headers.get("content-type", "")
            if status != 200:
                return {"status": status, "location": location}
            if "text/html" not in content_type.lower():
                return {"status": status, "contentType": content_type}
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("response too large")
                chunks.append(chunk)
            return {"status": status, "contentType": content_type, "html": b"".join(chunks).decode("utf-8", errors="replace")}
