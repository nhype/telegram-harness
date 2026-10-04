<p align="center">
  <img src="docs/assets/logo.svg" alt="" width="96">
</p>

<h1 align="center">telegram-harness</h1>

<p align="center">
  <b>用 Telegram 管理你的编程智能体。</b><br>
  Claude Code 智能体负责规划、开发、测试、部署和验证，由“项目经理”推动它们持续前进，<br>
  只在需要你拍板时才来问你。
</p>

<p align="center">
  <a href="https://github.com/nhype/telegram-harness/actions/workflows/ci.yml"><img src="https://github.com/nhype/telegram-harness/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/platform-Linux%20%2B%20systemd-informational.svg" alt="Linux + systemd">
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#与同类工具对比">同类对比</a> ·
  <a href="docs/architecture.md">架构</a>
</p>

<p align="center"><sub>
  <a href="README.md">English</a> ·
  <a href="README.ru.md">Русский</a> ·
  <a href="README.es.md">Español</a> ·
  <b>简体中文</b> ·
  <a href="README.de.md">Deutsch</a> ·
  <a href="README.it.md">Italiano</a>
</sub></p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/demo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/demo-light.svg">
    <img src="docs/assets/demo-light.svg" alt="左侧是 Telegram 聊天：项目所有者提出要加 CSV 导出，机器人依次汇报方案、实现、部署和线上验证。右侧是 Herdr 窗格：编写智能体修改代码、运行测试并完成部署，评审智能体批准了这次变更，bridge 日志显示全程没有停滞。" width="100%">
  </picture>
</p>

**在 Telegram 中驱动 Claude Code 智能体。** 你把任务发给自己的机器人，Hermes Agent 就会在 Herdr 终端窗格中启动一个 Claude Code 智能体，带它走完 OpenSpec 全流程（规划 → 编码 → 测试 → 部署 → 归档），只在需要你做决定或有结果时才给你发消息。

```
 you ── Telegram ──▶ Hermes Agent (host gateway) ──▶ your profile
                        │  chat: takes tasks, answers "status?"         skill: harness-delivery
                        │  webhook route ◀── events ── bridge ◀── Herdr server (pane status)
                        │  controller run per event                     skill: harness-controller
                        ▼
                     Herdr panes: Claude Code agents working in your repo (OpenSpec + lean-ctx)
```

- **Herdr** 在终端窗格中运行智能体，并上报每一次状态变化（工作中 / 空闲 / 已完成）。
- **bridge**（事件桥，`harness/scripts/herdr_event_bridge.py`）对这些事件做防抖处理，每个任务同一时间只唤醒一次 controller（调度器）运行；任务停滞或等待结束时，watchdog（看门狗）会再次唤醒它。
- **Hermes Agent** 担任项目经理：聊天端负责启动任务；controller 端读取窗格内容、决定下一步，并向智能体下达指令。技术问题它自己拿主意，只有涉及花钱、不可逆操作、产品取舍以及你本人的账号时才会问你。
- **OpenSpec** 为每个任务提供方案、检查清单和归档；**lean-ctx** 让智能体的上下文保持精简。

## 为什么选择 telegram-harness

大多数工具只是让你在手机上和编程智能体*聊天*，你还是得一直盯着它。telegram-harness 给你配了一位**项目经理**：你说出需求，它负责把变更开发、测试、部署、验证并合并到位。只有真正需要你的时候，它才会来找你。

