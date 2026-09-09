# AI Radar

AI Radar 是一个面向“今天 AI 圈又出现了什么”的资讯聚合工具。它定时采集国内外公开信息源，将项目、模型、论文、资讯、讨论和 X 热帖统一清洗、去重、排序，然后通过浏览器看板或 macOS 悬浮球展示。

详细的模块边界、数据流、刷新生命周期和 API 说明见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 功能概览

- 24 个国内外信息源并发采集，单个来源失败不会阻塞全局。
- SQLite 本地存储，按标题指纹跨来源去重，自动清理 30 天前数据。
- 按新鲜度、互动量、跨源确认和来源质量计算综合分。
- X 热帖采用“重点账号 + 全网观点长文”策略，读取 Article/长帖正文以及浏览、点赞、转发、收藏指标；默认每天最多一次、最多 70 条付费资源。
- “讨论”默认查看近 24 小时，优先呈现时效性强的 X 观点长文和高互动帖子，按真实传播数据排序，只有高互动社区讨论才作为补充。
- “论文”默认查看近 7 天的社区热门论文，只保留与 AI 模型强相关的研究，Hugging Face 赞同、讨论数和 GitHub Star 共同参与排序。
- 其他免费来源每次最多保留 30 条，聚合看板不会再用 50 条全局上限截断它们。
- Web 看板支持时间范围、地区、内容类型和关键词筛选。
- DeepSeek 基于最近 24 小时已采集内容生成“今日情报”，提炼方向、关键信号和内容选题。
- macOS 原生悬浮球支持拖动、右键退出、分类筛选、打开原文和英文翻译；会注册为登录项，并在本地隧道断开时自动重连。
- 翻译只在用户点击时调用阿里云百炼 DashScope，不影响采集成本。

## 目录

```text
ai-radar/
├── app.py                         # HTTP 服务、API、刷新调度
├── radar/
│   ├── sources.py                 # 来源与 X 账号配置
│   ├── collector.py               # 各来源采集器与统一标准化
│   ├── store.py                   # SQLite 存储、去重、排序、状态
│   └── translator.py              # DashScope 翻译适配器
├── web/                           # 零构建依赖的浏览器看板
├── desktop/                       # macOS AppKit + WebKit 悬浮球
├── deploy/                        # systemd 与 macOS 隧道模板
├── tests/                         # Python 单元测试
├── data/                          # 运行时数据库（不提交）
├── .env.example                   # 环境变量模板
└── docs/ARCHITECTURE.md           # 详细架构文档
```

## 快速开始

需要 Python 3.11 或更高版本。服务端只使用 Python 标准库，不需要安装第三方包。

```bash
cd ai-radar
cp .env.example .env.local
python3 app.py
```

访问 <http://127.0.0.1:8765>。首次启动会在后台采集，通常需要 10-30 秒。

常用命令：

```bash
python3 app.py serve                         # 启动服务
python3 app.py refresh                       # 手动执行一次采集并打印结果
AI_RADAR_PORT=9000 python3 app.py            # 修改监听端口
AI_RADAR_REFRESH_MINUTES=60 python3 app.py  # 修改普通来源刷新周期
```

桌面端需要 macOS 13+ 和 Xcode Command Line Tools：

```bash
cd desktop
./build.sh
open build/AIRadarDesktop.app
```

桌面 App 默认访问 `http://127.0.0.1:8765`。如果服务部署在远程服务器，请先建立 SSH 隧道，或修改 `desktop/AIRadarDesktop.m` 中的 API 地址后重新构建。默认榜单读取最近 24 小时；“讨论”单独读取近 24 小时高热观点池，“论文”单独读取近 7 天社区热门池。

## 信息源

当前内置 24 个来源：

| 类型 | 来源 |
| --- | --- |
| 项目/模型 | GitHub LLM 新项目、Hugging Face 模型、Hugging Face Spaces |
| 论文 | Hugging Face Daily Papers、arXiv AI/ML/CL |
| 社区讨论 | Hacker News、DEV Community AI、Reddit MachineLearning、Reddit LocalLLaMA |
| 海外媒体/机构 | TechCrunch AI、VentureBeat AI、The Decoder、OpenAI News、Google DeepMind、NVIDIA Deep Learning、Simon Willison、Import AI |
| 国内媒体/社区 | 量子位、机器之心、Solidot、HelloGitHub、少数派、阮一峰网络日志 |
| 社交平台 | X AI 热帖 |

来源定义集中在 `radar/sources.py`。RSS、JSON、GitHub、Hugging Face、Hacker News、arXiv、DEV、Reddit 和 X 分别使用独立适配逻辑。

## X API 配置与成本控制

将 X Bearer Token 放在本地 `.env.local` 或服务器环境变量中：

```dotenv
X_BEARER_TOKEN=your-token
X_MAX_RESULTS=70
X_MAX_CALLS_PER_DAY=1
X_MIN_INTERVAL_MINUTES=1440
# 直连 api.x.com 不通时再填写：
# X_HTTPS_PROXY=http://127.0.0.1:7890
```

一次默认采集最多读取 50 个 tweet resources：前两组重点账号各预留最多 15 条，最后 20 条从带链接的 AI 观点内容中发现。系统会排除转发和回复，请求 X Article、长帖正文及公开互动数据，并在 SQLite 中记录每日请求次数和资源数。X Article、长帖，以及达到浏览量与互动门槛的观点帖会进入“讨论”；排序综合真实浏览量、点赞、转发、引用、回复、收藏、发布时间和重点账号加权。为避免额外的 User Read 费用，采集不会展开作者用户对象。结果不足 50 条时保留实际数量，不使用无关内容硬凑。X 套餐的实际单条价格和计费规则以 X Developer Console 当前页面为准，界面中的费用只是按配置估算。

