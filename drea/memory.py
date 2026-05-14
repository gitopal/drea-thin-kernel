from __future__ import annotations

"""
memory.py — DREA v1.3 分层记忆路由器

职责：
- 管理 L0-L5 分层记忆的读写
- 默认上下文只加载 L0 + L1（信息密度最大化）
- L2-L5 通过 memory_read 按需读取
- MML / DDP / DAMP 写入 L5，带权限验证
- 所有 L1/L3/L5 写入使用 FileLock 保证并发安全
- CID token 估算集成

设计原则：
- No Execution, No Memory
- No Permission, No Training
- 默认上下文 < 2000 tokens
"""

from pathlib import Path
from typing import Any

from filelock import FileLock

from .file_protocol import DREAHome
from .util import (
    atomic_write_json,
    atomic_write_text,
    read_text,
    read_json,
    now_iso,
    new_id,
    sha256_obj,
    approx_tokens,
)

VALID_PERMISSIONS = {"trainable", "reference_only", "forbidden"}
VALID_DAMP_TYPES  = {
    "user_preference", "env_change",
    "tool_behavior",   "context_drift",
}


class MemoryRouter:
    """DREA v1.3 分层记忆路由器。"""

    def __init__(self, home: DREAHome):
        self.home = home

    # ────────────────────────────────────────────────────────
    # 默认上下文（L0 + L1 + 任务 + checkpoint）
    # ────────────────────────────────────────────────────────

    def default_context(self, task: dict, checkpoint: dict) -> dict:
        """
        构建每轮决策的默认上下文。
        只包含：L0 + L1 + 当前任务 + 当前checkpoint。
        其他记忆通过 memory_read 按需读取。
        """
        l0 = read_text(self.home.memory_l0)
        l1 = read_text(self.home.memory_l1)

        l0_tokens   = approx_tokens(l0)
        l1_tokens   = approx_tokens(l1)
        task_tokens = approx_tokens(str(task))
        ckpt_tokens = approx_tokens(str(checkpoint))
        total       = l0_tokens + l1_tokens + task_tokens + ckpt_tokens

        cfg = self.home.config_get()
        warn_threshold = cfg.get("cid_warn_threshold_tokens", 4000)

        return {
            "L0":                l0,
            "L1":                l1,
            "current_task":      task,
            "current_checkpoint": checkpoint,
            "token_estimate": {
                "L0":          l0_tokens,
                "L1":          l1_tokens,
                "task":        task_tokens,
                "checkpoint":  ckpt_tokens,
                "total":       total,
                "warn":        total > warn_threshold,
            },
        }

    # ────────────────────────────────────────────────────────
    # 按需读取（L2-L5）
    # ────────────────────────────────────────────────────────

    def read_memory(self, rel: str) -> str:
        """
        按相对路径读取记忆文件。
        rel 相对于 .drea/memory/ 目录。
        示例：memory_read("L2_facts/env.md")
        """
        path = self.home.path_in_root("memory/" + rel)
        if not path.exists():
            raise FileNotFoundError(f"memory not found: {rel}")
        return read_text(path)

    def read_skill(self, skill_filename: str) -> str:
        """读取 L3 技能文件。"""
        return self.read_memory(f"L3_skills/{skill_filename}")

    def list_skills(self, domain: str | None = None) -> list[dict]:
        """
        列出所有 L3 技能的元数据（从文件头部解析）。
        domain：按领域过滤，None 表示全部。
        """
        skills = []
        for path in sorted(self.home.memory_l3.glob("*.md")):
            meta = self._parse_skill_frontmatter(path)
            if meta:
                if domain is None or meta.get("domain") == domain:
                    meta["filename"] = path.name
                    skills.append(meta)
        return skills

    def _parse_skill_frontmatter(self, path: Path) -> dict | None:
        """解析 Skill Markdown 文件的 YAML frontmatter。"""
        try:
            text = read_text(path)
            if not text.startswith("---"):
                return None
            end = text.find("---", 3)
            if end == -1:
                return None
            frontmatter = text[3:end].strip()
            meta = {}
            for line in frontmatter.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            # 类型转换
            for float_key in ("quality_score",):
                if float_key in meta:
                    try:
                        meta[float_key] = float(meta[float_key])
                    except ValueError:
                        pass
            for int_key in ("success_count",):
                if int_key in meta:
                    try:
                        meta[int_key] = int(meta[int_key])
                    except ValueError:
                        pass
            for bool_key in ("stable", "emergence_candidate"):
                if bool_key in meta:
                    meta[bool_key] = meta[bool_key].lower() == "true"
            return meta
        except Exception:
            return None

    # ────────────────────────────────────────────────────────
    # MML 写入
    # ────────────────────────────────────────────────────────

    def write_mml(
        self,
        source:               str,
        source_task_id:       str | None,
        permission:           str,
        confidence:           float,
        quality_score:        float,
        topics:               list[str],
        data:                 dict,
        execution_verified:   bool = True,
        evaluation_verified:  bool = True,
    ) -> dict:
        """
        写入 MML（模型记忆层）到 L5_training。

        permission 必须是：trainable / reference_only / forbidden
        execution_verified：是否来自真实执行（No Execution, No Memory）
        evaluation_verified：是否经过评估（No Evaluation, No Evolution）
        """
        if permission not in VALID_PERMISSIONS:
            raise ValueError(
                f"invalid permission '{permission}', "
                f"must be one of {VALID_PERMISSIONS}"
            )
        if not execution_verified:
            raise ValueError(
                "MML requires execution_verified=True "
                "(No Execution, No Memory)"
            )
        if not evaluation_verified:
            raise ValueError(
                "MML requires evaluation_verified=True "
                "(No Evaluation, No Evolution)"
            )

        mml = {
            "mml_version":          "1.0",
            "mml_id":               new_id("mml"),
            "created_at":           now_iso(),
            "source":               source,
            "source_task_id":       source_task_id,
            "permission":           permission,
            "confidence":           confidence,
            "quality_score":        quality_score,
            "topics":               topics,
            "execution_verified":   execution_verified,
            "evaluation_verified":  evaluation_verified,
            "data":                 data,
            "content_hash":         "sha256:" + sha256_obj(data),
        }

        filename = f"{mml['mml_id']}.json"
        lock_path = self.home.memory_l5 / ".l5.lock"
        with FileLock(str(lock_path), timeout=5.0):
            atomic_write_json(self.home.memory_l5 / filename, mml)

        self.home.append_l1_entry(
            "L5 Training Data",
            f"- MML {filename} topics={topics} permission={permission}",
        )
        return mml

    # ────────────────────────────────────────────────────────
    # DDP 写入
    # ────────────────────────────────────────────────────────

    def write_ddp(
        self,
        teacher:       dict,
        task_type:     str,
        input_data:    dict,
        output_data:   dict,
        quality_score: float,
        cost:          dict,
        usage_policy:  dict,
    ) -> dict:
        """
        写入 DDP（教师蒸馏数据包）到 L5_training。

        teacher.permission 决定是否可用于训练。
        input_data / output_data 只存哈希，不存原始内容（隐私保护）。
        """
        permission = teacher.get("permission", "reference_only")
        if permission not in VALID_PERMISSIONS:
            raise ValueError(
                f"invalid teacher permission '{permission}', "
                f"must be one of {VALID_PERMISSIONS}"
            )

        ddp = {
            "ddp_version":   "1.0",
            "distill_id":    new_id("ddp"),
            "created_at":    now_iso(),
            "teacher":       teacher,
            "task_type":     task_type,
            "input_hash":    "sha256:" + sha256_obj(input_data),
            "output_hash":   "sha256:" + sha256_obj(output_data),
            "quality_score": quality_score,
            "cost":          cost,
            "usage_policy":  usage_policy,
        }

        filename = f"{ddp['distill_id']}.json"
        lock_path = self.home.memory_l5 / ".l5.lock"
        with FileLock(str(lock_path), timeout=5.0):
            atomic_write_json(self.home.memory_l5 / filename, ddp)

        self.home.append_l1_entry(
            "L5 Training Data",
            f"- DDP {filename} "
            f"teacher={teacher.get('teacher_id')} "
            f"permission={permission}",
        )
        return ddp

    def training_allowed(self, ddp: dict) -> bool:
        """判断 DDP 是否可用于训练学生模型。"""
        teacher  = ddp.get("teacher", {})
        usage    = ddp.get("usage_policy", {})
        return (
            teacher.get("permission") == "trainable"
            and usage.get("can_train_student") is True
        )

    # ────────────────────────────────────────────────────────
    # DAMP 写入
    # ────────────────────────────────────────────────────────

    def write_damp(
        self,
        damp_type:       str,
        source_task_id:  str | None,
        what_changed:    str,
        evidence:        list[str],
        confidence:      float,
        suggested_adjustment: str,
        affected_tools:  list[str],
        affected_layers: list[str],
        priority:        str = "medium",
        expires_at:      str | None = None,
    ) -> dict:
        """
        写入 DAMP（动态适应记忆协议）到 L5_training。

        damp_type：user_preference / env_change / tool_behavior / context_drift
        confidence：观察置信度，必须 >= 0.6
        priority：high / medium / low
        expires_at：过期时间（ISO8601），None 表示永不过期
        """
        if damp_type not in VALID_DAMP_TYPES:
            raise ValueError(
                f"invalid damp_type '{damp_type}', "
                f"must be one of {VALID_DAMP_TYPES}"
            )
        if confidence < 0.6:
            raise ValueError(
                f"DAMP confidence {confidence} < 0.6, "
                "observation not reliable enough"
            )
        if priority not in {"high", "medium", "low"}:
            raise ValueError(
                f"invalid priority '{priority}', "
                "must be high / medium / low"
            )

        observation = {
            "what_changed": what_changed,
            "evidence":     evidence,
            "confidence":   confidence,
        }
        adaptation = {
            "suggested_adjustment": suggested_adjustment,
            "affected_tools":       affected_tools,
            "affected_memory_layers": affected_layers,
            "priority":             priority,
        }

        damp = {
            "damp_version":  "1.0",
            "damp_id":       new_id("damp"),
            "created_at":    now_iso(),
            "damp_type":     damp_type,
            "source_task_id": source_task_id,
            "observation":   observation,
            "adaptation":    adaptation,
            "status":        "active",
            "expires_at":    expires_at,
            "content_hash":  "sha256:" + sha256_obj({
                "observation": observation,
                "adaptation":  adaptation,
            }),
        }

        filename = f"{damp['damp_id']}.json"
        lock_path = self.home.memory_l5 / ".l5.lock"
        with FileLock(str(lock_path), timeout=5.0):
            atomic_write_json(self.home.memory_l5 / filename, damp)

        self.home.append_l1_entry(
            "L5 Training Data",
            f"- DAMP {filename} "
            f"type={damp_type} "
            f"priority={priority} "
            f"confidence={confidence}",
        )
        return damp

    def list_active_damps(self) -> list[dict]:
        """
        列出所有 active 状态的 DAMP 记录。
        自动过滤已过期的记录（更新状态为 expired）。
        """
        active = []
        now = now_iso()
        for path in sorted(self.home.memory_l5.glob("damp_*.json")):
            damp = read_json(path, None)
            if damp is None:
                continue
            if damp.get("status") != "active":
                continue
            expires_at = damp.get("expires_at")
            if expires_at and expires_at < now:
                damp["status"] = "expired"
                atomic_write_json(path, damp)
                continue
            active.append(damp)
        return active
