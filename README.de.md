<p align="center">
  <img src="docs/assets/logo.svg" alt="" width="96">
</p>

<h1 align="center">telegram-harness</h1>

<p align="center">
  <b>Deine Coding-Agenten, gesteuert über Telegram.</b><br>
  Claude-Code-Agenten planen, bauen, testen, deployen und verifizieren. Ein Manager hält sie am Laufen<br>
  und fragt dich nur, wenn die Entscheidung wirklich bei dir liegt.
</p>

<p align="center">
  <a href="https://github.com/nhype/telegram-harness/actions/workflows/ci.yml"><img src="https://github.com/nhype/telegram-harness/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/platform-Linux%20%2B%20systemd-informational.svg" alt="Linux + systemd">
</p>

<p align="center">
  <a href="#schnellstart">Schnellstart</a> ·
  <a href="#vergleich">Vergleich</a> ·
  <a href="docs/architecture.md">Architektur</a>
</p>

<p align="center"><sub>
  <a href="README.md">English</a> ·
  <a href="README.ru.md">Русский</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <b>Deutsch</b> ·
  <a href="README.it.md">Italiano</a>
</sub></p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/demo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/demo-light.svg">
    <img src="docs/assets/demo-light.svg" alt="Links ein Telegram-Chat: Der Besitzer wünscht sich einen CSV-Export, der Bot meldet Plan, Umsetzung, Deploy und Live-Check. Rechts Herdr-Panes: Der Autor-Agent ändert, testet und deployt, der Reviewer-Agent gibt die Änderung frei, und das Bridge-Log zeigt keinen Stillstand." width="100%">
  </picture>
</p>

**Claude-Code-Agenten direkt aus Telegram steuern.** Du schreibst deinem Bot eine Aufgabe. Hermes Agent
startet einen Claude-Code-Agenten in einem Herdr-Terminal-Pane, führt ihn durch den OpenSpec-Lebenszyklus
(Plan → Code → Test → Deploy → Archiv) und meldet sich bei dir nur, wenn eine Entscheidung ansteht oder ein
Ergebnis vorliegt.

```
 du ── Telegram ──▶ Hermes Agent (Host-Gateway) ──▶ dein Profil
                       │  Chat: nimmt Aufgaben an, beantwortet „Status?“     Skill: harness-delivery
                       │  Webhook-Route ◀── Events ── Bridge ◀── Herdr-Server (Pane-Status)
                       │  ein Controller-Lauf pro Event                      Skill: harness-controller
                       ▼
                    Herdr-Panes: Claude-Code-Agenten arbeiten in deinem Repo (OpenSpec + lean-ctx)
```

- **Herdr** führt die Agenten in Terminal-Panes aus und meldet jede Statusänderung (arbeitet / im Leerlauf /
  fertig).
- **Die Bridge** (`harness/scripts/herdr_event_bridge.py`) entprellt diese Events und weckt pro Aufgabe
  immer nur einen Controller-Lauf; ein Watchdog weckt den Controller erneut, wenn eine Aufgabe ins Stocken
  gerät oder eine Wartezeit abläuft.
- **Hermes Agent** ist der Manager: Die Chat-Seite startet Aufgaben, die Controller-Seite liest das Pane,
  entscheidet über den nächsten Schritt und gibt dem Agenten die nächste Anweisung. Technische Fragen klärt
  er selbst; dich fragt er nur bei Geld, unumkehrbaren Aktionen, Produktentscheidungen und deinen eigenen
  Accounts.
- **OpenSpec** gibt jeder Aufgabe einen Plan, eine Checkliste und ein Archiv; **lean-ctx** hält den Kontext
  der Agenten schlank.

## Warum telegram-harness

Mit den meisten Tools kannst du vom Handy aus mit einem Coding-Agenten *chatten* – babysitten musst du ihn
trotzdem. telegram-harness gibt dir einen **Manager**: Du sagst, was du willst, und er sorgt dafür, dass die
Änderung gebaut, getestet, deployt, verifiziert und gemergt wird. Er meldet sich nur, wenn er dich wirklich
braucht.

- **Ein Manager, kein Durchreicher.** Zwischen deinen Nachrichten liest ein Controller nach jedem Schritt
  den Bildschirm des Agenten und hält ihn in Bewegung. Technische Fragen beantwortet er anhand des Repos und
  deiner früheren Entscheidungen, wählt Menüoptionen nach deinen Vorgaben und bringt den Agenten nach
  API-Fehlern oder vollem Kontext wieder in Gang.
- **Der komplette Lebenszyklus, bis in Produktion.** OpenSpec-Plan → Code → Tests → Deploy → Live-Smoke-Test
  → Archiv → Merge in `main`. „Fertig“ heißt *live und verifiziert*, nicht „Code geschrieben“.
