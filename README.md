# armadillo

API de scraping self-hosted, com a mesma API do Firecrawl, e servidor MCP para
agentes Claude. Roda sobre o [Crawl4AI](https://github.com/unclecode/crawl4ai)
(Chromium via Playwright) e cabe num único serviço systemd.

No servidor e no código o nome é `scrape-api` (serviço systemd,
`/etc/scrape-api`, pacote `scrape_api`).

- **REST no formato Firecrawl**: `/v2/scrape`, `/v2/map`, `/v2/crawl` (e os
  aliases `/v1/*`). O SDK oficial `firecrawl-py` funciona trocando só a URL.
- **MCP em `/mcp`** (streamable HTTP), com as ferramentas `scrape`,
  `screenshot`, `map`, `crawl_start` e `crawl_status`.
- **API key** obrigatória (`Authorization: Bearer <key>`) em tudo, menos em
  `/health`.
- **Proxy de IP só quando precisa**: a requisição sai direta e só tenta de novo
  pelo proxy se parecer bloqueada.

Requisitos, decisões e critérios de aceite: [docs/plan.md](docs/plan.md).

## Conectar um agente

Nos exemplos abaixo, `<URL>` é a URL pública do serviço (veja
[Acesso de outras máquinas](#acesso-de-outras-máquinas)).

Pegue a chave na máquina do serviço:

```bash
sudo sed -n 's/^SCRAPE_API_KEYS=//p' /etc/scrape-api/env
```

**Claude Code** (em qualquer máquina):

```bash
claude mcp add --scope user --transport http scrape <URL>/mcp \
  --header "Authorization: Bearer <key>"
```

**SDK do Firecrawl** (Python):

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

| Rota | O que faz |
| --- | --- |
| `GET /health` | Status, sem autenticação. |
| `POST /v2/scrape` | Uma página. `formats`: `markdown`, `html`, `rawHtml`, `links`, `screenshot` (sempre da página inteira; `fullPage` é aceito e ignorado). Também `onlyMainContent` (padrão `true`), `includeTags`, `excludeTags`, `waitFor` (ms), `timeout` (ms), `proxy`. |
| `POST /v2/map` | URLs do site, a partir de robots.txt/sitemaps e dos links da página. Também `search`, `limit`, `sitemap` (`include`/`skip`/`only`), `includeSubdomains`, `ignoreQueryParameters`. |
| `POST /v2/crawl` | Inicia um crawl e devolve `id` e `url`. Também `limit`, `maxDiscoveryDepth`, `includePaths`/`excludePaths` (regex no path), `crawlEntireDomain`, `allowSubdomains`, `allowExternalLinks`, `sitemap`, `delay`, `maxConcurrency`, `scrapeOptions`. Respeita robots.txt. |
| `GET /v2/crawl/{id}` | `status`, `total`, `completed` e `data`, em páginas de até 50 documentos ou 10 MB, com `next`. Traz `warning` se o crawl parou ao atingir o teto de memória. |
| `GET /v2/crawl/{id}/errors` | `errors` e `robotsBlocked`. |
| `DELETE /v2/crawl/{id}` | Cancela. Responde 409 se o crawl já terminou. |
| `/v1/scrape`, `/v1/map`, `/v1/crawl…` | Formato v1 (`map` devolve as URLs como strings; aceita `maxDepth` e `ignoreSitemap`). |
| `/mcp` | Servidor MCP. |

Fora do escopo: `/search`, extração com LLM (`json`, `summary`), batch scrape,
webhooks e ações na página.

## Proxy de IP

O campo `proxy` de cada requisição decide o caminho:

- `auto` (padrão): sai direto. Só tenta de novo por um proxy configurado se a
  resposta direta vier com 403, 429 ou 503, se for uma página de desafio
  anti-bot (Cloudflare, DataDome, PerimeterX, Imperva, Akamai), ou se a conexão
  cair ou estourar o tempo.
- `basic`: nunca usa proxy.
- `stealth` (ou `enhanced`, o nome usado pelo SDK mais novo): sempre usa proxy.
  Responde 400 se nenhum proxy estiver configurado.

`metadata.proxyUsed` informa o caminho usado (`basic` ou `stealth`). Sem proxy
configurado, uma página bloqueada volta com `warning`.

Para configurar, edite `/etc/scrape-api/env` e reinicie o serviço:

```bash
# Um ou vários, separados por vírgula (usados em rodízio)
SCRAPE_PROXY_URLS=http://usuario:senha@p.webshare.io:80
sudo systemctl restart scrape-api
```

Opção gratuita: a [Webshare](https://www.webshare.io) tem um plano grátis com
10 proxies de datacenter e 1 GB/mês. Alguns provedores de VM bloqueiam portas
de saída, então confira se a porta do proxy passa: Webshare usa a 80, Bright
Data a 33335, IPRoyal a 12321 e Oxylabs a 7777.

## Configuração (`/etc/scrape-api/env`)

| Variável | Padrão | |
| --- | --- | --- |
| `SCRAPE_API_KEYS` | (gerada na instalação) | Chaves aceitas, separadas por vírgula. O serviço não sobe sem chave. |
| `SCRAPE_PROXY_URLS` | vazio | Proxies upstream. |
| `SCRAPE_PORT` / `SCRAPE_HOST` | `3002` / `127.0.0.1` | Onde o serviço escuta. |
| `SCRAPE_PUBLIC_URL` | vazio (`--public-url` do instalador) | Base dos links `url`/`next` dos crawls. Vazia, usa o endereço da requisição. |
| `SCRAPE_MAX_CONCURRENCY` | `4` | Páginas abertas ao mesmo tempo no navegador. |
| `SCRAPE_PAGE_TIMEOUT_MS` | `30000` | Tempo limite por página. |
| `SCRAPE_MAX_CRAWL_PAGES` | `500` | Teto do `limit` de um crawl. |
| `SCRAPE_CRAWL_TTL_SECONDS` / `SCRAPE_MAX_CRAWL_JOBS` | `86400` / `50` | Por quanto tempo um crawl fica disponível depois de terminar, e quantos ficam em memória. |
| `SCRAPE_MAX_CRAWL_MB` | `256` | Teto de memória dos resultados de um crawl. Ao atingi-lo, o crawl termina com `warning`. |
| `SCRAPE_MAX_TOTAL_CRAWL_MB` | `1024` | Teto somado de todos os crawls. Os crawls terminados mais antigos são descartados primeiro. |
| `SCRAPE_ALLOW_PRIVATE_URLS` | `false` | Libera IPs privados e loopback. Deixe desligado num serviço público. |

## Instalar numa máquina nova

Funciona em qualquer Linux com systemd e cgroup v2, rodando como usuário comum
com sudo. Em Debian e Ubuntu, o instalador instala sozinho as bibliotecas de
sistema do Chromium; em outras distribuições, instale-as pelo gerenciador de
pacotes se ele pedir.

```bash
git clone <este repo> ~/apps/scrape-api   # ou copie a pasta
cd ~/apps/scrape-api && ./deploy/install.sh
```

O instalador instala:
- o `uv` (se faltar) e o ambiente Python;
- o Chromium do Playwright em `/opt/scrape-api` (e as bibliotecas do sistema, se
  necessário);
- o arquivo de configuração, com uma chave nova;
- a unit systemd `scrape-api`.

Depois, confere o `/health` e o filtro de rede. Pode rodar de novo à vontade: a
chave e as configurações são mantidas. Opções: `--port N` e `--public-url URL`.

## Acesso de outras máquinas

O serviço escuta só em `127.0.0.1:3002`. Para os agentes de outras máquinas o
alcançarem, ponha na frente um proxy reverso ou túnel com HTTPS (a chave vai no
cabeçalho), com tempo limite de pelo menos 2 minutos. Por exemplo:

```bash
tailscale serve --bg 3002                                        # só as suas máquinas (tailnet)
caddy reverse-proxy --from api.example.com --to 127.0.0.1:3002   # domínio próprio
cloudflared tunnel --url http://127.0.0.1:3002                   # sem abrir portas
```

O recurso de publicar portas do provedor da VM também serve. Depois, grave a URL
pública, que vira a base dos links dos crawls:

```bash
./deploy/install.sh --public-url https://api.example.com
```

## A partir do Mac (este repo)

O destino fica em `.deploy.env` (fora do Git), por exemplo
`SCRAPE_SSH="ssh -p 2222 usuario@host"`, ou na variável `SCRAPE_SSH`.

```bash
scripts/deploy.sh          # envia uma release nova para ~/apps/scrape-api/releases/ e roda o install.sh
scripts/test-remote.sh     # roda a suíte de testes na máquina remota (que tem o navegador)
SCRAPE_SSH="ssh user@outra-maquina" scripts/deploy.sh   # outra máquina
```

O serviço continua na release anterior até o `install.sh` da nova terminar bem.
O deploy guarda a release atual e a anterior e apaga as mais antigas.

Os testes de integração sobem a API de verdade, com um site de teste e um proxy
de teste locais. Cada teste de integração tem um ID estável
(`@pytest.mark.case("PRX-02")`), e o `tests/conftest.py` falha se um ID faltar
ou se repetir.

## Segurança

As páginas raspadas não são confiáveis. Qualquer site pode tentar levar o
navegador para dentro da VM, por exemplo para um serviço em `localhost`, e as
proteções são em camadas:

- **No kernel (a camada que vale).**
  - A unit `scrape-api.socket` é dona da porta e a passa ao serviço por socket
    activation.
  - O serviço tem `IPAddressDeny` para loopback, redes privadas, link-local,
    metadados e os próprios endereços da máquina (inclusive os públicos, como o
    IPv6 da VPS).
  - Toda conexão que o serviço ou o Chromium abrem para esses destinos é
    recusada, venha de redirect, iframe, popup, worker, websocket ou DNS
    rebinding. As conexões que chegam pela porta passada não são afetadas.
- **Na aplicação.** URLs ambíguas são recusadas: `\`, espaços, credenciais,
  porta ou host inválidos. O navegador recebe a URL canônica que foi conferida.
  Hosts privados são recusados no primeiro salto. Requisições do navegador para
  IPs privados literais ou `localhost` são abortadas. Uma página cuja URL final
  é privada é descartada, com resposta 400.
- **Isolamento.**
  - O Chromium roda com o sandbox ligado, como um usuário dinâmico.
  - Esse usuário não enxerga a home do usuário de login (onde ficam as
    credenciais dos agentes), nem os processos de outros usuários, nem o socket
    do Docker.
  - Valem também `MemoryMax=60%` e `TasksMax`.
- **Entradas do chamador.** Os regex de `includePaths`/`excludePaths` têm tempo
  limitado: um padrão lento faz o crawl parar de seguir links, com `warning`. As
  credenciais dos proxies são removidas das mensagens de erro.

## Limites conhecidos

- **Máquinas que suspendem quando ociosas** (alguns provedores de sandbox fazem
  isso) deixam a URL pública sem resposta até acordarem. Para deixar a API
  sempre disponível, desligue a suspensão automática no provedor.
- Os crawls ficam em memória e se perdem quando o serviço reinicia (o Firecrawl
  também guarda resultados por apenas 24 h).
- **Proteção do kernel só com o instalador.** Ela vem da unit systemd e exige
  cgroup v2 com BPF. O instalador confere o filtro (de dentro do cgroup do
  serviço, nenhuma conexão para loopback ou para os endereços da máquina pode
  passar) e, se ele falhar, volta para a versão anterior só se ela passar na
  mesma conferência; senão remove as units. Rodando fora da unit (por exemplo, nos testes), só
  valem as checagens da aplicação.
- **DNS.** Resolvedores em rede privada que aparecem no `/etc/resolv.conf` (o
  da VPC, o do roteador) são liberados para o serviço, em todas as portas.
  Resolvedores em loopback ou link-local nunca são: o instalador avisa, e o
  serviço fica sem DNS até haver outro. Com systemd-resolved, o serviço lê os
  servidores de `/run/systemd/resolve/resolv.conf` em vez do stub `127.0.0.53`.
- **Lista fixada na instalação.** Os resolvedores e os endereços da máquina são
  lidos quando o instalador roda. Se mudarem (outro IP, outro DNS), rode o
  instalador de novo.
- **Profundidade diferente do Firecrawl.** As páginas do sitemap entram como
  profundidade 1. O `maxDepth` da v1 conta saltos de link, não segmentos do
  caminho. URLs bloqueadas pelo robots.txt também consomem o `limit`.
- **Anti-bot pesado.** Sites com proteção anti-bot pesada podem continuar
  bloqueando, mesmo com proxy de datacenter. Nesses casos, um proxy
  residencial ou o Firecrawl Cloud funcionam melhor.
