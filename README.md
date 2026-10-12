# btc-ahr999-bot

A local-first Bitcoin DCA research dashboard with synthetic demos, a SQLite decision journal, daily scheduling, and an optional environment-configured JSON webhook. It never places trades. AHR999 calculation is not implemented in this release.

作者：`baigao417`。许可证：MIT。

## 解决什么问题

把指标观察、研究草稿、人工确认或跳过串成可回看的本地记录。
默认仅使用虚构行情，预算为零，调度和外部网络访问关闭。
不读取钱包、交易所账户或个人持仓，不执行交易。

**名称保留历史项目名，但当前代码没有 AHR999 指标或其阈值策略。**
当前规则使用 200 日均线、365 日高点回撤和 30 日日收益年化波动；
不能把本版本描述成已完成 AHR999 机器人或经验证的投资策略。

## 核心机制

```text
合成演示 / 显式启用的公开行情
  -> market_data.py：日样本统计
  -> decision_engine.py：研究倍率、依据、演算金额
  -> storage.py：SQLite 快照、草稿、唯一调度键、事件
  -> app.py：本机 HTTP API -> static/：确认或跳过
  -> notifier.py：仅 live 模式可选 JSON webhook

scheduler.py：每日计划 -> 幂等调度键 -> 超时记 missed、不自动补跑
```

规则倍率为 0.75、1、1.25 或 1.5；高波动时上限为 0.75。
月预算除以四再乘倍率仅用于草稿演算，不代表真实下单周期或金额。
确认操作只修改本地草稿状态。通知仅包含草稿 ID、动作、倍率和状态，
不发送预算、演算金额或持仓。推送失败不撤销已保存草稿，也不自动重试。
Webhook 禁止自动 HTTP 重定向，防止把认证头转发至其他地址。

## 快速开始

需要 Python 3.10+。默认 UTC 模式和测试只用标准库，不需安装依赖。

```powershell
python -B -m unittest discover -s tests -v
python -B app.py --help
python -B app.py --port 8787
```

打开 `http://127.0.0.1:8787`。端口占用时换用 `--port 8788`。
首次启动生成合成草稿与 `data/app.db`；仓库不附带数据库或历史记录。
点“合成演示”可生成新草稿；预算初始为零，可自行修改演算设置。
用 `Ctrl+C` 停止服务。

## 配置

`.env.example` 仅供参考，程序**不会自动读取 `.env`**。
通过 shell 环境变量或部署工具注入配置，不把真实凭证写进源码。

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `BTC_DB_PATH` | `data/app.db` | 相对项目目录的数据库路径；也可指定绝对路径 |
| `BTC_ALLOW_LIVE` | `false` | 必须精确设为 `true` 才允许 live 行情和关联通知 |
| `BTC_RUN_MODE` | `demo` | 调度执行模式，可选 `demo` / `live` |
| `BTC_SCHEDULER_ENABLED` | `false` | 精确设为 `true` 才启动调度线程 |
| `BTC_WEBHOOK_URL` | 空 | 可选 HTTPS JSON 接收地址，空值禁用 |
| `BTC_WEBHOOK_TOKEN` | 空 | 可选 Bearer 凭证，仅用于请求头，不落数据库 |

```powershell
# 以下操作由使用者主动执行后才会启用外部行情访问。
$env:BTC_ALLOW_LIVE = 'true'
$env:BTC_RUN_MODE = 'live'
$env:BTC_SCHEDULER_ENABLED = 'true'
python -B app.py
```

日计划默认 `09:00 UTC`，宽限 15 分钟；界面可修改时间和演算预算。
`POST /api/settings` 还支持 `timezone` 和 `max_misfire_minutes`。
Windows 上非 UTC 时区通常需在虚拟环境中安装可选依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -B app.py
```

服务只绑定 `127.0.0.1`。API：`GET /health`、`GET /api/state`、
`POST /api/run`（`{"mode":"demo"}`）、`POST /api/settings`、
`POST /api/decisions/<id>/confirm` 和 `/skip`。写请求需 JSON 对象。
不把 webhook URL 或凭证写入设置接口；配置只能来自服务进程环境。

## 测试与边界

测试全部使用虚构数据、临时数据库和模拟外部请求；HTTP 检查仅连接本机。
临时文件在仓库范围创建并自动清理，不读取环境里的真实凭证。
在线行情、真实通知、收益效果、生产部署和浏览器视觉验收不在本地测试结论内。

## 已知限制

- 未实现 AHR999、Telegram 原生消息格式、自动交易或收益回测。
- 公开行情端点的可用性、限流、地区限制与使用条款需要使用者另行核验；本次发布准备未连接端点。
- 日样本统计要求至少 365 个样本，过滤当前未结束的日记录；不保证提供方历史数据连续、无陈旧或完全一致。两个提供方的报价币种和日采样口径存在差异。
- 草稿生成中失败的计划键仍被占用，需手动生成新的草稿；没有自动重试或跨日补跑。
- 无用户认证、TLS、多用户隔离、通知持久队列或交付回执。只适合受信任的本机环境，不要直接公网暴露。
- 数据库可能含使用者自己的预算、选择和运行记录。`data/` 已忽略，发布前仍需独立审查所有新增文件。
- 无随仓库分发的市场数据、图片或第三方代码。详见 `THIRD_PARTY_NOTICES.md`。

## 投资免责声明

本项目仅用于软件研究、教学和个人实验，不构成投资、交易、财务或法律建议。
示例数据完全虚构，规则未验证盈利能力；历史表现、研究倍率与草稿均不保证未来收益。
加密资产存在剧烈波动、流动性、平台及监管风险，可能损失全部投入。
请独立核验数据和适用规则，自行承担使用及决策责任；本软件按 MIT 许可证以现状提供。
