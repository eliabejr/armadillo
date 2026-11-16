import httpx
import pytest
import asyncio
from scrape_api import offeragents

def test_feed_does_not_follow_redirects_or_fetch_arbitrary_urls(monkeypatch):
    requested = []
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://example.org/blocked"})
    original = httpx.AsyncClient
    monkeypatch.setattr(offeragents.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    url = "https://lista.mercadolivre.com.br/ofertas"
    assert asyncio.run(offeragents.fetch_feed(url)) == {"status": 302, "location": "https://example.org/blocked"}
    assert requested == [url]
    with pytest.raises(ValueError):
        asyncio.run(offeragents.fetch_feed("http://127.0.0.1/"))
