# DREA Thin Kernel v1.05

> **自主 AI 智能体的极简生命内核** — 11个原子工具，零人工干预，从代码到发布全自动

---

## 🤔 这是什么？你需不需要它？

**适合你，如果你在构建：**
- 自主 AI 智能体（Agent）系统
- 需要文件操作 + 代码执行 + 浏览器自动化的 AI 后端
- 多智能体协作框架（联邦同步）
- 需要可审计、可追溯的 AI 任务执行环境

**不适合你，如果你只需要：**
- 简单的 AI 对话聊天机器人（用 LangChain / OpenAI SDK 更合适）
- 纯 API 封装层（太重了）
- 现成的 RPA 工具（换 UiPath / Playwright 更直接）

---

## 🧠 设计原理

DREA Thin Kernel 遵循 **"薄内核、厚森林"** 哲学（Thin Kernel, Thick Forest）：

```
┌─────────────────────────────────────────────┐
│          上层：任意 AI 模型（LLM）            │
├─────────────────────────────────────────────┤
│     DREA Thin Kernel（本项目）                │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│   │  文件协议 │  │ 11原子工具│  │ 分层记忆  │  │
│   └──────────┘  └──────────┘  └──────────┘  │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│   │ 审计哈希链│  │ 涌现检测  │  │ 联邦同步  │  │
│   └──────────┘  └──────────┘  └──────────┘  │
├─────────────────────────────────────────────┤
│          下层：操作系统 / 文件系统             │
└─────────────────────────────────────────────┘
```

**核心思想：** AI 智能体通过操作 `.drea/` 目录下的文件完成所有状态管理，而不依赖数据库或网络服务。文件即协议，协议即接口。

---

## ⚡ 11个原子工具一览

| # | 工具名 | 作用 | 权限要求 |
|---|--------|------|----------|
| 1 | `file_patch` | 精准差异修改文件（类 git diff）| 本地写 |
| 2 | `file_list` | 列出目录文件树 | 本地读 |
| 3 | `memory_read` | 读取 L0-L5 分层记忆 | 本地读 |
| 4 | `memory_write` | 写入/更新分层记忆 | 本地写 |
| 5 | `code_run` | 执行代码（C0-C4 权限分级）| 可配置 |
| 6 | `ask_human` | 请求人类确认（高风险操作防护）| 无 |
| 7 | `web_scan` | 扫描网页并提取结构化内容 | 网络 |
| 8 | `fetch_url` | 下载 URL 内容（支持 HTTP/HTTPS）| 网络 |
| 9 | `DANGEROUS_PATTERNS` | 危险命令检测引擎（正则增强）| 无 |
| 10 | `browser_automate` | 零人工 Chrome/Edge 自动化（CDP 协议）| 浏览器 |
| 11 | `github_publish` | 零人工 GitHub 发布（建仓+上传+发布）| 网络+浏览器 |

---

## 🌟 核心特色

### 1️⃣ 文件协议优先（File Protocol First）
所有状态存储在 `.drea/` 目录，无需数据库。任务队列、记忆、审计链、联邦节点同步——全部通过文件读写完成。好处是：**可离线运行、可 git 版本控制、可直接人工检查**。

### 2️⃣ code_run 权限分级（C0-C4）
```
C0 = 只读沙箱（无网络、无写入）
C1 = 允许本地写入
C2 = 允许网络 + 写入
C3 = 允许外部 API
C4 = 完整系统权限（需显式授权）
```
AI 不能自行升级权限。每个代码执行任务必须声明所需的权限级别，系统自动阻断越权操作。

### 3️⃣ browser_automate（CDP 零干预浏览器控制）
通过 Chrome DevTools Protocol（CDP）直接控制已登录的浏览器，无需 WebDriver、无需插件：
- 自动发现运行中的 Chrome/Edge（端口 9222）
- 未启动则自动拉起（独立用户目录，不影响用户日常浏览）
- 支持 `Page.navigate` / `Runtime.evaluate` / `Page.captureScreenshot`

**典型场景：** 操控已登录的 GitHub 网页，自动生成 Personal Access Token（PAT），全程零人工介入。

