# telegram-harness

[English](README.md) · [Русский](README.ru.md)

**Агенты Claude Code под управлением из Telegram.** Вы пишете задачу своему боту. Hermes Agent запускает
агента Claude Code в терминальной панели Herdr и ведёт его по жизненному циклу OpenSpec: план → код →
тесты → деплой → архив. Вам он пишет, только когда нужно решение или готов результат.

```
 вы ── Telegram ──▶ Hermes Agent (хостовый gateway) ──▶ ваш профиль
                       │  чат: принимает задачи, отвечает на «как дела?»   скилл: harness-delivery
                       │  webhook-маршрут ◀── события ── bridge ◀── сервер Herdr (статусы панелей)
                       │  запуск controller на каждое событие              скилл: harness-controller
                       ▼
                    панели Herdr: агенты Claude Code работают в вашем репозитории (OpenSpec + lean-ctx)
```

- **Herdr** запускает агентов в терминальных панелях и сообщает о каждой смене статуса (работает /
  простаивает / закончил).
- **Bridge** (`harness/scripts/herdr_event_bridge.py`) сглаживает эти события и будит по одному запуску
  controller на задачу. Watchdog будит его снова, если задача зависла или истекло ожидание.
- **Hermes Agent** — менеджер. Чат запускает задачи, controller читает панель, решает следующий шаг и
  даёт агенту указание. Технические вопросы он решает сам. Вас спрашивает только про деньги,
  необратимые действия, продуктовые решения и ваши личные аккаунты.
- **OpenSpec** даёт каждой задаче план, чеклист и архив, **lean-ctx** держит контекст агентов
  компактным.

## Что понадобится

- Linux-сервер с systemd. Лучше отдельная VM или отдельный пользователь: агенты работают с
  `--dangerously-skip-permissions`.
- Токен Telegram-бота от [@BotFather](https://t.me/BotFather) и ваш числовой user id (его подскажет
  [@userinfobot](https://t.me/userinfobot)).
- Подписка Claude или API-ключ для Claude Code.
- LLM-провайдер для Hermes: OpenRouter, Anthropic, OpenAI, Nous Portal и т. п.
- python3 ≥ 3.11, git, curl; Node.js ≥ 20 + npm (для OpenSpec); `libatomic1` (в минимальных образах
  Ubuntu её нет: `sudo apt-get install -y libatomic1`).

## Быстрый старт

```bash
git clone https://github.com/nhype/telegram-harness
cd telegram-harness
./install.sh
```

Установщик:
- спросит имя проекта, путь к репозиторию, ваш Telegram id и токен бота (ввод скрыт);
- поставит недостающее официальными установщиками: Herdr, Hermes Agent, Claude Code, OpenSpec, lean-ctx;
- создаст для проекта профиль Hermes и запустит сервисы.

После этого:

```bash
claude                      # один раз войти в Claude Code
hermes -p <профиль> model   # выбрать модель, на которой работает Hermes
bin/harness doctor <профиль>
```

и отправить боту `/start`. Установка без вопросов:

```bash
TELEGRAM_BOT_TOKEN=... ./install.sh --yes --project Acme --repo /srv/acme --owner-id 123456789
```

Все параметры — в `./install.sh --help`. С `--dry-run` установщик только показывает план, ничего не
меняя.

## Каждый день

Пишите боту как коллеге:

- *«Добавь экспорт отчётов в CSV»*. Бот запустит агента: тот напишет предложение OpenSpec, реализует
  его, прогонит тесты, задеплоит и заархивирует. Когда изменение окажется в проде, придёт короткое
  сообщение.
- *«Как дела?»*. Что сделано, что осталось, работают агенты или ждут, что нужно от вас.
- Если бот что-то спросит (это редкость), отвечайте просто: *«да»*, *«2»*, *«давай»*. Ответ уйдёт тому
  агенту, который спрашивал.

Расскажите controller, как деплоится и проверяется ваш проект. Для этого заполните раздел **Project notes**
в конце `~/.hermes/profiles/<профиль>/skills/harness-controller/SKILL.md`. После вашей правки установщик
этот файл больше не перезаписывает.

## Обновление

```bash
git pull
./install.sh --profile <профиль>   # берёт сохранённые ответы; можно запускать повторно
```

## Удаление

```bash
bin/harness uninstall-services <профиль>   # bridge и webhook-маршрут; общий gateway Hermes остаётся
hermes profile delete <профиль>            # по желанию: сам профиль и его история
```

## Компоненты

| Компонент | Роль | Проверенная версия | Лицензия |
|---|---|---|---|
| [Herdr](https://herdr.dev) | терминальное рабочее пространство агентов, события статусов | 0.8.0 | Apache-2.0 |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Telegram-gateway, чат и запуски controller | 0.21.4 | MIT |
| [Claude Code](https://docs.claude.com/en/docs/claude-code) | агент-программист | 2.1.288 | коммерческая |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | работа с изменениями через спецификации | 1.13.1 | MIT |
| [lean-ctx](https://github.com/yvgude/lean-ctx) | сжатие контекста агентов | 3.10.2 | Apache-2.0 |

Сам harness — это bridge, реестр задач, route-скрипт, три плагина Hermes, два скилла и установщик.
Плагины подменяют несколько внутренних функций gateway Hermes, поэтому на заметно более новой версии
Hermes здесь может понадобиться обновление. Это покажет `bin/harness doctor`.

## Документация (на английском)

- [Architecture](docs/architecture.md) — компоненты, поток событий, ожидания и перепроверки
- [Configuration](docs/configuration.md) — `herdr-pipeline.json`, настройки профиля, Project notes
- [Manual install](docs/manual-install.md) — шаги установщика вручную
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)

## Лицензия

[MIT](LICENSE)
