# BerTech UAT Runner — Claude Code Handoff

## O que é isso
Ferramenta de UAT automatizado para o produto **Buoy-Champ** (BerTech).
Stack: **FastAPI + Jinja2 + SQLite** no backend, **Playwright async** para automação do browser.
Roda N workers simultâneos em headless, coleta erros de console e gera reports por step.

## Como iniciar
```bash
cd bertech_uat/
pip install -r requirements.txt
playwright install chromium
./start.sh          # http://localhost:8080
```

## Estrutura de arquivos relevantes
```
bertech_uat/
├── app/
│   ├── config.py                      ← envs, fixtures, email por usuário
│   ├── db.py                          ← SQLite schema + queries
│   ├── runner.py                      ← asyncio.gather N workers
│   ├── scenarios/
│   │   ├── registry.py                ← mapeamento de cenários
│   │   └── buoy_champ_onboarding.py   ← O ARQUIVO PRINCIPAL (1237 linhas)
│   └── web/
│       ├── routers/suites.py          ← POST /suites/{id}/run
│       ├── routers/runs.py            ← GET /runs/{id} + polling
│       └── templates/                 ← Jinja2 HTML
├── Payroll Register_mock.xlsx         ← fixture de upload (E3)
└── HANDOFF.md                         ← este arquivo
```

---

## Status atual dos 14 steps do cenário

| Step | Status | Observação |
|---|---|---|
| Onboarding 1 — Intro | ✅ Funciona | CTA "Get Started" OK |
| **Onboarding 2 — User Type** | ❌ Falha | Ver seção abaixo |
| **Onboarding 3 — Company Info** | ❌ Falha | Depende do O2 avançar |
| Onboarding 4 — Census & Payroll | ⚠️ Parcial | Depende do O3 |
| Onboarding 5 — Impact Estimate | ✅ Funciona | CTA detectado via fallback |
| Sign Up — /champ-signup | ⚠️ Incerto | `#password` pode falhar se O3 não preencher `#email` |
| OTP — /verify-champ | ❌ Nunca testado com sucesso | Bloqueado pelo Sign Up |
| Enrollment 1–5 | ⚠️ Não testado | Nunca alcançados |

---

## Problema principal: Onboarding 2 (Business Owner)

### O que acontece
O step O2 não consegue clicar no card "Business Owner". Como resultado, `Save & Next` não navega (form validation bloqueia sem seleção), a URL permanece `/onboarding-champ/` e todos os steps seguintes falham em cascata.

### O que já foi tentado (não funcionou)
- `button:has-text("Business Owner")` — sem texto no elemento
- `[data-value="business_owner"]` — atributo não existe
- `[class*="card"]:has-text("Business Owner")` — não encontrado
- JS scan DOM por texto "Business Owner" — também não encontrado
- `button:nth-of-type(1) > div` — ambíguo globalmente, não clicou o certo
- XPath do Chrome Recorder: `//*[@id="root"]/div/form/div[2]/div[2]/div/div/div/div[2]/div/button[1]/div` — ainda testando

### Próximo passo recomendado
Rodar em modo **headed** com `page.pause()` logo após O1 para inspecionar o DOM real da tela de seleção de tipo de usuário:

```python
# Adicionar temporariamente no início de O2, depois de _reload_wait:
await page.pause()  # abre o Playwright Inspector
```

Ou rodar um script isolado:
```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=500)
    page = browser.new_page()
    page.goto("https://dev.buoyhub.com/onboarding-champ/")
    page.locator('button').first.click()  # Get Started
    page.pause()  # inspecionar aqui
```

---

## Onboarding 3 — o formulário correto (confirmado via Chrome Recorder)

O notebook e a implementação anterior estavam errados. O formulário de onboarding **não tem** full_name, phone, website, EIN, address. É muito mais simples:

```
#company    → business name
#email      → email do usuário  ← CRÍTICO: precisa ser o email de teste (1secmail)
Tab → Enter → ArrowDown → Enter  → state dropdown (primeiro item)
button.css-sndmno  → Save & Next
```

O `#email` precisa receber o email único do worker (`bertechqa_rX_uY@1secmail.com`) para o OTP ir para o inbox certo.

---

## Onboarding 4 — seletores confirmados via Chrome Recorder

