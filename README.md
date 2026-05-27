# USCIS Silent Update Tracker

Monitora múltiplos casos USCIS (`IOE...`) via a API interna do `my.uscis.gov`, detecta *silent updates* (mudanças internas que não aparecem ainda no status público) por meio de SHA-256 sobre os campos relevantes, e renderiza um dashboard HTML estático com o histórico de cada caso.

Sem servidor, sem banco, sem autenticação própria — usa o cookie do seu Chrome.

---

## Como funciona

1. O script lê os cookies do Chrome direto do disco (via `browser_cookie3`).
2. Para cada caso configurado, faz `GET https://my.uscis.gov/account/case-service/api/cases/{receipt}`.
3. Calcula SHA-256 sobre `updatedAt`, `status`, `events`, `closed`, `actionRequired` — campos que mudam quando algo de fato acontece com o caso.
4. Compara com o SHA da execução anterior. Se diferente, é um silent update.
5. Persiste **toda** checagem em `data/{receipt}.json` (nada é descartado).
6. Regenera `dashboard.html` autossuficiente (dados + CSS + JS inline) — basta abrir no browser.

### Múltiplas contas USCIS

Cada cookie de sessão só dá acesso aos casos da conta logada. Pra monitorar casos de várias contas simultaneamente, o script usa **Chrome profiles separados** — um por conta — e descobre automaticamente qual profile autoriza qual caso.

- Auto-descoberta dos profiles em `~/Library/Application Support/Google/Chrome/`
- Cache do mapeamento `receipt → profile` em `data/_profile_map.json`
- Fallback automático: se o profile cacheado falha, tenta os outros
- Detecção: USCIS retorna **HTTP 500** quando a conta logada não é dona do caso (não 401/403), então o script trata ambos como "tentar outro profile"

---

## Setup

### 1. Dependências

```bash
python3 -m pip install -r requirements.txt
```

Apenas `requests` e `browser_cookie3`.

### 2. Configurar os casos

Copie o template e edite:

```bash
cp config.example.json config.json
```

```json
{
  "cases": [
    { "name": "Luis I-485", "receipt": "IOE0936674431" },
    { "name": "Theo EAD",   "receipt": "IOE0936674428" }
  ]
}
```

- `name`: label livre para o dashboard (use um prefixo consistente por dono — ex: "Luis", "Theo", "Camila" — o setup usa isso pra agrupar)
- `receipt`: número completo começando com `IOE`

> `config.json` está no `.gitignore` — não é commitado.

### 3. Chrome profiles (uma conta USCIS por profile)

Para cada conta USCIS que você quer monitorar:

1. No Chrome: `chrome://settings/manageProfile` → **Add** → nome (ex: "Theo")
2. Abra `https://my.uscis.gov` nesse profile e faça login completo (incluindo login.gov se for o caso)
3. Confirme que a página do `/account/applicant` mostra os casos dessa conta

> **Importante**: estar logado no Google nesse profile **não basta** — a sessão de `my.uscis.gov` é separada e precisa ser bootstrappada navegando até o `/account/applicant`.

### 4. Mapear casos aos profiles

```bash
python3 check.py setup
```

Saída exemplo:

```
Chrome profiles encontrados (2):
  Default         Luis    oieusouofinx@gmail.com
  Profile 1       Theo    theomvolpato@gmail.com

Testando acesso aos 6 casos...

  ✓ Luis I-485     IOE0936674431  →  Default
  ✓ Theo EAD       IOE0936674428  →  Profile 1
  ✗ Camila I-485   IOE0936674434  →  sem profile autorizado

Conta 'Camila' (1 caso[s]): sugestão de profile = "Camila"
   Abrir Chrome no profile 'Camila' agora? [y/N]
```

Pra casos não mapeados, o setup oferece abrir o Chrome num profile novo pra você logar. Depois de logar, rode `setup` de novo até dar tudo `✓`.

---

## Uso

