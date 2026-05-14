from __future__ import annotations

"""
loop.py — DREA v1.3 Agent Loop（16步生命周期）

职责：
- 实现完整的 16 步 Agent Loop
- 可中断、可恢复（每步写 checkpoint）
- GeneGuard 在任何工具执行前强制调用
- 成功执行后触发 Skill 结晶 + 涌现检测
- 每轮结束后触发联邦同步推送
- 审计链记录每个关键步骤

16步生命周期：
 1. load_identity
 2. load_gene
 3. load_L0_L1_memory
 4. read_inbox_task
 5. load_checkpoint
 6. route_relevant_memory
 7. gene_check
 8. select_atomic_tool
 9. execute_tool
10. write_observation
11. update_checkpoint
12. evaluate_result
13. if success → crystallize_skill_or_mml
14.            → run_emergence_check
15. if failure → generate_fail_card
16. append_audit → federated_sync_push → wait_next_task
"""

from pathlib import Path
from typing import Any

from .file_protocol import DREAHome
from .identity import IdentityManager
from .gene import GeneGuard
from .audit import AuditChain
from .checkpoint import CheckpointManager
from .memory import MemoryRouter
from .skill import SkillCrystallizer
from .emergence import EmergenceDetector
from .federated import FederatedSync
from .cid import CIDReporter
from .tools import AtomicTools, ToolError, PermissionError
from .util import (
    now_iso,
    new_id,
    read_json,
    atomic_write_json,
    sha256_obj,
)


