from __future__ import annotations

"""
cid.py — DREA v1.3 上下文信息密度报告器

职责：
- 计算每轮决策的上下文 token 消耗
- 检测无关记忆加载（irrelevant_memory）
- 生成 CID 报告并存入 L4_archive
- 提供 token 预算警告

设计原则：
- CID 是 DREA 独有的效率指标
- 高 irrelevant_memory_items 说明记忆路由需要优化
- CID 报告用于指导 L1 索引精简
"""

from .file_protocol import DREAHome
from .util import (
    atomic_write_json,
    now_iso,
    new_id,
    approx_tokens,
)


class CIDReporter:
    """DREA v1.3 上下文信息密度报告器。"""

    def __init__(self, home: DREAHome):
        self.home = home

    def write_report(
        self,
        task:            dict,
        context:         dict,
        result:          dict,
        loaded_memory:   list[str] | None = None,
        used_memory:     list[str] | None = None,
        tools_called:    list[str] | None = None,
    ) -> dict:
        """
        生成并存储 CID 报告。

        loaded_memory：本轮加载的记忆文件列表
        used_memory：实际被引用的记忆文件列表
        tools_called：本轮调用的工具列表
        """
        loaded_memory = loaded_memory or []
        used_memory   = used_memory   or []
        tools_called  = tools_called  or []

        cfg           = self.home.config_get()
        warn_threshold = cfg.get("cid_warn_threshold_tokens", 4000)

        # Token 计算
        l0_tokens    = approx_tokens(context.get("L0", ""))
        l1_tokens    = approx_tokens(context.get("L1", ""))
        task_tokens  = approx_tokens(str(context.get("current_task", "")))
        ckpt_tokens  = approx_tokens(str(context.get("current_checkpoint", "")))
        result_tokens = approx_tokens(str(result))

        tool_desc_tokens = approx_tokens(
            " ".join([
                "file_read", "file_write", "file_patch",
                "code_run", "web_scan", "ask_human",
                "memory_read", "memory_write", "checkpoint_update",
            ])
        )

        extra_memory_tokens = sum(
            approx_tokens(m) for m in loaded_memory
        )

        total_prompt_tokens = (
            l0_tokens + l1_tokens + task_tokens +
            ckpt_tokens + tool_desc_tokens + extra_memory_tokens
        )

        irrelevant = max(0, len(loaded_memory) - len(used_memory))

        report = {
            "cid_version":               "1.0",
            "cid_id":                    new_id("cid"),
            "task_id":                   task.get("task_id"),
            "created_at":                now_iso(),
            "token_breakdown": {
                "L0":                    l0_tokens,
                "L1":                    l1_tokens,
                "task":                  task_tokens,
                "checkpoint":            ckpt_tokens,
                "tool_descriptions":     tool_desc_tokens,
                "extra_memory":          extra_memory_tokens,
                "result":                result_tokens,
                "total_prompt":          total_prompt_tokens,
            },
            "memory_efficiency": {
                "items_loaded":          len(loaded_memory),
                "items_used":            len(used_memory),
                "irrelevant_items":      irrelevant,
                "efficiency_ratio": (
                    len(used_memory) / len(loaded_memory)
                    if loaded_memory else 1.0
                ),
            },
            "tools_called":              tools_called,
            "task_success":              result.get("evaluation", {}).get("success", False),
            "quality_score":             result.get("evaluation", {}).get("quality_score", 0.0),
            "warnings": {
                "token_budget_exceeded": total_prompt_tokens > warn_threshold,
                "low_memory_efficiency": irrelevant > 3,
                "no_tools_used":         len(tools_called) == 0,
            },
            "notes": "DREA v1.3 CID report.",
        }

        path = self.home.memory_l4 / f"{report['cid_id']}.json"
        atomic_write_json(path, report)
        return report
