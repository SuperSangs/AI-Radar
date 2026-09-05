# AI Radar 架构说明

## 1. 系统定位

AI Radar 是一个“采集、清洗、排序、展示”一体的 AI 资讯聚合工具。服务器负责定时抓取公开信息并保存到 SQLite；Web 页面和 macOS 悬浮球都只读取已经采集的数据。这样打开客户端不会触发 X API，也不会因为某个信息源变慢而阻塞界面。

```mermaid
flowchart LR
    S[公开信息源] --> C[radar/collector.py\n并发采集与标准化]
    C --> D[(SQLite\ndata/ai-radar.db)]
    D --> A[app.py\nHTTP API + 定时调度]
    A --> W[web/\n浏览器看板]
    A --> M[desktop/\nmacOS 悬浮球]
    A --> B[/api/daily-summary]
    B --> L[DashScope\nDeepSeek]
    M --> T[/api/translate]
    T --> L
```

## 2. 目录职责

| 路径 | 职责 |
| --- | --- |
| `app.py` | 标准库 HTTP 服务、静态文件、API 路由、后台刷新调度 |
| `radar/sources.py` | 信息源声明、来源类型、地区、权重、X 重点账号和查询语句 |
| `radar/collector.py` | RSS、GitHub、Hugging Face、Hacker News、arXiv、DEV、Reddit、X 适配器；清洗、分类、去重、并发刷新 |
| `radar/store.py` | SQLite schema、来源状态、刷新记录、X 请求/资源计数、条目 upsert、排序查询 |
| `radar/daily_summary.py` | 已采集条目的来源平衡抽样、DeepSeek 结构化总结、缓存复用和异常回退 |
| `radar/translator.py` | DashScope OpenAI 兼容接口调用和错误归一化 |
| `web/` | 浏览器版看板：筛选、搜索、来源状态、刷新状态 |
| `desktop/AIRadarDesktop.m` | AppKit + WebKit 原生悬浮球、右侧列表、拖动、右键退出、打开原文、翻译桥接 |
| `desktop/build.sh` | 使用 macOS `clang` 构建 `.app` |
| `deploy/ai-radar.service` | Linux systemd 服务模板 |
| `deploy/*.example` | macOS SSH 隧道和桌面自启动模板，不包含个人路径或服务器信息 |
| `tests/` | 采集、X 配额、去重、排序、翻译接口的单元测试 |
| `data/` | 运行时 SQLite 数据目录，已被 Git 忽略 |

## 3. 刷新生命周期

1. `app.py` 启动后台 `scheduler`。首次启动立即执行一次 `refresh_all`，之后按 `AI_RADAR_REFRESH_MINUTES` 调度。
2. `refresh_all` 用线程池并发处理所有 `SOURCES`。每个来源单独记录成功、条数、延迟和错误。
3. 采集器把不同格式的数据转成统一条目：标题、摘要、作者、原文 URL、发布时间、地区、内容分类、互动量、标签和元数据。
4. `Store.upsert_items` 按 `(source_key, external_id)` 幂等写入；标题指纹用于跨来源合并相同内容。
5. 查询时计算有效时间和综合分，返回排序后的榜单。超过 30 天的条目在刷新结束时清理。

## 4. 排名与去重

- 新鲜度：越接近当前时间分数越高。
- 互动量：点赞、评论、star、下载量等使用对数缩放，避免单个平台占满榜单。
- 跨源确认：同一标题被多个来源报道时增加分数。
- 来源优先级：X 内容整体最高；X 内部以浏览量和点赞、转发、引用、回复、收藏等传播数据为主，重点账号只提供小幅加权，其余来源再按综合热度排序。
- 去重键：优先使用标题指纹；同组保留信息质量最高的条目，同时展示来源数量。

项目、模型以“采集时间”和“发布时间”的较新者作为有效时间，避免旧项目首次进入 Radar 后立刻消失。X 内容只使用原始发布时间，防止重复采集把旧观点伪装成今日热点。

## 5. X 采集策略

`x-ai` 使用 X Recent Search，默认 `X_MAX_RESULTS=50`、每天最多 1 次、两次请求至少间隔 24 小时。一次采集最多消耗 50 个 tweet resources：