- **Er fragt nur, was du entscheiden musst:** Geld, unumkehrbare Aktionen, Produktentscheidungen, deine
  Accounts. Alles andere entscheidet er selbst und berichtet dir davon. Ein schlichtes „ja“ im Chat landet
  bei genau dem Agenten, der gefragt hat.
- **Bleibt nie still hängen.** Ein eventgesteuerter Watchdog erkennt Stillstand, abgelaufene Wartezeiten und
  hängende Panes. Kommt eine Aufgabe trotzdem nicht voran, schreibt dir die Bridge selbst – auch wenn Hermes
  ausgefallen ist.
- **Ein zweites Paar Augen.** Ein unabhängiger Reviewer-Agent prüft Plan und Diff auf Risiken bei Geld,
  Datenschutz, Sicherheit, Datenverlust und Nebenläufigkeit, bevor irgendetwas live geht.
- **Dein Server, dein Abo.** Code und Produktion verlassen nie deine Maschine, und die Agenten laufen über
  deinen eigenen Claude-Plan. Keine SaaS-Rechnung pro Aufgabe, keine VM eines Anbieters. MIT-lizenziert.
- **Jederzeit zuschauen oder übernehmen.** Jeder Agent lebt in einem Herdr-Terminal-Pane, das du öffnen,
  mitlesen und in das du selbst tippen kannst.
- **Schlanker Kontext.** lean-ctx komprimiert, was die Agenten lesen – so passen auch lange Aufgaben in den
  Kontext und kosten weniger.
- **Aus der Praxis entstanden.** Herausgelöst aus einem Setup, das jeden Tag Änderungen in Produktion
  ausliefert. Mit über 320 Tests, CI und einem Integrationstest gegen eine echte Hermes-Instanz in einem
  sauberen Container.

## Vergleich

| | **telegram-harness** | Telegram-Bots für Claude Code¹ | Mobile Clients² | Lokale Orchestratoren³ | Cloud-Coding-Agenten⁴ |
|---|---|---|---|---|---|
| Wo die Agenten laufen | dein Server | dein Server | dein Rechner | dein Rechner | Cloud des Anbieters |
| Wie du sie steuerst | Telegram, in ganz normaler Sprache | Telegram-Chat mit einer Session | Handy- oder Web-App | Desktop-TUI oder Board | Web, IDE, Slack, GitHub |
| Wer den Agenten zwischen deinen Nachrichten am Laufen hält | **der Controller** | du | du | du | der Agent des Anbieters |
| Plan → Code → Deploy → Live-Check → Merge, fest eingebaut | **ja** | nein | nein | nein, Review und Merge machst du | endet meist beim Pull Request |
| Unabhängiger Reviewer-Agent | **ja** | nein | nein | nein | unterschiedlich |
| Hängende Aufgaben werden erkannt und gemeldet | **ja** | nein | Benachrichtigungen | nein | unterschiedlich |
| Stört dich nur bei echten Entscheidungen | **ja** | bei jeder Frage | bei jeder Frage | bei jeder Frage | unterschiedlich |
| Deployt in *deine eigene* Produktion | **ja** | von Hand | von Hand | von Hand | selten |
| Kosten | dein Claude-Plan + ein LLM für Hermes | dein Plan | dein Plan | dein Plan | pro Nutzer oder nach Verbrauch |
| Lizenz | MIT | überwiegend Open Source | Open Source | Open Source | proprietär |

¹ z. B. claude-code-telegram, CCBot, Claude Telegram Bot Bridge. ² z. B. Happy, Omnara.
³ z. B. Claude Squad, Vibe Kanban. ⁴ z. B. Codex cloud, Hintergrund-Agenten von Cursor, GitHub Copilot coding agent, Devin.
Die Spalten beschreiben das typische Setup der jeweiligen Kategorie, Stand Oktober 2026. Einzelne Projekte
entwickeln sich schnell weiter, wirf also am besten einen Blick in ihre Doku.

**Wann etwas anderes besser passt:**
- Du willst live vom Handy aus pair-programmieren, Zeile für Zeile (da ist ein Mobile Client einfacher).
- Du hast keinen Linux-Server oder brauchst macOS bzw. Docker (wird noch nicht unterstützt).
- Dein Team braucht einen gemeinsamen Chat für mehrere Nutzer (telegram-harness ist für einen Besitzer pro
  Bot gebaut).

## Was du brauchst

- Einen Linux-Server mit systemd (am besten eine eigene VM oder einen eigenen User, denn die Agenten laufen
  mit `--dangerously-skip-permissions`).
