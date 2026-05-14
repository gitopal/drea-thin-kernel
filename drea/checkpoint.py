from __future__ import annotations

"""
checkpoint.py — DREA v1.3 运行状态检查点

职责：
- 记录Agent Loop每一步的运行状态
- 支持中断恢复（从最近checkpoint继续）
- 历史checkpoint归档（支持回溯）
- state_hash保证checkpoint完整性

设计原则：
- 每步都写checkpoint，不是每轮
- checkpoint是Loop可恢复性的唯一依据
- current.json始终是最新状态
- history/目录保存所有历史快照
"""

from .file_protocol import DREAHome
from .util import (
    atomic_write_json,
    read_json,
    now_iso,
    new_id,
    sha256_obj,
)


class CheckpointManager:
    """DREA v1.3 检查点管理器。"""

    def __init__(self, home: DREAHome):
        self.home = home
        self.current_path  = self.home.checkpoint_current
        self.history_dir   = self.home.checkpoint_history

    # ────────────────────────────────────────────────────────
    # 更新
    # ────────────────────────────────────────────────────────

    def update(
        self,
        drea_id:         str,
        current_task_id: str | None,
        step:            int,
        last_action:     str,
        working_memory:  dict | None = None,
        resource_state:  dict | None = None,
        loop_phase:      str = "running",
    ) -> dict:
        """
        更新当前checkpoint。
        旧的current.json自动归档到history/。

        loop_phase：当前Loop阶段标识，用于中断恢复定位。
        可选值：load_identity | load_gene | read_task | gene_check |
                execute_tool | evaluate | crystallize | audit | idle
        """
        ckpt_body = {
            "checkpoint_version": "1.0",
            "checkpoint_id":      new_id("ckpt"),
            "created_at":         now_iso(),
            "drea_id":            drea_id,
            "current_task_id":    current_task_id,
            "step":               step,
            "last_action":        last_action,
            "loop_phase":         loop_phase,
            "working_memory":     working_memory or {},
            "resource_state":     resource_state or {},
        }
        ckpt_body["state_hash"] = "sha256:" + sha256_obj(ckpt_body)

        # 归档旧checkpoint
        old = read_json(self.current_path, None)
        if old:
            old_id = old.get("checkpoint_id", new_id("ckpt"))
            hist_path = self.history_dir / f"{old_id}.json"
            atomic_write_json(hist_path, old)

        # 写入新checkpoint
        atomic_write_json(self.current_path, ckpt_body)
        return ckpt_body

    # ────────────────────────────────────────────────────────
    # 读取
    # ────────────────────────────────────────────────────────

    def get(self) -> dict:
        """读取当前checkpoint。不存在时返回空白初始状态。"""
        return read_json(self.current_path, self._empty())

    def get_history(self, limit: int = 20) -> list[dict]:
        """
        读取最近N条历史checkpoint，按时间倒序排列。
        用于调试和回溯分析。
        """
        files = sorted(
            self.history_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        results = []
        for f in files[:limit]:
            data = read_json(f, None)
            if data:
                results.append(data)
        return results

    def verify(self) -> bool:
        """验证当前checkpoint的state_hash完整性。"""
        ckpt = read_json(self.current_path, None)
        if ckpt is None:
            return True  # 空checkpoint视为有效
        stored = ckpt.get("state_hash", "")
        body = {k: v for k, v in ckpt.items() if k != "state_hash"}
        expected = "sha256:" + sha256_obj(body)
        return stored == expected

    # ────────────────────────────────────────────────────────
    # 工具方法
    # ────────────────────────────────────────────────────────

    def _empty(self) -> dict:
        """空白初始checkpoint（Loop首次启动时使用）。"""
        return {
            "checkpoint_version": "1.0",
            "checkpoint_id":      "none",
            "created_at":         None,
            "drea_id":            "unknown",
            "current_task_id":    None,
            "step":               0,
            "last_action":        "none",
            "loop_phase":         "idle",
            "working_memory":     {},
            "resource_state":     {},
            "state_hash":         "",
        }
