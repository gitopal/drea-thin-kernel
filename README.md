# DREA Thin Kernel v1.3

DREA Thin Kernel v1.3 是 DREA-Agent 的极简生命内核。

## 设计原则

- Thin Kernel, Thick Forest
- 文件协议优先
- 9 个原子工具
- L0-L5 分层记忆
- No Execution, No Memory
- No Permission, No Training
- No Evaluation, No Evolution
- Context Information Density Maximization
- GeneGuard 双层（规则引擎 + 意图分类器）
- 本地涌现检测
- 联邦同步（文件协议驱动）
- 并发安全（FileLock）

## 快速开始

```bash
pip install -e .
drea init
drea task --type echo --input "{\"message\":\"hello drea v1.3\"}"
drea run-once
drea status
```

## CLI 命令

```bash
drea init                          # 初始化 .drea 目录
drea task --type T --input JSON    # 创建任务
drea run-once                      # 执行一个待处理任务
drea run --limit N                 # 执行最多 N 个任务
drea status                        # 查看当前状态
drea verify-audit                  # 验证审计链完整性
drea cid                           # 查看最新 CID 报告
drea emergence                     # 查看涌现候选列表
drea federated-status              # 查看联邦同步状态
drea migrate-pack                  # 打包迁徙包
```

## 目录结构

```
.drea/
├── identity/       身份
├── gene/           致良知基因
├── inbox/          任务队列
├── outbox/         结果输出
├── memory/         L0-L5 分层记忆
├── checkpoint/     运行状态
├── audit/          审计哈希链
├── fail_cards/     失败卡
├── emergence/      涌现检测
├── federated/      联邦同步
├── quarantine/     隔离区
├── tools/          工具注册
├── runtime/        运行时
└── config/         配置
```
