<p align="center">
  <img src="docs/assets/logo.svg" alt="" width="96">
</p>

<h1 align="center">telegram-harness</h1>

<p align="center">
  <b>I tuoi agenti di coding, gestiti da Telegram.</b><br>
  Gli agenti Claude Code pianificano, sviluppano, testano, rilasciano e verificano. Un manager li fa andare avanti<br>
  e ti interpella solo sulle decisioni che spettano a te.
</p>

<p align="center">
  <a href="https://github.com/nhype/telegram-harness/actions/workflows/ci.yml"><img src="https://github.com/nhype/telegram-harness/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/platform-Linux%20%2B%20systemd-informational.svg" alt="Linux + systemd">
</p>

<p align="center">
  <a href="#avvio-rapido">Avvio rapido</a> ·
  <a href="#confronto-con-le-alternative">Confronto</a> ·
  <a href="docs/architecture.md">Architettura</a>
</p>

<p align="center"><sub>
  <a href="README.md">English</a> ·
  <a href="README.ru.md">Русский</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.de.md">Deutsch</a> ·
  <b>Italiano</b>
</sub></p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/demo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/demo-light.svg">
    <img src="docs/assets/demo-light.svg" alt="A sinistra una chat Telegram: il proprietario chiede l'export in CSV e il bot riporta piano, implementazione, deploy e verifica live. A destra i pannelli di Herdr: l'agente autore modifica, testa e rilascia, l'agente revisore approva la modifica e il log del bridge non mostra stalli." width="100%">
  </picture>
</p>

**Gestisci gli agenti Claude Code da Telegram.** Mandi un task al tuo bot; Hermes Agent avvia un agente
Claude Code in un pannello di terminale Herdr, lo guida lungo il ciclo di vita OpenSpec (piano → codice →
test → deploy → archivio) e ti scrive solo quando serve una decisione o c'è un risultato.

```
 tu ── Telegram ──▶ Hermes Agent (gateway sull'host) ──▶ il tuo profilo
                       │  chat: riceve i task, risponde a "a che punto siamo?"   skill: harness-delivery
                       │  route webhook ◀── eventi ── bridge ◀── server Herdr (stato dei pannelli)
                       │  un'esecuzione del controller per evento                skill: harness-controller
                       ▼
                    pannelli Herdr: agenti Claude Code al lavoro nel tuo repo (OpenSpec + lean-ctx)
```

- **Herdr** esegue gli agenti in pannelli di terminale e segnala ogni cambio di stato (al lavoro /
  inattivo / finito).
- **Il bridge** (`harness/scripts/herdr_event_bridge.py`) accorpa questi eventi (debounce) e avvia un solo
  run del controller per task alla volta; un watchdog lo risveglia quando un task si blocca o un'attesa
  scade.
- **Hermes Agent** è il manager: il lato chat avvia i task, il lato controller legge il pannello, decide il
  passo successivo e dà istruzioni all'agente. Le questioni tecniche le risolve da solo e chiede a te solo
  su soldi, azioni irreversibili, scelte di prodotto e i tuoi account personali.
- **OpenSpec** dà a ogni task un piano, una checklist e un archivio; **lean-ctx** mantiene compatto il
  contesto degli agenti.

## Perché telegram-harness

La maggior parte degli strumenti ti permette di *chattare* con un agente di coding dal telefono, ma poi
tocca comunque a te fargli da babysitter. telegram-harness ti dà un **manager**: tu dici cosa ti serve, e
lui porta la modifica fino in fondo, tra sviluppo, test, deploy, verifica e merge. Ti scrive solo quando ha
davvero bisogno di te.

- **Un manager, non un passacarte.** Tra un tuo messaggio e l'altro, un controller legge lo schermo
  dell'agente dopo ogni passo e lo fa andare avanti. Risponde alle domande tecniche partendo dal repo e
  dalle tue decisioni passate, sceglie le opzioni dei menu secondo la tua policy e rimette in carreggiata
  l'agente dopo errori dell'API o un contesto saturo.
- **L'intero ciclo di vita, fino in produzione.** Piano OpenSpec → codice → test → deploy → smoke test live
  → archivio → merge in `main`. "Fatto" significa *in produzione e verificato*, non "codice scritto".