```python
# Payroll frequency
page.locator("body").click(position={"x": 1074, "y": 643})  # abre dropdown
page.locator("#menu-payroll li:nth-of-type(1)").click()       # Weekly

# Annual gross (React controlled — precisa de nativeSetter)
document.getElementById('annual_gross')  # ID confirmado

# Worker state
page.locator("body").click(position={"x": 1077, "y": 639})
page.locator("#menu-worker_distribution\\.0\\.state li:nth-of-type(1)").click()  # Alabama

# Num employees — ATENÇÃO: ID tem pontos, não usar CSS #id\.x
document.getElementById('worker_distribution.0.percentage')  # usar getElementById

# Get Estimate
button.css-sndmno / aria "Get Estimate"
```

**Bug corrigido**: `#worker_distribution\.0\.percentage` lança SyntaxError no querySelector.
Correto: `document.getElementById('worker_distribution.0.percentage')` ou `input[id="worker_distribution.0.percentage"]`.

---

## Sign Up — seletores confirmados via Chrome Recorder

```
#password           → senha (ID confirmado no Recorder)
button.css-sndmno   → "Create An Account" (não "Create Account")
```

O `#email` **não aparece** na página de sign-up — ele é pré-preenchido com o email digitado em O3. Por isso O3 precisa estar correto primeiro.

---

## OTP — implementação atual (1secmail REST API)

Trocou de Mailinator (scraping via browser, bloqueado por bot detection) para **1secmail.com REST API**:

```python
async def _get_otp_1secmail(login, domain="1secmail.com", max_attempts=20, poll_interval=4.0):
    # GET https://www.1secmail.com/api/v1/?action=getMessages&login=XXX&domain=1secmail.com
    # GET https://www.1secmail.com/api/v1/?action=readMessage&login=XXX&domain=1secmail.com&id=YYY
    # Extrai regex r'\b(\d{6})\b' do textBody/htmlBody
```

O OTP ainda **nunca foi testado com sucesso** porque Sign Up nunca completou. Uma vez que O2→O3→Sign Up funcionem, o OTP deve funcionar — a menos que o app rejeite `@1secmail.com` como domínio descartável (verifique no network tab se o POST de /api/signup retorna erro de email inválido).

---

## Enrollment 1–5 — implementado mas não testado

Quando O2–OTP estiverem funcionando, o flow continua para enrollment. Esses steps foram implementados com base em Chrome Recorder JSONs fornecidos pelo usuário:

- **E1**: Account Manager role card + F5
- **E2**: Company Information form completo (EIN, address, payroll provider, etc.)
- **E3B**: Persona KYB via `frame_locator('iframe[src*="withpersona.com"]')`
- **E3C**: Payroll upload (`set_input_files` + `Payroll Register_mock.xlsx`) + LOI "No"
- **E4**: Final Impact Report → Full Workforce já selecionado → "Continue With Selection"
- **E5**: Sign Agreements → PandaDoc abre em nova aba → Tab/Enter signing flow

---

## Variáveis de ambiente relevantes

```bash
ONEMAIL_PREFIX=bertechqa        # prefixo dos inboxes 1secmail
ONEMAIL_DOMAIN=1secmail.com     # domínio 1secmail
MAX_CONCURRENT_USERS=10         # cap de workers
BERTECH_DB_PATH=/tmp/bertech.db # usar /tmp/ se filesystem montado via rede
PW_TIMEOUT=30000
PW_NAV_TIMEOUT=60000
```

---

## Decisões de design importantes

1. **Email isolation**: cada worker usa `bertechqa_rRUN_uUSER@1secmail.com` — inbox completamente separado
2. **MUI Select**: `locator.click()` não abre dropdowns nesse SPA — usar `page.evaluate()` com `.click()` nativo ou clicar no `body` com coordenadas para abrir o menu
3. **React controlled inputs**: `fill()` não dispara `onChange` — usar JS `nativeSetter` + `dispatchEvent`
4. **Salesforce persistence**: aguardar 2–4s antes do F5 após cada "Next" de enrollment, senão SF não salvou e a página volta
5. **PandaDoc E5**: abre como nova **aba** (não iframe) — usar `page.context.expect_page()`
6. **SQLite WAL mode**: falha em filesystem montado via rede — fallback para DELETE mode já implementado em db.py
