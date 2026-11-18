import asyncio
import httpx
from scrape_api import affiliate_session


def test_sidecar_requires_key_and_only_exposes_fixed_route(monkeypatch):
    monkeypatch.setattr(affiliate_session, 'KEYS', ('local-test-key',))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=affiliate_session.app), base_url='http://local') as client:
            assert (await client.post('/feed')).status_code == 401
            assert (await client.post('/feed', headers={'Authorization':'Bearer wrong'})).status_code == 401
            assert (await client.get('/feed', headers={'Authorization':'Bearer local-test-key'})).status_code == 405
            assert (await client.post('/other', headers={'Authorization':'Bearer local-test-key'})).status_code == 404
    asyncio.run(run())
