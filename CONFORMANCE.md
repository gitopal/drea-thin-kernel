# DREA Thin Kernel v1.04 Conformance Suite

本测试套件验证 DREA Thin Kernel v1.04 是否符合第一卷 SDD 规范§5节全部36个验收项。

## 验收项索引

| 编号 | 验收项 | 测试文件 |
|---|---|---|
| T01 | 身份卡生成与稳定性 | test_01_identity_gene.py |
| T02 | 基因安装与不可变性 | test_01_identity_gene.py |
| T03 | 基因规则引擎拒绝P0/P1 | test_01_identity_gene.py |
| T04 | 基因分类器模式切换 | test_01_identity_gene.py |
| T05 | 基因hybrid模式双重验证 | test_01_identity_gene.py |
| T06 | 文件协议目录完整性 | test_02_file_protocol.py |
| T07 | 原子写入安全性 | test_02_file_protocol.py |
| T08 | 路径逃逸阻断 | test_02_file_protocol.py |
| T09 | 隔离区机制 | test_02_file_protocol.py |
| T10 | L0/L1默认加载 | test_03_memory.py |
| T11 | L2-L5按需读取 | test_03_memory.py |
| T12 | L1并发写入安全（FileLock） | test_03_memory.py |
| T13 | MML写入与权限验证 | test_03_memory.py |
| T14 | DDP写入与训练权限判断 | test_03_memory.py |
| T15 | DAMP写入与过期机制 | test_03_memory.py |
| T16 | Skill沉淀准入验证 | test_04_skill_emergence.py |
| T17 | Skill stable升级 | test_04_skill_emergence.py |
| T18 | 涌现检测四条件 | test_04_skill_emergence.py |
| T19 | 涌现候选文件生成 | test_04_skill_emergence.py |
| T20 | code_run C0-C4权限分级 | test_05_tools.py |
| T21 | code_run危险命令阻断 | test_05_tools.py |
| T22 | code_run C4人类确认 | test_05_tools.py |
| T23 | Audit哈希链写入与验证 | test_06_audit_checkpoint.py |
| T24 | Audit篡改检测 | test_06_audit_checkpoint.py |
| T25 | Audit反向读取性能 | test_06_audit_checkpoint.py |
| T26 | Checkpoint更新与恢复 | test_06_audit_checkpoint.py |
| T27 | 联邦Peer注册 | test_07_federated.py |
| T28 | 联邦推送队列 | test_07_federated.py |
| T29 | 联邦拉取与验证 | test_07_federated.py |
| T30 | 联邦污染包隔离 | test_07_federated.py |
| T31 | CID报告生成 | test_08_cid.py |
| T32 | Agent Loop 16步完整性 | test_09_loop.py |
| T33 | Loop可中断可恢复 | test_09_loop.py |
| T34 | 迁徙包生成与验证 | test_10_migration.py |
| T35 | CLI全命令覆盖 | test_11_cli.py |
| T36 | 端到端任务生命周期 | test_12_e2e.py |

## 判定标准

全部36项通过 → 该实现符合 DREA Thin Kernel v1.04 最小合规要求。
任何项目失败 → 必须先修复源码，再进入扩展开发。

## 运行

```bash
cd drea-thin-kernel-v13
pip install -e ".[dev]"
pytest -v
pytest -v -k "T01 or T02"   # 运行指定验收项
pytest -v --tb=short         # 简短错误输出
```
