# OfferAgents authenticated Mercado Livre feed

The opt-in session feed reads a fixed public affiliate-products list in the
Computing category. It does not expose a browser, arbitrary navigation, cookies,
account data or raw page HTML. Its browser profile is local to the VPS and must
already be signed in by its owner. Verification challenges stop collection and
require the owner to resolve them; they are never solved by the service.

`affiliate_session.py` checks the source's current robots policy using the
authenticated browser before opening the fixed list. Chromium runs with its
sandbox enabled, in a private Xvfb display; the example unit limits filesystem
and network access. The sidecar listens only on a Unix socket and requires one of
the existing scrape API keys. The public route is also protected by API-key
middleware. Prices and product URLs are validated again by OfferAgents.

For this installation, copy the package files to `/opt/offeragents-ml/src`,
provide a root-owned virtual environment at `/opt/offeragents-ml/venv`, and
install `deploy/offeragents-ml.service.example` as a systemd unit. Adapt the
user and profile path for another machine. Do not expose the Unix socket to the
network. Set `MERCADOLIVRE_SESSION_FALLBACK_ENABLED=true` in the Trigger
production environment only after verifying both services. The collector will
try again at the next scheduled run after temporary unavailability; a paused
VPS does not wake itself and a CAPTCHA still requires human action.