- **是项目经理，不是传话筒。** 在你两条消息之间，controller 会在智能体每走一步之后读取它的屏幕，推着它继续前进。它依据代码仓库和你过往的决策回答技术问题，按你设定的策略选择菜单选项，还能从 API 报错和上下文占满中自动恢复。
- **全生命周期，直达生产环境。** OpenSpec 规划 → 编码 → 测试 → 部署 → 线上冒烟测试 → 归档 → 合并到 `main`。“完成”意味着*已上线并通过验证*，而不是“代码写完了”。
- **只问该由你决定的事：** 花钱、不可逆操作、产品取舍、你的账号。其余事项它自行决定并向你汇报。在聊天里简单回一句“好”，也能准确送到提问的那个智能体手里。
- **绝不悄无声息地卡住。** 事件驱动的 watchdog 会捕捉停滞、已到期的等待和挂起的窗格。如果任务依然没有进展，bridge 会亲自给你发消息，即使 Hermes 已经宕机。
- **多一双眼睛把关。** 在任何改动上线之前，一个独立的评审智能体会检查方案和 diff，排查资金、隐私、安全、数据丢失和并发方面的风险。
- **你的服务器，你的订阅。** 代码和生产环境始终不离开你的机器，智能体跑在你自己的 Claude 套餐上。没有按任务计费的 SaaS 账单，也没有厂商提供的虚拟机。采用 MIT 许可证。
- **随时旁观，随时接管。** 每个智能体都运行在一个 Herdr 终端窗格里，你可以随时打开、查看，也可以直接在里面输入。
- **精简上下文。** lean-ctx 会压缩智能体读取的内容，让长任务装得下，成本也更低。
- **源自真实生产实践。** 它提炼自一套每天都在向生产环境交付变更的实际部署，拥有 320+ 个测试、CI，以及在干净容器中对接真实 Hermes 的集成测试。

## 与同类工具对比

| | **telegram-harness** | Claude Code 的 Telegram 机器人¹ | 移动客户端² | 本地编排工具³ | 云端编程智能体⁴ |
|---|---|---|---|---|---|
| 智能体运行在哪里 | 你的服务器 | 你的服务器 | 你的电脑 | 你的电脑 | 厂商云端 |
| 你如何指挥它们 | Telegram，直接说人话 | 在 Telegram 中与会话聊天 | 手机 / Web 应用 | 桌面 TUI 或看板 | Web、IDE、Slack、GitHub |
| 在你两条消息之间，谁推动智能体继续工作 | **controller** | 你 | 你 | 你 | 厂商的智能体 |
| 内置全流程：规划 → 编码 → 部署 → 线上验证 → 合并 | **是** | 否 | 否 | 否，由你审查并合并 | 通常止步于 pull request |
| 独立评审智能体 | **有** | 无 | 无 | 无 | 因产品而异 |
| 自动发现并上报卡住的任务 | **是** | 否 | 推送通知 | 否 | 因产品而异 |
| 只在真正需要决策时打扰你 | **是** | 每个问题都问你 | 每个问题都问你 | 每个问题都问你 | 因产品而异 |
| 部署到*你自己的*生产环境 | **是** | 手动 | 手动 | 手动 | 很少 |
| 成本 | 你的 Claude 套餐 + Hermes 所用的 LLM | 你的套餐 | 你的套餐 | 你的套餐 | 按席位或用量计费 |
| 许可证 | MIT | 大多开源 | 开源 | 开源 | 专有 |

¹ 例如 claude-code-telegram、CCBot、Claude Telegram Bot Bridge。² 例如 Happy、Omnara。³ 例如 Claude Squad、Vibe Kanban。⁴ 例如 Codex cloud、Cursor background agents、GitHub Copilot coding agent、Devin。各列描述的是截至 2026 年 10 月各类方案的典型形态。具体项目迭代很快，请以其官方文档为准。

**以下情况，其他方案可能更合适：**
- 你想在手机上逐行实时结对编程（移动客户端更简单）；
- 你没有 Linux 服务器，或者需要 macOS 或 Docker（暂不支持）；
- 你的团队需要多人共享的聊天（telegram-harness 的设计是每个机器人只服务一位所有者）。

## 准备工作