重点账号和关键词在 `radar/sources.py` 中维护；修改后无需改采集器。

## 翻译配置

翻译和今日总结都使用阿里云百炼 DashScope 的 OpenAI 兼容接口，默认模型为 `deepseek-v4-flash-0731`：

```dotenv
DASHSCOPE_API_KEY=your-dashscope-key
TRANSLATION_MODEL=deepseek-v4-flash-0731
SUMMARY_MODEL=deepseek-v4-flash-0731
SUMMARY_CACHE_HOURS=6
SUMMARY_MAX_ITEMS=90
# DASHSCOPE_CHAT_URL=https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
```

`GET /api/daily-summary` 只分析 AI Radar 最近 24 小时已经采集并排序的数据，不额外抓取信息。结果按北京时间每天缓存，并在缓存期内复用；浏览器看板中的“重新总结”按钮会显式触发一次新生成。输入会先做来源平衡抽样，避免单一平台数量过多导致总结方向失真。

不要把 API Key 写入源码、plist、截图或 Git 历史。建议使用控制台的环境变量、密钥管理服务，并为公开过的 Key 重新生成。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/items?range=24h&region=all&type=all&limit=100` | 获取排序后的资讯；时间范围支持 `24h/3d/7d` |
| `GET` | `/api/sources` | 获取来源状态与 X 计费日资源数 |
| `GET` | `/api/summary` | 获取总数、今日数、更新时间和刷新状态 |
| `GET` | `/api/daily-summary` | 获取或生成最近 24 小时的 DeepSeek 今日情报 |
| `POST` | `/api/daily-summary` | 使用最新采集内容强制重新生成今日情报 |
| `POST` | `/api/refresh` | 异步启动一次全量刷新 |
| `POST` | `/api/translate` | 请求体 `{"text":"英文内容"}`，返回译文 |

普通客户端只读 `/api/items`、`/api/sources`、`/api/summary` 和带缓存的 `/api/daily-summary`；只有用户明确刷新时才调用 `/api/refresh`。桌面客户端不会额外触发 X 采集。

## 服务器部署

生产服务默认监听 `127.0.0.1:8765`，推荐通过 SSH 隧道访问，不直接暴露端口：

```bash
sudo install -m 644 deploy/ai-radar.service /etc/systemd/system/ai-radar.service
sudo systemctl daemon-reload
sudo systemctl enable --now ai-radar
sudo systemctl status ai-radar
```

将环境变量保存到 systemd `EnvironmentFile` 指定的 `/etc/ai-radar/env`，并确保该文件权限为 `600`。`deploy/ai-radar.service` 默认假设项目路径为 `/opt/ai-radar`、运行用户为 `ubuntu`；换服务器时请按实际用户和路径调整。真实环境文件、数据库、SSH 密钥和个性化 plist 都已被 `.gitignore` 排除，禁止提交到仓库。

macOS 隧道模板是 `deploy/com.ai-radar.tunnel.plist.example`。复制后，将 `__SSH_KEY_PATH__` 和 `__SSH_USER_AND_HOST__` 替换为自己的值，再安装到 `~/Library/LaunchAgents/`：

```bash
cp deploy/com.ai-radar.tunnel.plist.example ~/Library/LaunchAgents/com.ai-radar.tunnel.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ai-radar.tunnel.plist
```

## macOS 悬浮球自启动

先构建并安装 App，再复制 `desktop/com.ai-radar.desktop.plist.example`，把 `__APP_EXECUTABLE__` 替换成 App 内可执行文件的绝对路径：

```bash
./desktop/build.sh
mkdir -p "$HOME/Applications"
cp -R desktop/build/AIRadarDesktop.app "$HOME/Applications/"
cp desktop/com.ai-radar.desktop.plist.example "$HOME/Library/LaunchAgents/com.ai-radar.desktop.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.ai-radar.desktop.plist"
```

右键悬浮球可退出当前进程；由于模板只设置 `RunAtLoad`，退出后不会被 `KeepAlive` 立即拉起，下次登录仍会自动启动。

## 测试与质量检查

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py radar tests
```

macOS 构建语法检查：

```bash
clang -fsyntax-only -fobjc-arc \
  -framework AppKit -framework WebKit -framework Foundation \
  desktop/AIRadarDesktop.m
```

## 上传 GitHub 前检查

1. 确认 `.env.local`、`data/*.db`、`desktop/build/` 没有被 Git 跟踪。
2. 删除 README、日志、截图和命令历史里的真实 Bearer Token、DashScope Key、服务器私钥路径。
3. 把 `deploy/*.example` 和 `desktop/*.example` 中的占位符替换只放在本机副本，不要回写模板。
4. 如果在上一级“思考”目录执行 Git，先单独创建 `ai-radar` 仓库；否则会把同级的其他项目一起上传。

本项目目前没有提交任何密钥。若某个 Key 曾经公开出现在聊天、日志或截图中，应在对应控制台立即撤销并重新生成。


本轮实际行为及尚未实现项见 [内容筛选第一轮](docs/CONTENT_ITERATION_1.md)。今日总结已改为仅输入最近 24 小时筛选后的全部 X 条目，取消旧的混合来源抽样；指标当前仍在每日采集时更新。
