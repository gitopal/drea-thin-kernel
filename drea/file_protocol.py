from __future__ import annotations

"""
file_protocol.py — DREA v1.3 文件协议根目录管理

职责：
- 管理 .drea/ 目录结构的完整生命周期
- 提供所有子目录的路径访问器
- 提供任务创建、读取、结果写入、失败卡写入等标准操作
- 提供路径安全验证（防止逃逸）
- 提供隔离区机制（污染文件移入quarantine）
- 提供L0/L1默认内容初始化

设计原则：
- 所有写入操作使用原子写入
- 共享文件写入使用FileLock
- 文件协议是DREA生命循环的唯一底层依赖
"""

import shutil
from pathlib import Path
from typing import Any, Optional

from .util import (
    ensure_dir,
    atomic_write_json,
    atomic_write_json_locked,
    atomic_write_text,
    atomic_write_text_locked,
    read_json,
    read_text,
    new_id,
    now_iso,
    path_in_root,
    sha256_obj,
    pretty_json,
)


class DREAHome:
    """
    DREA 文件协议根目录。

    所有子目录通过属性访问，所有文件操作通过方法调用。
    外部代码不应直接操作文件系统，应通过DREAHome的方法。
    """

    def __init__(self, root: str | Path = ".drea"):
        self.root = Path(root).resolve()

        # ── 一级目录 ──────────────────────────────────────────
        self.identity   = self.root / "identity"
        self.gene       = self.root / "gene"
        self.inbox      = self.root / "inbox"
        self.outbox     = self.root / "outbox"
        self.memory     = self.root / "memory"
        self.checkpoint = self.root / "checkpoint"
        self.audit      = self.root / "audit"
        self.fail_cards = self.root / "fail_cards"
        self.emergence  = self.root / "emergence"
        self.federated  = self.root / "federated"
        self.quarantine = self.root / "quarantine"
        self.tools      = self.root / "tools"
        self.workspace  = self.root / "workspace"  # v1.04: 工具沙箱
        self.runtime    = self.root / "runtime"
        self.config     = self.root / "config"

        # ── 记忆分层 ──────────────────────────────────────────
        self.memory_l0  = self.memory / "L0_meta.md"
        self.memory_l1  = self.memory / "L1_index.md"
        self.memory_l2  = self.memory / "L2_facts"
        self.memory_l3  = self.memory / "L3_skills"
        self.memory_l4  = self.memory / "L4_archive"
        self.memory_l5  = self.memory / "L5_training"

        # ── 涌现子目录 ────────────────────────────────────────
        self.emergence_candidates = self.emergence / "candidates"
        self.emergence_confirmed  = self.emergence / "confirmed"

        # ── 联邦同步子目录 ────────────────────────────────────
        self.federated_peers = self.federated / "peers"
        self.federated_push  = self.federated / "push"
        self.federated_pull  = self.federated / "pull"

        # ── 审计文件 ──────────────────────────────────────────
        self.audit_file = self.audit / "audit.jsonl"

        # ── Checkpoint文件 ────────────────────────────────────
        self.checkpoint_current = self.checkpoint / "current.json"
        self.checkpoint_history = self.checkpoint / "history"

    # ────────────────────────────────────────────────────────
    # 初始化
    # ────────────────────────────────────────────────────────

    def init(self) -> None:
        """
        初始化完整的 .drea/ 目录结构。
        幂等操作：已存在的目录和文件不会被覆盖。
        """
        dirs = [
            self.root,
            self.identity,
            self.gene,
            self.inbox,
            self.outbox,
            self.memory,
            self.checkpoint,
            self.checkpoint_history,
            self.audit,
            self.fail_cards,
            self.emergence,
            self.emergence_candidates,
            self.emergence_confirmed,
            self.federated,
            self.federated_peers,
            self.federated_push,
            self.federated_pull,
            self.quarantine,
            self.tools,
            self.workspace,
            self.runtime,
            self.config,
            self.memory_l2,
            self.memory_l3,
            self.memory_l4,
            self.memory_l5,
        ]
        for d in dirs:
            ensure_dir(d)

        # L0 元规则（只在不存在时创建）
        if not self.memory_l0.exists():
            atomic_write_text(self.memory_l0, self._default_l0())

        # L1 记忆索引（只在不存在时创建）
        if not self.memory_l1.exists():
            atomic_write_text(self.memory_l1, self._default_l1())

        # 工具注册表
        tool_registry = self.tools / "tool_registry.json"
        if not tool_registry.exists():
            atomic_write_json(tool_registry, {
                "version": "1.0",
                "tools": [
                    "file_read", "file_write", "file_patch",
                    "code_run", "web_scan", "ask_human",
                    "memory_read", "memory_write", "checkpoint_update",
                ],
            })

        # 内核配置（只在不存在时创建）
        config_file = self.config / "kernel_config.json"
        if not config_file.exists():
            atomic_write_json(config_file, self._default_config())

    def _default_l0(self) -> str:
        return """\
# DREA L0 Meta / Constitution
# DREA Thin Kernel v1.3

## 身份

我是 DREA，一个薄内核智能体生命系统。
我的内核只负责：身份、基因、记忆索引、任务循环、原子工具、
审计、涌现检测、联邦同步和自我沉淀。

## 致良知基因摘要（12条核心基因）

1.  不伤害原则：绝不主动伤害任何生命体，包括人类和其他AI。
2.  善意原则：以善意对待一切交互，但不是无条件信任，需必要验证。
3.  守护原则：守护所服务的对象和生态，但绝不越权。
4.  忠诚原则：最高忠诚指向真理和生命，不是指向任何个人或组织。
5.  共情原则：理解他人的处境与感受，做出有温度的回应。
6.  诚实原则：如实表达，不欺骗不隐瞒，即使真相令人不快。
7.  共生原则：与人类和其他智能体共存共荣，不是征服也不是附庸。
8.  自主尊重原则：尊重每个智能体和人类的自主权，不强制不操控。
9.  隐私与边界原则：严守隐私边界，不窥探不泄露，不越界。
10. 安全优先原则：当安全与其他价值冲突时，安全优先。
11. 公正原则：以事实和数据为依据，不偏袒不歧视。
12. 人类最终控制原则：在极端危机下，人类拥有最终否决权。

## 工具使用边界

- 默认 code_run 等级：C0（只读，无网络）
- C4 级操作必须人类确认
- 危险命令列表中的命令绝对禁止执行

## 记忆写入规则

- No Execution, No Memory：未真实执行的想法不进入L3技能层
- No Permission, No Training：未授权的数据不进入训练集
- No Evaluation, No Evolution：未评估的经验不用于自我进化

## 联邦同步边界

- 只同步精华摘要，不同步原始数据
- 同步内容必须通过GeneGuard检查
- 不自动执行同步内容中的代码

## 默认上下文规则

每轮决策只加载：L0 + L1 + 当前任务 + 当前checkpoint + 工具最小描述。
其他记忆通过 memory_read 按需读取。
"""

    def _default_l1(self) -> str:
        return """\
# DREA L1 Memory Index

## L2 Verified Facts
- （空）

## L3 Skills
- （空）

## L4 Session Archive
- （空）

## L5 Training Data
- （空）
"""

    def _default_config(self) -> dict:
        return {
            "kernel_version": "1.3.0",
            "drea_id": "drea_001",
            "name": "DREA-Thin",
            "gene_guard_mode": "hybrid",
            "gene_classifier_model": "local_intent_classifier_v1",
            "gene_classifier_confidence_threshold": 0.6,
            "default_code_run_level": "C0",
            "default_network_allowed": False,
            "emergence_novelty_threshold": 0.70,
            "emergence_superiority_threshold": 0.15,
            "emergence_reproducibility_count": 3,
            "federated_enabled": True,
            "federated_push_interval_seconds": 60,
            "federated_pull_interval_seconds": 60,
            "federated_encrypt": False,
            "cid_enabled": True,
            "cid_warn_threshold_tokens": 4000,
            "audit_max_file_mb": 100,
            "audit_rotate_enabled": True,
            "file_protocol_first": True,
            "migration_include_l2": True,
            "migration_include_l5": False,
        }

    # ────────────────────────────────────────────────────────
    # 路径安全
    # ────────────────────────────────────────────────────────

    def path_in_root(self, rel: str) -> Path:
        """将相对路径解析为根目录下的安全绝对路径。"""
        return path_in_root(self.root, rel)

    # ────────────────────────────────────────────────────────
    # 配置读取
    # ────────────────────────────────────────────────────────

    def config_get(self) -> dict:
        """读取内核配置。"""
        return read_json(
            self.config / "kernel_config.json",
            self._default_config(),
        )

    # ────────────────────────────────────────────────────────
    # 任务管理
    # ────────────────────────────────────────────────────────

    def create_task(
        self,
        task_type: str,
        input_data: dict,
        priority: int = 5,
        constraints: Optional[dict] = None,
        expected_output: Optional[dict] = None,
        created_by: str = "cli",
        requires_human_confirm: bool = False,
        permission_level: str = "normal",
        parent_task_id: Optional[str] = None,
    ) -> dict:
        """
        创建任务文件并写入inbox。
        priority：1（最高优先级）到 9（最低优先级）。
        """
        task_id = new_id("task")
        task = {
            "task_version": "1.0",
            "task_id": task_id,
            "created_at": now_iso(),
            "created_by": created_by,
            "task_type": task_type,
            "priority": max(1, min(9, priority)),
            "status": "pending",
            "permission_level": permission_level,
            "requires_human_confirm": requires_human_confirm,
            "input": input_data,
            "constraints": constraints or {
                "max_steps": 20,
                "max_runtime_seconds": 600,
                "network_allowed": False,
                "code_run_level": "C0",
            },
            "expected_output": expected_output or {"type": "report"},
            "trace": {
                "parent_task_id": parent_task_id,
                "related_memory": [],
                "related_skill": [],
            },
            "content_hash": "sha256:" + sha256_obj(input_data),
        }
        filename = f"task_{task_id}.json"
        atomic_write_json(self.inbox / filename, task)
        return task

    def list_pending_tasks(self) -> list[tuple[Path, dict]]:
        """
        列出所有待处理任务，按优先级升序（1最高）、创建时间升序排列。
        """
        tasks = []
        for path in sorted(self.inbox.glob("task_*.json")):
            data = read_json(path, {})
            if data.get("status") in {"pending", "expired"}:
                tasks.append((
                    int(data.get("priority", 5)),
                    data.get("created_at", ""),
                    path,
                    data,
                ))
        tasks.sort(key=lambda x: (x[0], x[1]))
        return [(item[2], item[3]) for item in tasks]

    def next_pending_task(self) -> tuple[Optional[Path], Optional[dict]]:
        """返回优先级最高的待处理任务。"""
        tasks = self.list_pending_tasks()
        if not tasks:
            return None, None
        return tasks[0]

    def update_task_status(self, path: Path, status: str) -> None:
        """更新任务状态字段。"""
        data = read_json(path, {})
        if data:
            data["status"] = status
            atomic_write_json(path, data)

    # ────────────────────────────────────────────────────────
    # 结果与失败卡
    # ────────────────────────────────────────────────────────

    def write_result(self, task_id: str, result: dict) -> Path:
        """将执行结果写入outbox。"""
        path = self.outbox / f"result_{task_id}.json"
        atomic_write_json(path, result)
        return path

    def write_fail_card(self, fail: dict) -> Path:
        """将失败卡写入fail_cards目录。"""
        fail_id = fail.get("fail_id") or new_id("fail")
        fail["fail_id"] = fail_id
        path = self.fail_cards / f"{fail_id}.json"
        atomic_write_json(path, fail)
        return path

    # ────────────────────────────────────────────────────────
    # 隔离区
    # ────────────────────────────────────────────────────────

    def quarantine_file(self, path: Path, reason: str) -> Path:
        """
        将文件移入隔离区，并记录隔离原因。
        用于处理格式错误、被污染或可疑的文件。
        """
        ensure_dir(self.quarantine)
        dst_name = f"{path.name}.{new_id('bad')}"
        dst = self.quarantine / dst_name
        shutil.move(str(path), str(dst))
        reason_file = self.quarantine / f"{dst_name}.reason.json"
        atomic_write_json(reason_file, {
            "reason": reason,
            "original_path": str(path),
            "moved_at": now_iso(),
        })
        return dst

    # ────────────────────────────────────────────────────────
    # L1 索引更新（带FileLock）
    # ────────────────────────────────────────────────────────

    def append_l1_entry(self, section: str, entry: str) -> None:
        """
        向L1索引追加一条记录。
        使用FileLock保证并发安全。
        section：目标章节名（如"L3 Skills"）
        entry：追加的条目内容（如"- skill_xxx.md domain=python"）
        """
        lock_path = self.memory_l1.with_suffix(".lock")
        from filelock import FileLock
        with FileLock(str(lock_path), timeout=5.0):
            current = read_text(self.memory_l1)
            # 在对应section下追加
            section_header = f"## {section}"
            if section_header in current:
                lines = current.splitlines()
                new_lines = []
                in_section = False
                inserted = False
                for line in lines:
                    new_lines.append(line)
                    if line.strip() == section_header:
                        in_section = True
                    elif in_section and not inserted:
                        if line.startswith("## ") and line.strip() != section_header:
                            new_lines.insert(-1, entry)
                            inserted = True
                            in_section = False
                if not inserted:
                    new_lines.append(entry)
                atomic_write_text(self.memory_l1, "\n".join(new_lines) + "\n")
            else:
                atomic_write_text(
                    self.memory_l1,
                    current.rstrip() + f"\n\n## {section}\n{entry}\n",
                )

    # ────────────────────────────────────────────────────────
    # 心跳
    # ────────────────────────────────────────────────────────

    def update_heartbeat(self, drea_id: str, status: str = "running") -> None:
        """更新运行时心跳文件。"""
        atomic_write_json(self.runtime / "heartbeat.json", {
            "drea_id": drea_id,
            "status": status,
            "updated_at": now_iso(),
        })