- 一台带 systemd 的 Linux 服务器（最好使用专用虚拟机或专用用户：智能体会以 `--dangerously-skip-permissions` 模式运行）。
- 从 [@BotFather](https://t.me/BotFather) 获取的 Telegram 机器人 token，以及你的数字用户 ID（可以问 [@userinfobot](https://t.me/userinfobot)）。
- 供 Claude Code 使用的 Claude 订阅或 API key。
- 供 Hermes 使用的 LLM 服务商（OpenRouter、Anthropic、OpenAI、Nous Portal 等）。
- python3 ≥ 3.11、git、curl；Node.js ≥ 20 + npm（OpenSpec 需要）；`libatomic1`（精简版 Ubuntu 镜像不带它，需执行 `sudo apt-get install -y libatomic1`）。

## 快速开始

```bash
git clone https://github.com/nhype/telegram-harness
cd telegram-harness
./install.sh
```

安装程序会询问项目名称、代码仓库路径、你的 Telegram ID 和机器人 token（输入时隐藏），自动安装缺失的组件（Herdr、Hermes Agent、Claude Code、OpenSpec、lean-ctx，均使用各自的官方安装程序），为项目创建一个 Hermes profile，并启动相关服务。接下来：

```bash
claude                      # 登录 Claude Code (只需一次)
hermes -p <profile> model   # 选择 Hermes 使用的模型
bin/harness doctor <profile>
```

最后给你的机器人发送 `/start`。请不要移动克隆下来的目录：profile 会把其中的脚本和插件链接进来。非交互式安装（读取 token 时不会让它留在 shell 历史记录里）：

```bash
read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN
./install.sh --yes --project Acme --repo /srv/acme --owner-id 123456789
```

每个项目都要使用独立的机器人 token：两个 Hermes profile 不能共用同一个机器人。

运行 `./install.sh --help` 查看全部选项（`--dry-run` 只打印执行计划，不做任何改动）。

## 日常使用

像跟同事说话一样给机器人发消息：

- *“给报表页加上 CSV 导出”* → 机器人启动一个智能体，由它撰写 OpenSpec 提案，再完成实现、测试、部署和归档；上线后你会收到一条简短的消息。
- *“进展如何？”* → 汇报已经完成了什么、还剩什么、智能体是在干活还是在等待，以及需要你做什么。
- 机器人偶尔会向你提问，直接用大白话回答即可：*“好”*、*“2”*、*“继续吧”*。回复会送达提问的那个智能体。

告诉 controller 你的项目如何部署、如何做冒烟测试：编辑 `~/.hermes/profiles/<profile>/skills/harness-controller/SKILL.md` 末尾的 **Project notes** 部分。一旦你修改过这个文件，安装程序就不会再覆盖它。

## 更新

```bash
git pull
./install.sh --profile <profile>   # 复用之前保存的回答, 可放心重复运行
```

## 卸载

```bash
bin/harness uninstall-services <profile>   # 移除 bridge 和 webhook 路由; 共享的 Hermes gateway 会保留
hermes profile delete <profile>            # 可选: 删除 profile 及其历史记录
```

## 组件

| 组件 | 作用 | 已验证版本 | 许可证 |
|---|---|---|---|
| [Herdr](https://herdr.dev) | 智能体的终端工作区，推送状态事件 | 0.8.0 | Apache-2.0 |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Telegram gateway（网关），负责聊天和 controller 运行 | 0.21.5 (d795726f) | MIT |
| [Claude Code](https://docs.claude.com/en/docs/claude-code) | 编程智能体 | 2.1.288 | 商业许可 |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | 规范驱动的变更工作流 | 1.13.1 | MIT |
| [lean-ctx](https://github.com/yvgude/lean-ctx) | 智能体上下文压缩 | 3.10.2 | Apache-2.0 |

harness 本身由 bridge、任务注册表、路由脚本、三个 Hermes 插件、两个 skill（技能）和安装程序组成。这些插件会给 Hermes gateway 的少量内部实现打补丁，所以换用新得多的 Hermes 版本时，这里可能也需要相应更新；执行 `hermes update` 之后，请运行 `bin/harness test-plugins`（在 Hermes 自身的运行时中执行插件测试）。

## 文档（英文）

- [Architecture](docs/architecture.md)：组件、事件流、等待与复查
- [Configuration](docs/configuration.md)：`herdr-pipeline.json`、profile 设置、Project notes
- [Manual install](docs/manual-install.md)：把安装程序的各个步骤拆成手动命令
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)

## 许可证

[MIT](LICENSE)