- **Ti chiede solo ciò che spetta a te decidere:** soldi, azioni irreversibili, scelte di prodotto, i tuoi
  account. Tutto il resto lo decide da sé e te lo riferisce. Un semplice "sì" in chat arriva all'agente che
  ha fatto la domanda.
- **Mai fermo in silenzio.** Un watchdog guidato dagli eventi intercetta stalli, attese scadute e pannelli
  piantati. Se un task continua a non muoversi, è il bridge stesso a scriverti, anche quando Hermes è giù.
- **Un secondo paio d'occhi.** Un agente revisore indipendente esamina il piano e il diff alla ricerca di
  rischi legati a soldi, privacy, sicurezza, perdita di dati e concorrenza, prima che qualsiasi cosa vada
  in produzione.
- **Il tuo server, il tuo abbonamento.** Codice e produzione non lasciano mai la tua macchina e gli agenti
  girano sul tuo piano Claude. Niente fattura SaaS a task, niente VM del fornitore. Licenza MIT.
- **Osserva o prendi il comando quando vuoi.** Ogni agente vive in un pannello di terminale Herdr che puoi
  aprire, leggere e in cui puoi scrivere.
- **Contesto snello.** lean-ctx comprime ciò che gli agenti leggono, così i task lunghi ci stanno e
  costano meno.
- **Nato sul campo.** Estratto da un setup che porta modifiche in produzione ogni giorno. Ha oltre 320
  test, CI e un test di integrazione con un vero Hermes in un container pulito.

## Confronto con le alternative

| | **telegram-harness** | Bot Telegram per Claude Code¹ | Client mobile² | Orchestratori locali³ | Agenti di coding in cloud⁴ |
|---|---|---|---|---|---|
| Dove girano gli agenti | il tuo server | il tuo server | il tuo computer | il tuo computer | cloud del fornitore |
| Come li guidi | Telegram, in linguaggio naturale | chat Telegram con una sessione | app per telefono / web | TUI o board sul desktop | web, IDE, Slack, GitHub |
| Chi fa avanzare l'agente tra un tuo messaggio e l'altro | **il controller** | tu | tu | tu | l'agente del fornitore |
| Piano → codice → deploy → verifica live → merge, di serie | **sì** | no | no | no, review e merge li fai tu | di solito si ferma a una pull request |
| Agente revisore indipendente | **sì** | no | no | no | dipende |
| Task bloccati rilevati e segnalati | **sì** | no | notifiche | no | dipende |
| Ti interrompe solo per le decisioni vere | **sì** | a ogni domanda | a ogni domanda | a ogni domanda | dipende |
| Rilascia nella *tua* produzione | **sì** | a mano | a mano | a mano | raramente |
| Costo | il tuo piano Claude + un LLM per Hermes | il tuo piano | il tuo piano | il tuo piano | per postazione o a consumo |
| Licenza | MIT | per lo più open source | open source | open source | proprietaria |

¹ ad es. claude-code-telegram, CCBot, Claude Telegram Bot Bridge. ² ad es. Happy, Omnara.
³ ad es. Claude Squad, Vibe Kanban. ⁴ ad es. Codex cloud, agenti in background di Cursor, GitHub Copilot coding agent, Devin.
Le colonne descrivono la configurazione tipica di ciascuna categoria a ottobre 2026. I singoli progetti
cambiano in fretta, quindi controlla la loro documentazione.

**Quando è meglio scegliere altro:**
- vuoi fare pair programming dal telefono in tempo reale, riga per riga (un client mobile è più semplice);
- non hai un server Linux, oppure ti serve macOS o Docker (non ancora supportati);
- il tuo team ha bisogno di una chat condivisa multiutente (telegram-harness è pensato per un solo
  proprietario per bot).

## Cosa serve

- Un server Linux con systemd (meglio una VM o un utente dedicati: gli agenti girano con
  `--dangerously-skip-permissions`).
