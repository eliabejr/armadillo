# armadillo

[English](README.md) · [Português](README.pt-BR.md) · **Español**

API de scraping self-hosted, con la misma API que Firecrawl, y servidor MCP para
agentes Claude. Funciona sobre [Crawl4AI](https://github.com/unclecode/crawl4ai)
(Chromium vía Playwright) y cabe en un solo servicio systemd.

En el servidor y en el código el nombre es `scrape-api` (servicio systemd,
`/etc/scrape-api`, paquete `scrape_api`).

- **REST con el formato de Firecrawl**: `/v2/scrape`, `/v2/map`, `/v2/crawl` (y
  los alias `/v1/*`). El SDK oficial `firecrawl-py` funciona cambiando solo la
  URL.
- **MCP en `/mcp`** (streamable HTTP), con las herramientas `scrape`,
  `screenshot`, `map`, `crawl_start` y `crawl_status`.
- **API key** obligatoria (`Authorization: Bearer <key>`) en todo, salvo en
  `/health`.
- **Proxy de IP solo cuando hace falta**: la petición sale directa y solo
  reintenta por el proxy si parece bloqueada.

Requisitos, decisiones y criterios de aceptación: [docs/plan.md](docs/plan.md).

## Conectar un agente

En los ejemplos de abajo, `<URL>` es la URL pública del servicio (ver
[Acceso desde otras máquinas](#acceso-desde-otras-máquinas)).

Obtén la clave en la máquina del servicio:

```bash
sudo sed -n 's/^SCRAPE_API_KEYS=//p' /etc/scrape-api/env
```

**Claude Code** (en cualquier máquina):

```bash
claude mcp add --scope user --transport http scrape <URL>/mcp \
  --header "Authorization: Bearer <key>"
```

**SDK de Firecrawl** (Python):

```python
from firecrawl import Firecrawl
fc = Firecrawl(api_key="<key>", api_url="<URL>")
doc = fc.scrape("https://example.com", formats=["markdown"])
job = fc.crawl("https://docs.example.com", limit=50)
```

**curl**:

```bash
curl -s <URL>/v2/scrape \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "formats": ["markdown", "links"]}'
```

## Endpoints

| Ruta | Qué hace |
| --- | --- |
| `GET /health` | Estado, sin autenticación. |
| `POST /v2/scrape` | Una página. `formats`: `markdown`, `html`, `rawHtml`, `links`, `screenshot` (siempre de la página entera; `fullPage` se acepta y se ignora). También `onlyMainContent` (por defecto `true`), `includeTags`, `excludeTags`, `waitFor` (ms), `timeout` (ms), `proxy`. |
| `POST /v2/map` | Las URLs del sitio, a partir de robots.txt/sitemaps y de los enlaces de la página. También `search`, `limit`, `sitemap` (`include`/`skip`/`only`), `includeSubdomains`, `ignoreQueryParameters`. |
| `POST /v2/crawl` | Inicia un crawl y devuelve `id` y `url`. También `limit`, `maxDiscoveryDepth`, `includePaths`/`excludePaths` (regex sobre el path), `crawlEntireDomain`, `allowSubdomains`, `allowExternalLinks`, `sitemap`, `delay`, `maxConcurrency`, `scrapeOptions`. Respeta robots.txt. |
| `GET /v2/crawl/{id}` | `status`, `total`, `completed` y `data`, en páginas de hasta 50 documentos o 10 MB, con `next`. Incluye `warning` si el crawl se detuvo al alcanzar el tope de memoria. |
| `GET /v2/crawl/{id}/errors` | `errors` y `robotsBlocked`. |
| `DELETE /v2/crawl/{id}` | Cancela. Responde 409 si el crawl ya terminó. |
| `/v1/scrape`, `/v1/map`, `/v1/crawl…` | El formato v1 (`map` devuelve las URLs como strings; acepta `maxDepth` e `ignoreSitemap`). |
| `/mcp` | Servidor MCP. |

Fuera de alcance: `/search`, extracción con LLM (`json`, `summary`), batch
scrape, webhooks y acciones en la página.

## Proxy de IP

El campo `proxy` de cada petición decide el camino:

- `auto` (por defecto): sale directo. Solo reintenta por un proxy configurado si
  la respuesta directa es un 403, 429 o 503, una página de desafío anti-bot
  (Cloudflare, DataDome, PerimeterX, Imperva, Akamai), o si la conexión se corta
  o agota el tiempo.
- `basic`: nunca usa proxy.
- `stealth` (o `enhanced`, el nombre que usa el SDK más nuevo): siempre usa
  proxy. Responde 400 si no hay ningún proxy configurado.

`metadata.proxyUsed` indica el camino usado (`basic` o `stealth`). Sin proxy
configurado, una página bloqueada vuelve con `warning`.

Para configurarlo, edita `/etc/scrape-api/env` y reinicia el servicio:

```bash
# Uno o varios, separados por coma (se usan por turnos)
SCRAPE_PROXY_URLS=http://usuario:contraseña@p.webshare.io:80
sudo systemctl restart scrape-api
```

Opción gratuita: [Webshare](https://www.webshare.io) tiene un plan gratuito con
10 proxies de datacenter y 1 GB/mes. Algunos proveedores de VM bloquean puertos
de salida, así que comprueba que el puerto del proxy pasa: Webshare usa el 80,
Bright Data el 33335, IPRoyal el 12321 y Oxylabs el 7777.

## Configuración (`/etc/scrape-api/env`)

| Variable | Por defecto | |
| --- | --- | --- |
| `SCRAPE_API_KEYS` | (generada en la instalación) | Claves aceptadas, separadas por coma. El servicio no arranca sin clave. |
| `SCRAPE_PROXY_URLS` | vacío | Proxies upstream. |
| `SCRAPE_PORT` / `SCRAPE_HOST` | `3002` / `127.0.0.1` | Dónde escucha el servicio. |
| `SCRAPE_PUBLIC_URL` | vacío (`--public-url` del instalador) | Base de los enlaces `url`/`next` de los crawls. Vacía, se usa la dirección de la petición. |
| `SCRAPE_MAX_CONCURRENCY` | `4` | Páginas abiertas a la vez en el navegador. |
| `SCRAPE_PAGE_TIMEOUT_MS` | `30000` | Tiempo límite por página. |
| `SCRAPE_MAX_CRAWL_PAGES` | `500` | Tope del `limit` de un crawl. |
| `SCRAPE_CRAWL_TTL_SECONDS` / `SCRAPE_MAX_CRAWL_JOBS` | `86400` / `50` | Cuánto tiempo queda disponible un crawl después de terminar, y cuántos se guardan en memoria. |
| `SCRAPE_MAX_CRAWL_MB` | `256` | Tope de memoria de los resultados de un crawl. Al alcanzarlo, el crawl termina con `warning`. |
| `SCRAPE_MAX_TOTAL_CRAWL_MB` | `1024` | Tope sumado de todos los crawls. Los crawls terminados más antiguos se descartan primero. |
| `SCRAPE_ALLOW_PRIVATE_URLS` | `false` | Permite IPs privadas y loopback. Déjalo desactivado en un servicio público. |

## Instalar en una máquina nueva

Funciona en cualquier Linux con systemd, cgroup v2 y el `python3` del sistema
(3.10 o más nuevo), ejecutado como usuario normal con sudo. En Debian y Ubuntu,
el instalador instala por sí solo las bibliotecas de sistema de Chromium; en
otras distribuciones, instálalas con el gestor de paquetes si te lo pide.

```bash
git clone https://github.com/eliabejr/armadillo.git ~/apps/scrape-api
cd ~/apps/scrape-api && ./deploy/install.sh
```

El instalador instala:
- `uv` (si falta) y el entorno de Python;
- el Chromium de Playwright en `/opt/scrape-api` (y las bibliotecas del
  sistema, si hace falta);
- el archivo de configuración, con una clave nueva;
- las units systemd `scrape-api` (el servicio) y `scrape-api-addresses` (el
  vigilante de las direcciones de la máquina, ver [Seguridad](#seguridad)).

Después comprueba `/health` y el filtro de red. Puedes volver a ejecutarlo
cuantas veces quieras: la clave y la configuración se conservan. Opciones:
`--port N` y `--public-url URL`.

## Acceso desde otras máquinas

El servicio escucha solo en `127.0.0.1:3002`. Para que los agentes de otras
máquinas lleguen a él, pon delante un proxy inverso o un túnel con HTTPS (la
clave viaja en una cabecera), con un tiempo límite de al menos 2 minutos. Por
ejemplo:

```bash
tailscale serve --bg 3002                                        # solo tus máquinas (tailnet)
caddy reverse-proxy --from api.example.com --to 127.0.0.1:3002   # dominio propio
cloudflared tunnel --url http://127.0.0.1:3002                   # sin abrir puertos
```

La publicación de puertos del proveedor de la VM también sirve. Después, guarda
la URL pública, que pasa a ser la base de los enlaces de los crawls:

```bash
./deploy/install.sh --public-url https://api.example.com
```

## Desde tu máquina (este repo)

El destino va en `.deploy.env` (ignorado por Git), por ejemplo
`SCRAPE_SSH="ssh -p 2222 usuario@host"`, o en la variable `SCRAPE_SSH`.

```bash
scripts/deploy.sh          # envía una release nueva a ~/apps/scrape-api/releases/ y ejecuta install.sh
scripts/test-remote.sh     # ejecuta la suite de tests en la máquina remota (que tiene el navegador)
SCRAPE_SSH="ssh user@otra-maquina" scripts/deploy.sh   # otra máquina
```

Una release nueva solo entra en servicio si su `install.sh` pasa. Si falla, el
instalador vuelve a la anterior cuando esta pasa las mismas comprobaciones
(incluida la del vigilante de direcciones), o elimina las units. El deploy
conserva la release actual y la anterior y borra las más antiguas.

Los tests de integración levantan la API de verdad, con un sitio de prueba y un
proxy de prueba locales. Cada test de integración tiene un ID estable
(`@pytest.mark.case("PRX-02")`), y `tests/conftest.py` falla si falta un ID o
si se repite.

## Seguridad

Las páginas scrapeadas no son de confianza. Cualquier sitio puede intentar
llevar el navegador hacia dentro de la VM, por ejemplo a un servicio en
`localhost`, así que las protecciones van por capas:

- **En el kernel (la capa que cuenta).**
  - La unit `scrape-api.socket` es dueña del puerto y se lo pasa al servicio
    por socket activation.
  - El servicio tiene `IPAddressDeny` para loopback, redes privadas,
    link-local, metadatos y los prefijos de la propia máquina, públicos
    incluidos: sus direcciones IPv4 y el /64 de cada dirección IPv6 (o el
    prefijo de la red local, si es más amplio).
  - El kernel descarta toda conexión que el servicio o Chromium abren hacia
    esos destinos (agota el tiempo), venga de un redirect, iframe, popup,
    worker, websocket o DNS rebinding. Las conexiones que llegan por el puerto
    pasado no se ven afectadas.
  - Las direcciones cambian (una VM puede despertar con otra IPv6, sin
    reiniciar nada), así que la unit `scrape-api-addresses` sigue los eventos
    de dirección y de ruta del kernel y añade cada prefijo nuevo al filtro del
    servicio en ejecución, en menos de un segundo. Se ejecuta como root, desde
    una copia de `deploy/netcheck.py` que solo root puede modificar, con el
    `python3` del sistema. El servicio está atado a ella (`BindsTo`): si el
    vigilante se detiene, el servicio también.
- **En la aplicación.** Se rechazan las URLs ambiguas: `\`, espacios,
  credenciales, puerto o host no válidos. El navegador recibe la URL canónica
  que se comprobó. Los hosts privados se rechazan en el primer salto. Las
  peticiones del navegador a IPs privadas literales o a `localhost` se abortan.
  Una página cuya URL final es privada se descarta, con respuesta 400.
- **Aislamiento.**
  - Chromium se ejecuta con su sandbox activado, como un usuario dinámico.
  - Ese usuario no ve la home del usuario de login (donde están las
    credenciales de los agentes), ni los procesos de otros usuarios, ni el
    socket de Docker.
  - También se aplican `MemoryMax=60%` y `TasksMax`.
- **Entradas del llamador.** Las regex de `includePaths`/`excludePaths` tienen
  tiempo limitado: un patrón lento hace que el crawl deje de seguir enlaces, con
  `warning`. Las credenciales de los proxies se eliminan de los mensajes de
  error.

## Límites conocidos

- **Las máquinas que se suspenden cuando están inactivas** (algunos proveedores
  de sandbox lo hacen) dejan la URL pública sin respuesta hasta que despiertan.
  Para que la API esté siempre disponible, desactiva la suspensión automática en
  el proveedor.
- Los crawls viven en memoria y se pierden cuando el servicio se reinicia
  (Firecrawl también guarda los resultados solo 24 h).
- **Protección del kernel solo con el instalador.** Viene de la unit systemd y
  requiere cgroup v2 con BPF. El instalador comprueba el filtro (desde dentro
  del cgroup del servicio, ninguna conexión a loopback ni a las direcciones de
  la máquina puede pasar) y, si falla, vuelve a la versión anterior solo si esta
  pasa la misma comprobación; si no, elimina las units. Fuera de la unit (por
  ejemplo, en los tests), solo valen las comprobaciones de la aplicación.
- **DNS.** Los resolvers en una red privada que aparecen en `/etc/resolv.conf`
  (el de la VPC, el del router) se permiten al servicio, en todos los puertos.
  Los resolvers en loopback o link-local nunca: el instalador avisa, y el
  servicio queda sin DNS hasta que haya otro. Con systemd-resolved, el servicio
  lee los servidores de `/run/systemd/resolve/resolv.conf` en lugar del stub
  `127.0.0.53`.
- **DNS fijado en la instalación.** Los resolvers se leen cuando se ejecuta el
  instalador; si cambian, vuelve a ejecutarlo. Las direcciones de la máquina,
  no: el vigilante las sigue, con una ventana de una fracción de segundo entre
  el cambio y el bloqueo.
- **Vecinos en la misma red IPv6.** Los sitios alojados en el mismo /64 que la
  máquina (o en la misma red local, si su prefijo es más amplio) quedan fuera
  del alcance del servicio.
- **Profundidad distinta de la de Firecrawl.** Las páginas del sitemap cuentan
  como profundidad 1. El `maxDepth` de v1 cuenta saltos de enlace, no segmentos
  del path. Las URLs bloqueadas por robots.txt también consumen el `limit`.
- **Anti-bot fuerte.** Los sitios con una protección anti-bot fuerte pueden
  seguir bloqueando, incluso con un proxy de datacenter. En esos casos, un
  proxy residencial o Firecrawl Cloud funcionan mejor.