```bash
python3 check.py
```

Saída:

```
[09:30:01] Chrome profiles encontrados: Default, Profile 1
[09:30:01] Checando 6 casos...
[09:30:02] ✓ Luis I-485 (IOE0936674431) [Default] — sem mudança
[09:30:03] 🔔 Theo EAD (IOE0936674428) [Profile 1] — SILENT UPDATE DETECTADO
[09:30:04] ⚠️  Maria I-485 (IOExxxxxxxxx) [Default, Profile 1] — sem profile autorizado
[09:30:05] Dashboard gerado: dashboard.html
```

Abrir o dashboard:

```bash
open dashboard.html
```

### Frequência

1x por dia (de manhã, horário ET é quando a USCIS movimenta) é mais do que suficiente. 6 requests + leves backoffs ficam abaixo do radar do WAF.

### Agendamento

Pra rodar automaticamente, configurar `launchd` (macOS) ou `cron`. O script é idempotente — se a sessão expirou, ele marca `cookie_expired` no estado e o dashboard mostra um banner.

---

## Estrutura

```
uscis-tracker/
├── check.py                # script principal (CLI)
├── config.json             # casos a monitorar (gitignored)
├── config.example.json     # template
├── requirements.txt
├── data/                   # histórico (gitignored)
│   ├── IOE0936674431.json     # uma entry por receipt
│   ├── ...
│   └── _profile_map.json      # cache de receipt → Chrome profile
└── dashboard.html          # gerado a cada execução (gitignored)
```

### Formato de `data/{receipt}.json`

```json
{
  "receipt": "IOE0936674431",
  "name": "Luis I-485",
  "last_sha": "a3f9b2...",
  "cookie_expired": false,
  "profile": "Default",
  "checks": [
    {
      "timestamp": "2026-05-26T17:40:48",
      "sha": "a3f9b2...",
      "changed": false,
      "cookie_expired": false,
      "profile": "Default",
      "snapshot": { "...": "raw JSON da API" }
    }
  ]
}
```

Todas as checagens são preservadas — o array `checks` cresce indefinidamente.

---

## Dashboard

`dashboard.html` é um arquivo único e autossuficiente. Estética dark/âmbar/mono, sem dependências externas além do Google Fonts (IBM Plex). Mostra por caso:

- Status público atual (quando disponível)
- `updatedAt` interno
- Eventos recentes (últimos 5, com `eventCode` + timestamp)
- Histórico de checagens (últimas 20, com SHA e flag `CHANGED`)
- Badge: `● SEM MUDANÇA`, `🔔 SILENT UPDATE` (pulsante) ou `⚠️ COOKIE EXPIRADO`

---

## Caveats

- **Endpoint não-documentado**: `case-service/api/cases/{receipt}` é interno. Pode mudar sem aviso.
- **Cloudflare**: o endpoint público `egov.uscis.gov/csol-api/case-tracking/` está protegido por challenge JS, então o script não consegue enriquecer com o texto amigável de status (apenas `eventCode`). Mapeamento local de códigos pode ser adicionado depois.
- **Full Disk Access (macOS)**: `browser_cookie3` lê o SQLite do Chrome. Pode ser preciso conceder *Full Disk Access* ao Terminal/iTerm/VS Code em **System Settings → Privacy & Security**.
- **Cookies em RAM**: cookies de aba anônima e cookies "fresquinhos" do Chrome em execução podem ainda não estar no disco. Em geral o flush acontece em segundos.
- **Sessão expira**: a sessão de `my.uscis.gov` cai após algumas horas de inatividade. Quando isso acontece, o caso fica `cookie_expired: true` até você reabrir o profile e relogar.

---

## Privacidade

Todos os dados ficam locais. Nada é enviado para fora além das próprias requests à USCIS. O `.gitignore` exclui:

- `config.json` (nomes + receipts)
- `data/` (snapshots completos)
- `dashboard.html` (renderiza os dados)