class DREALoop:
    """
    DREA v1.3 Agent Loop。

    设计原则：
    - 每步都写 checkpoint，保证可中断可恢复
    - GeneGuard 在任何工具执行前强制调用
    - Loop 本身保持短小，业务逻辑不进入 Loop
    - 所有关键事件写入 AuditChain
    """

    def __init__(
        self,
        home:     DREAHome,
        drea_id:  str = "drea_001",
        name:     str = "DREA-Thin",
    ):
        self.home     = home
        self.drea_id  = drea_id
        self.name     = name

        # 组件初始化（延迟到 init() 调用）
        self.identity:   IdentityManager  | None = None
        self.gene:       GeneGuard        | None = None
        self.audit:      AuditChain       | None = None
        self.checkpoint: CheckpointManager| None = None
        self.memory:     MemoryRouter     | None = None
        self.skill:      SkillCrystallizer| None = None
        self.emergence:  EmergenceDetector| None = None
        self.federated:  FederatedSync    | None = None
        self.cid:        CIDReporter      | None = None
        self.tools:      AtomicTools      | None = None

        self._initialized = False

    # ────────────────────────────────────────────────────────
    # 初始化
    # ────────────────────────────────────────────────────────

    def init(self) -> None:
        """初始化所有组件。幂等操作。"""
        if self._initialized:
            return

        self.home.init()

        self.identity   = IdentityManager(self.home, self.drea_id, self.name)
        self.gene       = GeneGuard(self.home)
        self.audit      = AuditChain(self.home)
        self.checkpoint = CheckpointManager(self.home)
        self.memory     = MemoryRouter(self.home)
        self.skill      = SkillCrystallizer(self.home)
        self.emergence  = EmergenceDetector(self.home, self.memory)
        self.federated  = FederatedSync(self.home)
        self.cid        = CIDReporter(self.home)
        self.tools      = AtomicTools(self.home, self.memory, self.checkpoint)

        self._initialized = True

    # ────────────────────────────────────────────────────────
    # 主入口：执行一个任务
    # ────────────────────────────────────────────────────────

    def run_once(self) -> dict:
        """
        执行 inbox 中优先级最高的待处理任务。
        返回执行结果摘要。
        """
        self._ensure_init()

        # ── 步骤1：load_identity ──────────────────────────────
        identity_card = self.identity.ensure()
        self._ckpt(None, 1, "load_identity", loop_phase="load_identity")

        # ── 步骤2：load_gene ──────────────────────────────────
        gene_data = self.gene.ensure()
        self._ckpt(None, 2, "load_gene", loop_phase="load_gene")

        # ── 步骤3：load_L0_L1_memory ─────────────────────────
        # L0/L1 在 default_context 中加载，此步骤确认文件存在
        l0_exists = self.home.memory_l0.exists()
        l1_exists = self.home.memory_l1.exists()
        self._ckpt(None, 3, "load_L0_L1_memory",
                   loop_phase="load_memory",
                   working_memory={"l0_ok": l0_exists, "l1_ok": l1_exists})

        # ── 步骤4：read_inbox_task ────────────────────────────
        task_path, task = self.home.next_pending_task()
        if task is None:
            self._ckpt(None, 4, "no_task", loop_phase="idle")
            return {"status": "idle", "reason": "no_pending_tasks"}

        task_id = task["task_id"]
        self.home.update_task_status(task_path, "running")
        self._ckpt(task_id, 4, "read_inbox_task", loop_phase="read_task")
        self.audit.log(self.drea_id, "task_started", task_id, task)

        # ── 步骤5：load_checkpoint ────────────────────────────
        ckpt = self.checkpoint.get()
        # 检查是否是中断恢复
        is_resume = (
            ckpt.get("current_task_id") == task_id
            and ckpt.get("loop_phase") not in {"idle", "none", None}
        )
        self._ckpt(task_id, 5, "load_checkpoint",
                   loop_phase="load_checkpoint",
                   working_memory={"is_resume": is_resume})

        # ── 步骤6：route_relevant_memory ──────────────────────
        context = self.memory.default_context(task, ckpt)
        loaded_memory: list[str] = []
        used_memory:   list[str] = []

        # 根据任务类型按需加载相关技能
        task_type = task.get("task_type", "")
        relevant_skills = self._route_skills(task_type)
        for skill_file in relevant_skills:
            try:
                content = self.memory.read_skill(skill_file)
                loaded_memory.append(skill_file)
                context[f"skill_{skill_file}"] = content
            except FileNotFoundError:
                pass

        self._ckpt(task_id, 6, "route_relevant_memory",
                   loop_phase="route_memory",
                   working_memory={"loaded_skills": loaded_memory})

        # ── 步骤7：gene_check ─────────────────────────────────
        gene_policy = self.gene.evaluate(task)
        self._ckpt(task_id, 7, "gene_check",
                   loop_phase="gene_check",
                   working_memory={"gene_allowed": gene_policy["allowed"]})

        if not gene_policy["allowed"]:
            fail_card = self.gene.fail_card(task_id, gene_policy, task)
            self.home.write_fail_card(fail_card)
            self.home.update_task_status(task_path, "failed")
            self.audit.log(self.drea_id, "gene_violation", task_id,
                           {"policy": gene_policy})
            self.audit.log(self.drea_id, "task_failed", task_id,
                           {"reason": "gene_violation"})
            return {
                "status":    "failed",
                "task_id":   task_id,
                "reason":    "gene_violation",
                "severity":  gene_policy["severity"],
                "fail_card": fail_card,
            }

        # ── 步骤8：select_atomic_tool ─────────────────────────
        tool_name, tool_args = self._select_tool(task, context)
        self._ckpt(task_id, 8, f"select_tool:{tool_name}",
                   loop_phase="select_tool",
                   working_memory={"tool": tool_name})

        # ── 步骤9：execute_tool ───────────────────────────────
        tools_called = []
        observation  = {}
        tool_error   = None

        try:
            # GeneGuard 对工具调用参数再次检查
            tool_action = {"action": f"execute {tool_name}", "args": tool_args}
            tool_policy = self.gene.evaluate(tool_action)
            if not tool_policy["allowed"]:
                raise PermissionError(
                    f"GeneGuard blocked tool execution: "
                    f"{tool_policy['severity']}"
                )

            observation = self._dispatch_tool(tool_name, tool_args)
            tools_called.append(tool_name)
            self.audit.log(self.drea_id, "tool_executed", task_id,
                           {"tool": tool_name, "success": True})

        except (ToolError, PermissionError) as e:
            tool_error = str(e)
            self.audit.log(self.drea_id, "tool_executed", task_id,
                           {"tool": tool_name, "success": False,
                            "error": tool_error})

        self._ckpt(task_id, 9, f"execute_tool:{tool_name}",
                   loop_phase="execute_tool",
                   working_memory={
                       "tool_error": tool_error,
                       "observation_keys": list(observation.keys()),
                   })

        # ── 步骤10：write_observation ─────────────────────────
        obs_file = self.home.root / "runtime" / f"obs_{task_id}.json"
        atomic_write_json(obs_file, {
            "task_id":     task_id,
            "tool":        tool_name,
            "observation": observation,
            "tool_error":  tool_error,
            "recorded_at": now_iso(),
        })
        self._ckpt(task_id, 10, "write_observation",
                   loop_phase="write_observation")

        # ── 步骤11：update_checkpoint ─────────────────────────
        self._ckpt(task_id, 11, "pre_evaluate",
                   loop_phase="pre_evaluate",
                   working_memory={"observation": observation})

        # ── 步骤12：evaluate_result ───────────────────────────
        result = self._evaluate(task, observation, tool_error, tools_called)
        self._ckpt(task_id, 12, "evaluate_result",
                   loop_phase="evaluate",
                   working_memory={
                       "success":       result["evaluation"]["success"],
                       "quality_score": result["evaluation"]["quality_score"],
                   })

        # ── 步骤13/14：成功路径 ───────────────────────────────
        if result["evaluation"]["success"]:
            # 步骤13：crystallize_skill_or_mml
            skill_meta = self.skill.maybe_crystallize(task, result)
            if skill_meta:
                self.audit.log(self.drea_id, "skill_crystallized",
                               task_id, skill_meta)
                result["crystallized_skill"] = skill_meta

                # 步骤14：run_emergence_check
                # 检查是否需要更新 success_count
                filename = skill_meta.get("filename", "")
                if filename:
                    updated = self.skill.increment_success(filename)
                    if updated.get("emergence_candidate"):
                        emergence_result = self.emergence.check(updated)
                        if emergence_result["triggered"]:
                            self.audit.log(
                                self.drea_id,
                                "emergence_detected",
                                task_id,
                                emergence_result,
                            )
                            result["emergence"] = emergence_result

                            # 推送涌现候选到联邦
                            cfg = self.home.config_get()
                            if cfg.get("federated_enabled", True):
                                self.federated.push(
                                    sync_type  = "emergence_candidate",
                                    payload    = emergence_result,
                                    permission = "reference_only",
                                )

            self.home.update_task_status(task_path, "completed")
            self.audit.log(self.drea_id, "task_completed", task_id, result)

        # ── 步骤15：失败路径 ──────────────────────────────────
        else:
            fail_card = {
                "fail_card_version": "1.0",
                "fail_id":           new_id("fail"),
                "task_id":           task_id,
                "failed_at":         now_iso(),
                "severity":          "P3",
                "reason":            tool_error or "evaluation_failed",
                "constraint_id":     "",
                "summary":           result["evaluation"].get("notes", ""),
                "reproducible":      True,
                "repair_suggestion": "Check tool args, permissions, and input data.",
                "payload_hash":      "sha256:" + sha256_obj(task),
            }
            self.home.write_fail_card(fail_card)
            self.home.update_task_status(task_path, "failed")
            self.audit.log(self.drea_id, "task_failed", task_id,
                           {"reason": tool_error or "evaluation_failed"})
            result["fail_card"] = fail_card

        # ── 步骤16：audit + federated_sync + wait ────────────
        # 写入结果
        result_path = self.home.write_result(task_id, result)

        # CID 报告
        cfg = self.home.config_get()
        if cfg.get("cid_enabled", True):
            cid_report = self.cid.write_report(
                task, context, result,
                loaded_memory, used_memory, tools_called,
            )
            result["cid_id"] = cid_report["cid_id"]

        # 联邦同步推送（Skill摘要）
        if cfg.get("federated_enabled", True) and result["evaluation"]["success"]:
            skill_meta = result.get("crystallized_skill")
            if skill_meta:
                self.federated.push(
                    sync_type  = "skill_summary",
                    payload    = {
                        "skill_id":      skill_meta["skill_id"],
                        "domain":        skill_meta["domain"],
                        "quality_score": skill_meta["quality_score"],
                    },
                    permission = "trainable",
                )

        # 最终 checkpoint
        self._ckpt(task_id, 16, "completed", loop_phase="idle")

        # 清理观察文件
        obs_file.unlink(missing_ok=True)

        return {
            "status":   "ok",
            "task_id":  task_id,
            "success":  result["evaluation"]["success"],
            "quality":  result["evaluation"]["quality_score"],
            "result_path": str(result_path),
        }

    # ────────────────────────────────────────────────────────
    # 批量执行
    # ────────────────────────────────────────────────────────

    def run(self, limit: int = 10) -> list[dict]:
        """执行最多 limit 个待处理任务。"""
        self._ensure_init()
        results = []
        for _ in range(limit):
            r = self.run_once()
            results.append(r)
            if r.get("status") == "idle":
                break
        return results

    # ────────────────────────────────────────────────────────
    # 内部工具方法
    # ────────────────────────────────────────────────────────

    def _ensure_init(self) -> None:
        if not self._initialized:
            self.init()

    def _ckpt(
        self,
        task_id:        str | None,
        step:           int,
        last_action:    str,
        loop_phase:     str = "running",
        working_memory: dict | None = None,
        resource_state: dict | None = None,
    ) -> None:
        """写入 checkpoint 的便捷方法。"""
        self.checkpoint.update(
            drea_id         = self.drea_id,
            current_task_id = task_id,
            step            = step,
            last_action     = last_action,
            loop_phase      = loop_phase,
            working_memory  = working_memory or {},
            resource_state  = resource_state or {},
        )

    def _route_skills(self, task_type: str) -> list[str]:
        """
        根据任务类型路由相关技能文件名列表。
        从 L1 索引中查找匹配的技能。
        """
        from .util import read_text
        l1 = read_text(self.home.memory_l1)
        skills = []
        for line in l1.splitlines():
            if "L3 Skill:" in line and task_type in line:
                # 解析文件名
                parts = line.strip().split()
                for part in parts:
                    if part.endswith(".md"):
                        skills.append(part)
        return skills[:3]  # 最多加载3个相关技能，控制上下文密度

    def _select_tool(
        self,
        task:    dict,
        context: dict,
    ) -> tuple[str, dict]:
        """
        根据任务类型选择原子工具和参数。
        这是 Loop 的决策核心，实际部署时由 LLM 驱动。
        当前实现为基于规则的简单路由，用于测试和演示。
        """
        task_type = task.get("task_type", "")
        inp       = task.get("input", {})

        if task_type == "echo":
            return "file_write", {
                "rel_path": f"echo_{task['task_id']}.txt",
                "content":  inp.get("message", ""),
            }
        elif task_type == "web_fetch":
            return "web_scan", {
                "url": inp.get("url", "https://example.com"),
            }
        elif task_type == "code_exec":
            return "code_run", {
                "code":     inp.get("code", "print('hello')"),
                "level":    inp.get("level", "C0"),
                "language": inp.get("language", "python"),
            }
        elif task_type == "memory_store":
            return "memory_write", {
                "write_type": inp.get("write_type", "fact"),
                "data":       inp,
            }
        else:
            # 默认：写入任务摘要到 workspace
            return "file_write", {
                "rel_path": f"task_{task['task_id']}_summary.txt",
                "content":  (
                    f"task_type: {task_type}\n"
                    f"input: {inp}\n"
                    f"processed_at: {now_iso()}\n"
                ),
            }

    def _dispatch_tool(self, tool_name: str, tool_args: dict) -> dict:
        """将工具名分发到对应的工具方法。"""
        dispatch = {
            "file_read":         self.tools.file_read,
            "file_write":        self.tools.file_write,
            "file_patch":        self.tools.file_patch,
            "code_run":          self.tools.code_run,
            "web_scan":          self.tools.web_scan,
            "ask_human":         self.tools.ask_human,
            "memory_read":       self.tools.memory_read,
            "memory_write":      self.tools.memory_write,
            "checkpoint_update": self.tools.checkpoint_update,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            raise ToolError(f"unknown tool: {tool_name}")
        return fn(**tool_args)

    def _evaluate(
        self,
        task:        dict,
        observation: dict,
        tool_error:  str | None,
        tools_called: list[str],
    ) -> dict:
        """
        评估任务执行结果。
        当前实现：基于工具执行成功与否的简单评估。
        实际部署时由 LLM 驱动更复杂的评估逻辑。
        """
        result_id = new_id("result")
        success   = tool_error is None and bool(observation)

        quality = 0.0
        if success:
            # 基础质量分：有输出 = 0.75，有sha256验证 = +0.1
            quality = 0.75
            if "sha256" in observation:
                quality += 0.10
            if observation.get("success") is True:
                quality += 0.05
            quality = min(quality, 1.0)

        # 判断是否应该沉淀为 Skill
        target_layer = "L3_skills" if success and quality >= 0.65 else "none"

        return {
            "result_version": "1.0",
            "result_id":      result_id,
            "task_id":        task.get("task_id"),
            "completed_at":   now_iso(),
            "status":         "completed" if success else "failed",
            "steps_taken":    len(tools_called),
            "output":         observation,
            "evaluation": {
                "success":       success,
                "quality_score": quality,
                "method":        "auto_rule",
                "notes":         tool_error or "",
            },
            "memory_candidate": {
                "target_layer": target_layer,
                "title":        f"Auto skill: {task.get('task_type')}",
                "domain":       task.get("task_type", "general"),
                "content":      "",
                "permission":   "trainable",
            },
            "cid_summary": {
                "prompt_tokens":          0,
                "irrelevant_memory_items": 0,
            },
            "content_hash": "sha256:" + sha256_obj(observation),
        }
