from __future__ import annotations

"""
audit.py — DREA v1.3 审计哈希链

职责：
- 记录所有关键事件到 audit.jsonl
- 每条记录包含：previous_hash → current_hash，形成不可篡改链
- 验证审计链完整性（detect tamper）
- 反向读取最后一行，O(1)性能（修复v1.2全量遍历问题）
- 支持审计文件轮转（防止单文件过大）

设计原则：
- append_jsonl 是唯一写入方式，不允许修改历史记录
- verify() 检测任何篡改
- 审计链是DREA可信度的基础
"""

import json
from pathlib import Path

from .file_protocol import DREAHome
from .util import (
    append_jsonl,
    read_all_jsonl_lines,
    read_last_jsonl_line,
    now_iso,
    new_id,
    sha256_obj,
)


# 必须审计的事件类型（规范§2.14）
REQUIRED_AUDIT_EVENTS = {
    "identity_created",
    "gene_installed",
    "task_received",
    "task_started",
    "task_completed",
    "task_failed",
    "tool_executed",
    "gene_violation",
    "skill_crystallized",
    "emergence_detected",
    "emergence_confirmed",
    "mml_written",
    "ddp_written",
    "damp_written",
    "federated_push",
    "federated_pull",
    "checkpoint_updated",
    "migration_packed",
}


class AuditChain:
    """
    DREA v1.3 审计哈希链。

    每条记录结构：
    {
        audit_version:  "1.0",
        event_id:       "audit_xxxxx",
        created_at:     "ISO8601",
        actor:          "drea_001 | cli | human",
        action:         "task_completed | skill_crystallized | ...",
        target:         "task_xxxxx | skill_xxxxx | null",
        payload_hash:   "sha256:xxxxx",   # payload内容哈希，不存原始payload
        previous_hash:  "sha256:xxxxx",   # 上一条记录的current_hash
        current_hash:   "sha256:xxxxx",   # 本条记录的哈希（不含current_hash字段）
    }
    """

    def __init__(self, home: DREAHome):
        self.home = home
        self.path = self.home.audit_file

    # ────────────────────────────────────────────────────────
    # 写入
    # ────────────────────────────────────────────────────────

    def log(
        self,
        actor:   str,
        action:  str,
        target:  str | None,
        payload: dict,
    ) -> dict:
        """
        写入一条审计记录。
        previous_hash 通过反向读取O(1)获取，不全量遍历。
        """
        previous_hash = self._last_hash()

        event_body = {
            "audit_version":  "1.0",
            "event_id":       new_id("audit"),
            "created_at":     now_iso(),
            "actor":          actor,
            "action":         action,
            "target":         target,
            "payload_hash":   "sha256:" + sha256_obj(payload),
            "previous_hash":  previous_hash,
        }
        # current_hash 覆盖整个event_body（不含自身）
        event_body["current_hash"] = "sha256:" + sha256_obj(event_body)

        append_jsonl(self.path, event_body)
        self._maybe_rotate()
        return event_body

    # ────────────────────────────────────────────────────────
    # 读取
    # ────────────────────────────────────────────────────────

    def list(self) -> list[dict]:
        """读取所有审计记录。"""
        return read_all_jsonl_lines(self.path)

    def _last_hash(self) -> str:
        """
        O(1) 反向读取最后一条记录的 current_hash。
        修复v1.2中全量遍历的性能问题。
        """
        last = read_last_jsonl_line(self.path)
        if last is None:
            return ""
        return last.get("current_hash", "")

    # ────────────────────────────────────────────────────────
    # 验证
    # ────────────────────────────────────────────────────────

    def verify(self) -> bool:
        """
        验证审计链完整性。
        检查：
        1. 每条记录的 previous_hash 等于上一条的 current_hash
        2. 每条记录的 current_hash 等于其body的sha256
        任何不一致返回 False。
        """
        previous = ""
        for event in self.list():
            # 检查链式哈希
            if event.get("previous_hash", "") != previous:
                return False

            # 重新计算 current_hash
            stored_current = event.get("current_hash", "")
            body = {k: v for k, v in event.items() if k != "current_hash"}
            expected = "sha256:" + sha256_obj(body)
            if stored_current != expected:
                return False

            previous = stored_current
        return True

    def verify_report(self) -> dict:
        """
        返回详细的验证报告。
        包含：总记录数、第一个错误位置、错误类型。
        """
        previous = ""
        total = 0
        for i, event in enumerate(self.list()):
            total += 1

            if event.get("previous_hash", "") != previous:
                return {
                    "valid": False,
                    "total_events": total,
                    "error_at_index": i,
                    "error_type": "chain_broken",
                    "event_id": event.get("event_id"),
                }

            stored_current = event.get("current_hash", "")
            body = {k: v for k, v in event.items() if k != "current_hash"}
            expected = "sha256:" + sha256_obj(body)
            if stored_current != expected:
                return {
                    "valid": False,
                    "total_events": total,
                    "error_at_index": i,
                    "error_type": "hash_mismatch",
                    "event_id": event.get("event_id"),
                }

            previous = stored_current

        return {
            "valid": True,
            "total_events": total,
            "error_at_index": None,
            "error_type": None,
        }

    # ────────────────────────────────────────────────────────
    # 轮转
    # ────────────────────────────────────────────────────────

    def _maybe_rotate(self) -> None:
        """
        检查审计文件大小，超过配置阈值时轮转。
        轮转：将当前文件重命名为 audit_{timestamp}.jsonl，新建空文件。
        """
        cfg = self.home.config_get()
        if not cfg.get("audit_rotate_enabled", True):
            return

        max_mb = cfg.get("audit_max_file_mb", 100)
        if not self.path.exists():
            return

        size_mb = self.path.stat().st_size / (1024 * 1024)
        if size_mb < max_mb:
            return

        from .util import now_iso
        ts = now_iso().replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
        rotated = self.home.audit / f"audit_{ts}.jsonl"
        self.path.rename(rotated)
        # 新文件从空链开始（previous_hash = ""）