- Un token per il bot Telegram da [@BotFather](https://t.me/BotFather) e il tuo user id numerico
  (chiedilo a [@userinfobot](https://t.me/userinfobot)).
- Un abbonamento Claude o una API key per Claude Code.
- Un provider LLM per Hermes (OpenRouter, Anthropic, OpenAI, Nous Portal, …).
- python3 ≥ 3.11, git, curl; Node.js ≥ 20 + npm (per OpenSpec); `libatomic1` (le immagini minimali di
  Ubuntu non la includono: `sudo apt-get install -y libatomic1`).

## Avvio rapido

```bash
git clone https://github.com/nhype/telegram-harness
cd telegram-harness
./install.sh
```

L'installer chiede il nome del progetto, il percorso del repo, il tuo id Telegram e il token del bot
(l'input resta nascosto), installa quello che manca (Herdr, Hermes Agent, Claude Code, OpenSpec, lean-ctx,
dai rispettivi installer ufficiali), configura un profilo Hermes per il progetto e avvia i servizi. Poi:

```bash
claude                      # accedi a Claude Code (basta una volta)
hermes -p <profilo> model   # scegli il modello su cui gira Hermes
bin/harness doctor <profilo>
```

e invia `/start` al tuo bot. Non spostare il clone: gli script e i plugin del profilo sono link che puntano
lì dentro. Installazione non interattiva (il token viene letto senza finire nella cronologia della shell):

```bash
read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN
./install.sh --yes --project Acme --repo /srv/acme --owner-id 123456789
```

Ogni progetto vuole un token del bot tutto suo: due profili Hermes non possono condividere lo stesso bot.

Esegui `./install.sh --help` per vedere tutte le opzioni (`--dry-run` mostra il piano senza modificare
nulla).

## Uso quotidiano

Scrivi al bot come faresti con un collega:

- *"Aggiungi l'export CSV alla pagina dei report"* → il bot avvia un agente, che scrive una proposta
  OpenSpec, poi la implementa, la testa, la rilascia e la archivia; quando è in produzione ti arriva un
  breve messaggio.
- *"a che punto siamo?"* → cosa è fatto, cosa manca, se gli agenti stanno lavorando o sono in attesa, cosa
  serve da te.
- Quando il bot ti chiede qualcosa (succede di rado), rispondi a parole tue: *"sì"*, *"2"*, *"vai pure"* —
  la risposta arriva all'agente che ha fatto la domanda.

Spiega al controller come si fa il deploy del tuo progetto e come verificarlo con uno smoke test: modifica
le **Project notes** in fondo a `~/.hermes/profiles/<profilo>/skills/harness-controller/SKILL.md`. Dopo una
tua modifica, l'installer non sovrascrive più quel file.

## Aggiornamento

```bash
git pull
./install.sh --profile <profilo>   # riusa le risposte salvate; puoi rilanciarlo senza rischi
```

## Disinstallazione

```bash
bin/harness uninstall-services <profilo>   # bridge + route webhook; il gateway Hermes condiviso resta
hermes profile delete <profilo>            # facoltativo: il profilo e la sua cronologia
```

## Componenti

| Componente | Ruolo | Versione testata | Licenza |
|---|---|---|---|
| [Herdr](https://herdr.dev) | workspace da terminale per agenti, eventi di stato | 0.8.0 | Apache-2.0 |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | gateway Telegram, chat + run del controller | 0.21.5 (d795726f) | MIT |
| [Claude Code](https://docs.claude.com/en/docs/claude-code) | l'agente di coding | 2.1.288 | commerciale |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | workflow delle modifiche guidato dalle specifiche | 1.13.1 | MIT |
| [lean-ctx](https://github.com/yvgude/lean-ctx) | compressione del contesto per agenti | 3.10.2 | Apache-2.0 |

L'harness vero e proprio è fatto di bridge, registro dei task, script della route, tre plugin per Hermes,
due skill e l'installer. I plugin applicano patch ad alcune parti interne del gateway di Hermes, quindi con
un Hermes molto più recente potrebbe servire un aggiornamento anche qui; dopo `hermes update`, esegui
`bin/harness test-plugins` (i test dei plugin, eseguiti nel runtime di Hermes stesso).

## Documentazione (in inglese)

- [Architecture](docs/architecture.md) — componenti, flusso degli eventi, attese e ricontrolli
- [Configuration](docs/configuration.md) — `herdr-pipeline.json`, impostazioni del profilo, Project notes
- [Manual install](docs/manual-install.md) — i passaggi dell'installer sotto forma di comandi
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)

## Licenza

[MIT](LICENSE)