- Ein Telegram-Bot-Token von [@BotFather](https://t.me/BotFather) und deine numerische User-ID
  (frag einfach [@userinfobot](https://t.me/userinfobot)).
- Ein Claude-Abo oder einen API-Key für Claude Code.
- Einen LLM-Anbieter für Hermes (OpenRouter, Anthropic, OpenAI, Nous Portal, …).
- python3 ≥ 3.11, git, curl; Node.js ≥ 20 + npm (für OpenSpec); `libatomic1` (fehlt in minimalen
  Ubuntu-Images: `sudo apt-get install -y libatomic1`).

## Schnellstart

```bash
git clone https://github.com/nhype/telegram-harness
cd telegram-harness
./install.sh
```

Der Installer fragt nach Projektname, Repo-Pfad, deiner Telegram-ID und dem Bot-Token (verdeckte Eingabe),
installiert alles, was fehlt (Herdr, Hermes Agent, Claude Code, OpenSpec, lean-ctx – jeweils über die
offiziellen Installer), richtet ein Hermes-Profil für das Projekt ein und startet die Dienste. Danach:

```bash
claude                      # einmalig bei Claude Code anmelden
hermes -p <profil> model    # Modell wählen, auf dem Hermes läuft
bin/harness doctor <profil>
```

und schick deinem Bot `/start`. Lass den Clone, wo er ist: Das Profil verweist per Link auf Skripte und
Plugins darin. Nicht-interaktive Installation (das Token wird eingelesen, ohne in deiner Shell-History zu
landen):

```bash
read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN
./install.sh --yes --project Acme --repo /srv/acme --owner-id 123456789
```

Nimm für jedes Projekt ein eigenes Bot-Token: Zwei Hermes-Profile können sich keinen Bot teilen.

Alle Optionen zeigt dir `./install.sh --help` (`--dry-run` gibt den Plan aus, ohne etwas zu ändern).

## Im Alltag

Schreib dem Bot wie einem Kollegen:

- *„Füg der Berichtsseite einen CSV-Export hinzu“* → Der Bot startet einen Agenten, der einen
  OpenSpec-Vorschlag schreibt und ihn dann umsetzt, testet, deployt und archiviert; sobald die Änderung live
  ist, bekommst du eine kurze Nachricht.
- *„Status?“* → was erledigt ist, was noch fehlt, ob die Agenten arbeiten oder warten und was von dir
  gebraucht wird.
- Wenn der Bot etwas fragt (selten), antworte ganz normal: *„ja“*, *„2“*, *„leg los“* – die Antwort geht an
  den Agenten, der gefragt hat.

Erklär dem Controller, wie dein Projekt deployt wird und wie man es per Smoke-Test prüft: Ergänze dazu die
**Project notes** am Ende von `~/.hermes/profiles/<profil>/skills/harness-controller/SKILL.md`. Sobald du
die Datei bearbeitet hast, überschreibt der Installer sie nie wieder.

## Aktualisieren

```bash
git pull
./install.sh --profile <profil>   # nutzt deine gespeicherten Antworten; gefahrlos wiederholbar
```

## Deinstallieren

```bash
bin/harness uninstall-services <profil>   # Bridge + Webhook-Route; das gemeinsame Hermes-Gateway bleibt
hermes profile delete <profil>            # optional: das Profil samt Verlauf
```

## Komponenten

| Komponente | Rolle | Getestete Version | Lizenz |
|---|---|---|---|
| [Herdr](https://herdr.dev) | Terminal-Workspace für Agenten, Status-Events | 0.8.0 | Apache-2.0 |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Telegram-Gateway, Chat- und Controller-Läufe | 0.21.5 (d795726f) | MIT |
| [Claude Code](https://docs.claude.com/en/docs/claude-code) | der Coding-Agent | 2.1.288 | kommerziell |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | spezifikationsgetriebener Workflow für Änderungen | 1.13.1 | MIT |
| [lean-ctx](https://github.com/yvgude/lean-ctx) | Kontextkompression für Agenten | 3.10.2 | Apache-2.0 |

telegram-harness selbst besteht aus der Bridge, der Task-Registry, dem Route-Skript, drei Hermes-Plugins,
zwei Skills und dem Installer. Die Plugins patchen ein paar Interna des Hermes-Gateways, daher kann eine
deutlich neuere Hermes-Version hier ein Update erfordern. Führ nach `hermes update` den Befehl
`bin/harness test-plugins` aus (die Plugin-Tests, direkt in der Hermes-eigenen Runtime).

## Dokumentation (auf Englisch)

- [Architecture](docs/architecture.md) – Komponenten, Event-Fluss, Wartezeiten und erneute Prüfungen
- [Configuration](docs/configuration.md) – `herdr-pipeline.json`, Profileinstellungen, Project notes
- [Manual install](docs/manual-install.md) – die Schritte des Installers als einzelne Befehle
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)

## Lizenz

[MIT](LICENSE)