### 4️⃣ github_publish（完整 GitHub 发布自动化）
凭据自愈链（Credential Self-Healing Chain）：
```
参数传入 → 环境变量 → 磁盘 token 文件 → 浏览器 OAuth 自举
```
一条指令完成：建仓 → 上传所有文件 → 创建 git tag → 发布 Release，不依赖 `git` 命令行工具，纯 GitHub REST API。

### 5️⃣ GeneGuard 双层防护
- **规则引擎**：40+ 正则匹配危险命令（`rm -rf /`、`format C:`、fork bomb 等）
- **意图分类器**：可接入本地 ML 模型，识别语义层面的恶意意图
- v1.05 新增：防止正则绕过（空格变换攻击）

### 6️⃣ L0-L5 分层记忆系统
```
L0 = 工作记忆（当前任务上下文）
L1 = 短期记忆（最近N条）
L2 = 情景记忆（时间序列事件）
L3 = 语义记忆（知识/概念）
L4 = 程序记忆（操作技能）
L5 = 元记忆（关于记忆的记忆）
```

---

## 🚀 快速开始（5分钟上手）

### 安装
```bash
git clone https://github.com/gitopal/drea-thin-kernel
cd drea-thin-kernel
pip install -e .
```

### 初始化
```bash
drea init          # 创建 .drea/ 目录结构
drea status        # 检查运行状态
```

### 创建并执行第一个任务
```bash
# 创建一个 echo 任务
drea task --type echo --input '{"message": "Hello DREA v1.05"}'

# 执行任务
drea run-once

# 查看结果
drea status
```

### 使用 browser_automate（需要 Chrome/Edge）
```python
from drea.tools import AtomicTools

agent = AtomicTools(home_dir=".drea")

# 打开网页截图
result = agent.browser_automate({
    "steps": [
        {"action": "navigate", "url": "https://example.com"},
        {"action": "screenshot", "path": "output.png"}
    ]
})
```

### 使用 github_publish 一键发布
```python
from drea.github_publisher import publish_to_github

# 自动完成：建仓 → 上传 → 打tag → 发Release
result = publish_to_github(
    source_dir="./src",
    repo_name="my-project",
    version="v1.0",
    description="My first auto-published project"
)
print(result["release_url"])
```

---

## 📁 目录结构说明

安装并 `drea init` 后，`.drea/` 目录如下：

```
.drea/
├── identity/       # 智能体身份（名称、版本、角色）
├── gene/           # 致良知基因（行为准则约束文件）
├── inbox/          # 任务队列（JSON 格式任务文件）
├── outbox/         # 任务执行结果
├── memory/         # L0-L5 分层记忆
├── checkpoint/     # 运行断点（支持断点续跑）
├── audit/          # SHA256 审计哈希链
├── fail_cards/     # 失败任务记录（含错误堆栈）
├── emergence/      # 涌现行为候选列表
├── federated/      # 联邦节点同步目录
├── quarantine/     # 危险任务隔离区
├── tools/          # 工具注册表
├── runtime/        # 运行时状态
└── config/         # 配置文件
```

---

## 📊 v1.04 → v1.05 更新内容

| 变更 | 说明 |
|------|------|
| ➕ 新增 `browser_automate` | CDP 协议零人工浏览器控制 |
| ➕ 新增 `github_publish` | 完整 GitHub 发布自动化 |
| 🔧 `DANGEROUS_PATTERNS` 增强 | 新增防空格绕过正则（v1.04 可被空格变换欺骗）|
| 🔧 `_ensure_chrome()` | 自动查找并拉起 Chrome/Edge，不再需要手动配置 |
| 🔧 凭据自愈链 | PAT 永久保存到 `~/.drea/github_token`，下次免重新授权 |

---

## 🔗 相关链接

- 📦 **仓库主页**：https://github.com/gitopal/drea-thin-kernel
- 📋 **所有版本**：https://github.com/gitopal/drea-thin-kernel/releases
- 🐛 **提交 Issue**：https://github.com/gitopal/drea-thin-kernel/issues

---

> 由 DREA 生态 004 号核心开发员自主构建并发布 · 2026-05-14