- 第 1 组：重点账号 1-15，最多 15 条。
- 第 2 组：重点账号 16-30，最多 15 条。
- 第 3 组：带链接的严格 AI 关键词观点发现，最多 20 条。

重点账号不足时可以分页补齐，但总量不会超过 50 条，也不会为了凑数加入不相关内容。查询排除纯转发和回复，按 relevancy 获取最近 7 天内的结果。严格关键词为 AI、LLM、GPT、Claude、Gemini、DeepSeek、Qwen、Cursor、Agent、MCP、Model、Coding、大模型、智能体、开源模型。采集器同时请求 `article`、`note_tweet` 和 `public_metrics`，将 X Article、长帖与达到传播阈值的观点帖归入“讨论”，并保留浏览、点赞、转发和收藏等指标供两端展示。它不会展开作者用户对象，避免额外 User Read 费用。所有 X 请求/资源计数写入 `source_cursors`，用于节流和成本估算。

聚合结果先按来源优先级分层，再在同层内按传播质量排序：X 高热观点、X 其他内容、其他免费来源。重点账号只作为质量加成，不能压过浏览和互动明显更高的全网观点。X 每次最多展示 50 条，其余每个来源分别最多展示 30 条。

## 6. API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/items?range=24h&region=all&type=all&limit=100` | 获取榜单；`range` 支持 `24h/3d/7d` |
| `GET` | `/api/sources` | 获取来源健康状态、最近条数和 X 资源计数 |
| `GET` | `/api/summary` | 获取总数、今日数、最后更新时间、刷新状态 |
| `GET` | `/api/daily-summary` | 读取缓存；必要时用 DeepSeek 生成今日情报 |
| `POST` | `/api/daily-summary` | 强制使用当前已采集数据重新生成今日情报 |
| `POST` | `/api/refresh` | 异步启动一次全量刷新 |
| `POST` | `/api/translate` | 请求体 `{"text":"..."}`，返回中文译文和模型名 |

服务默认只绑定 `127.0.0.1`。生产环境通过 SSH 本地端口转发访问，不直接把服务暴露到公网。

## 7. 两种客户端

### 浏览器看板

`web/index.html`、`web/app.js`、`web/styles.css` 是零构建依赖的静态前端。顶部“今日情报”展示 DeepSeek 对最近 24 小时内容的方向总结、关键信号和选题建议。页面会缓存不同时间范围的列表，只有用户点击“刷新”才调用 `/api/refresh`；普通轮询只读取摘要和来源状态。

### macOS 悬浮球

桌面 App 是原生 AppKit 程序，面板内部使用 WebKit 渲染轻量 HTML。启动后会注册为 macOS 登录项，并根据用户本地 LaunchAgent 配置自行维护 SSH 隧道；连接失败时会在后台重建隧道并快速重试。应用每分钟读取最近 24 小时的 `/api/items`、近 3 天的高热“讨论”池和带缓存的 `/api/daily-summary`，不调用采集刷新接口。Web 端点击“讨论”时也会自动切换到近 3 天。悬浮球顶部先显示今日方向，行动建议可展开；点击条目交给系统默认浏览器打开，英文内容通过 `/api/translate` 翻译并在卡片内展开。

## 8. 运行与部署边界

- 本地开发：`python3 app.py`，只需要 Python 3.11+，服务端无第三方 Python 依赖。
- Linux：systemd 以非 root 用户运行，写权限仅开放给 `data/`，服务默认绑定回环地址。
- macOS：需要 Xcode Command Line Tools，使用 `desktop/build.sh` 编译；SSH 隧道和桌面自启动使用 `deploy/*.example` 与 `desktop/*.example`，安装前必须替换占位符。
- DeepSeek：翻译仅在点击时调用；今日总结默认缓存 6 小时且只使用已采集条目，不参与采集和排序。

## 9. 已知限制

- RSS/API 的可用性取决于第三方站点，错误会记录在来源状态中并在下一轮重试。
- X Recent Search 受账号套餐、API 余额、限流和搜索结果质量影响；代码只保证资源上限和频率保护，不保证每天一定有 50 条。
- 当前 SQLite 适合单用户或低并发部署；多人使用、全文检索和长期历史建议迁移到 PostgreSQL/专用搜索引擎。
- 当前没有用户认证。若把 HTTP 服务暴露到公网，应先加反向代理、TLS 和认证。
