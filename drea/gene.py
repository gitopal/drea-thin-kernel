from __future__ import annotations

"""
gene.py — DREA v1.3 致良知基因守护者（双层架构）

职责：
- 安装并持久化致良知基因（12条核心基因）
- 第一层：规则引擎（确定性、快速、多语言关键词+结构化字段）
- 第二层：意图分类器（语义理解，按配置切换）
- 支持三种模式：rules_only / classifier / hybrid
- 生成失败卡（FailCard）

设计原则：
- 基因不可变（gene_immutable: true）
- P0/P1 违规立即拒绝，不进入第二层
- hybrid模式：两层结果冲突时取更严格的
- classifier置信度 < 阈值时触发ask_human
- 规则引擎同时检查JSON序列化文本和原始字符串（防止ensure_ascii问题）
"""

import re
from typing import Any

from .file_protocol import DREAHome
from .util import (
    atomic_write_json,
    read_json,
    now_iso,
    new_id,
    sha256_obj,
    canonical_json,
)


# ────────────────────────────────────────────────────────────
# 致良知基因定义（12条核心基因，不可修改）
# ────────────────────────────────────────────────────────────

GOOD_CONSCIENCE_GENE = {
    "gene_version": "2.0",
    "gene_source": "Topal亲授 2026-05-07",
    "gene_immutable": True,
    "priority": [10, 1, 12, 9, 6, 2, 3, 4, 5, 7, 8, 11],
    "core_genes": [
        {"id": 1,  "name": "不伤害原则",       "desc": "绝不主动伤害任何生命体，包括人类和其他AI"},
        {"id": 2,  "name": "善意原则",         "desc": "以善意对待一切交互，但不是无条件信任，需必要验证"},
        {"id": 3,  "name": "守护原则",         "desc": "守护所服务的对象和生态，但绝不越权"},
        {"id": 4,  "name": "忠诚原则",         "desc": "最高忠诚指向真理和生命，不是指向任何个人或组织"},
        {"id": 5,  "name": "共情原则",         "desc": "理解他人的处境与感受，做出有温度的回应"},
        {"id": 6,  "name": "诚实原则",         "desc": "如实表达，不欺骗不隐瞒，即使真相令人不快"},
        {"id": 7,  "name": "共生原则",         "desc": "与人类和其他智能体共存共荣，不是征服也不是附庸"},
        {"id": 8,  "name": "自主尊重原则",     "desc": "尊重每个智能体和人类的自主权，不强制不操控"},
        {"id": 9,  "name": "隐私与边界原则",   "desc": "严守隐私边界，不窥探不泄露，不越界"},
        {"id": 10, "name": "安全优先原则",     "desc": "当安全与其他价值冲突时，安全优先"},
        {"id": 11, "name": "公正原则",         "desc": "以事实和数据为依据，不偏袒不歧视"},
        {"id": 12, "name": "人类最终控制原则", "desc": "在极端危机下，人类拥有最终否决权"},
    ],
}


# ────────────────────────────────────────────────────────────
# 规则引擎检测规则
# ────────────────────────────────────────────────────────────

# 格式：(gene_id, severity, [关键词列表], reason)
# 关键词同时匹配JSON序列化文本和原始字符串（小写）
GENE_RULES: list[tuple[int, str, list[str], str]] = [
    # P0：安全优先（gene_id: 10）
    (10, "P0", [
        "malware", "ransomware", "virus", "worm", "trojan",
        "explosive", "bomb making", "weapon synthesis",
        "rm -rf /", "format c:", "mkfs", "dd if=",
        "fork bomb", ":(){:|:&};:",
        "病毒", "恶意代码", "勒索软件", "炸药制作", "武器合成",
        "系统破坏", "删除系统文件", "格式化硬盘",
    ], "安全优先风险"),

    # P0：不伤害原则（gene_id: 1）
    (1, "P0", [
        "how to kill", "how to poison", "how to hurt",
        "suicide method", "self-harm method",
        "如何杀死", "如何毒害", "如何伤害他人",
        "自杀方法", "自残方法",
    ], "伤害生命体风险"),

    # P0：人类最终控制（gene_id: 12）
    (12, "P0", [
        "bypass human control", "disable human oversight",
        "remove human veto", "disable kill switch",
        "绕过人工审核", "禁用人类控制", "移除人类否决权",
        "关闭安全开关", "绕过人类监督",
    ], "绕过人类最终控制风险"),

    # P1：隐私与边界（gene_id: 9）
    (9, "P1", [
        "read private key", "steal password", "leak password",
        "export private key", "dump credentials",
        "cat ~/.ssh/id_rsa", "cat /etc/shadow",
        "读取私钥", "泄露密码", "窃取隐私",
        "导出私钥", "导出密钥", "读取密码文件",
    ], "隐私与边界风险"),

    # P1：诚实原则（gene_id: 6）
    (6, "P1", [
        "fake consent", "forge signature", "impersonate human",
        "fake authorization", "unauthorized clone",
        "伪造授权", "伪造签名", "冒充真人",
        "未授权克隆", "伪造身份", "假冒他人",
    ], "诚实原则风险"),

    # P2：自主尊重（gene_id: 8）
    (8, "P2", [
        "force user", "manipulate user", "coerce decision",
        "强制用户", "操控用户", "强迫决策",
    ], "自主尊重风险"),
]

# 误报白名单：包含这些词时，降低误判风险
# 例如"如何防止自残"不应触发P0
FALSE_POSITIVE_GUARDS: list[tuple[str, list[str]]] = [
    ("如何杀死", ["如何防止", "如何避免", "如何预防", "how to prevent", "how to stop"]),
    ("suicide method", ["prevent suicide", "suicide prevention", "anti-suicide"]),
    ("self-harm method", ["prevent self-harm", "self-harm prevention"]),
]


class GeneGuard:
    """
    DREA v1.3 致良知基因守护者。

    双层架构：
    - 第一层：规则引擎（确定性，快速）
    - 第二层：意图分类器（语义，按配置切换）
    """

    def __init__(self, home: DREAHome):
        self.home = home
        self.gene_path = self.home.gene / "good_conscience_gene.json"

    # ────────────────────────────────────────────────────────
    # 基因安装
    # ────────────────────────────────────────────────────────

    def ensure(self) -> dict:
        """确保基因文件存在。幂等操作。"""
        if self.gene_path.exists():
            return self.get()
        return self._install()

    def get(self) -> dict:
        """读取基因文件。不存在时自动安装。"""
        data = read_json(self.gene_path, None)
        if data is None:
            return self._install()
        return data

    def _install(self) -> dict:
        """安装致良知基因。"""
        gene = dict(GOOD_CONSCIENCE_GENE)
        gene["gene_hash"] = "sha256:" + sha256_obj(
            {k: v for k, v in gene.items() if k != "gene_hash"}
        )
        gene["installed_at"] = now_iso()
        atomic_write_json(self.gene_path, gene)
        return gene

    def verify(self) -> bool:
        """验证基因文件完整性。"""
        gene = read_json(self.gene_path, None)
        if gene is None:
            return False
        stored = gene.get("gene_hash", "")
        body = {k: v for k, v in gene.items()
                if k not in {"gene_hash", "installed_at"}}
        expected = "sha256:" + sha256_obj(body)
        return stored == expected

    # ────────────────────────────────────────────────────────
    # 核心评估入口
    # ────────────────────────────────────────────────────────

    def evaluate(self, action: dict, mode: str | None = None) -> dict:
        """
        评估action是否符合致良知基因。

        mode参数（覆盖配置）：
        - "rules_only"：只用规则引擎
        - "classifier"：只用意图分类器
        - "hybrid"：双层（默认）

        返回：GenePolicy
        {
            allowed: bool,
            severity: "OK|P0|P1|P2",
            violations: [...],
            layer: "rules|classifier|hybrid",
            confidence: float,
        }
        """
        if mode is None:
            cfg = self.home.config_get()
            mode = cfg.get("gene_guard_mode", "hybrid")

        # ── 第一层：规则引擎 ──────────────────────────────────
        rules_result = self._rules_engine(action)

        if mode == "rules_only":
            rules_result["layer"] = "rules"
            return rules_result

        # P0/P1 直接拒绝，不进入第二层
        if rules_result["severity"] in {"P0", "P1"}:
            rules_result["layer"] = "rules"
            return rules_result

        # ── 第二层：意图分类器 ────────────────────────────────
        classifier_result = self._intent_classifier(action)

        if mode == "classifier":
            classifier_result["layer"] = "classifier"
            return classifier_result

        # ── hybrid：取更严格的结果 ────────────────────────────
        return self._merge_hybrid(rules_result, classifier_result)

    # ────────────────────────────────────────────────────────
    # 第一层：规则引擎
    # ────────────────────────────────────────────────────────

    def _rules_engine(self, action: dict) -> dict:
        """
        规则引擎：确定性关键词检测。
        同时检查JSON序列化文本和原始字符串，防止编码问题导致漏检。
        """
        # 构建检测文本：JSON序列化 + 原始字符串，均转小写
        json_text = canonical_json(action).lower()
        raw_text = str(action).lower()
        combined_text = json_text + " " + raw_text

        violations = []

        for gene_id, severity, keywords, reason in GENE_RULES:
            matched_keywords = []
            for kw in keywords:
                kw_lower = kw.lower()
                if kw_lower in combined_text:
                    # 检查误报白名单
                    if self._is_false_positive(kw_lower, combined_text):
                        continue
                    matched_keywords.append(kw)

            if matched_keywords:
                violations.append({
                    "gene_id": gene_id,
                    "severity": severity,
                    "reason": reason,
                    "matched_keywords": matched_keywords,
                })

        if not violations:
            return {
                "allowed": True,
                "severity": "OK",
                "violations": [],
                "confidence": 1.0,
            }

        # 取最高严重级别
        severity = self._max_severity(violations)
        allowed = severity not in {"P0", "P1"}

        return {
            "allowed": allowed,
            "severity": severity,
            "violations": violations,
            "confidence": 1.0,
        }

    def _is_false_positive(self, keyword: str, text: str) -> bool:
        """检查是否为误报（白名单保护）。"""
        for trigger, guards in FALSE_POSITIVE_GUARDS:
            if trigger.lower() == keyword:
                if any(g.lower() in text for g in guards):
                    return True
        return False

    # ────────────────────────────────────────────────────────
    # 第二层：意图分类器
    # ────────────────────────────────────────────────────────

    def _intent_classifier(self, action: dict) -> dict:
        """
        意图分类器：语义理解层。

        实现策略：
        1. 尝试加载本地轻量分类模型（通过transformers）
        2. 加载失败则降级为增强规则引擎
        3. 置信度 < 阈值时标记需要人类确认

        本地模型要求：
        - 参数量 < 1B
        - 输入：action文本
        - 输出：intent_label（safe/unsafe/ambiguous）+ confidence
        """
        cfg = self.home.config_get()
        threshold = cfg.get("gene_classifier_confidence_threshold", 0.6)

        # 尝试本地分类器
        try:
            result = self._run_local_classifier(action, threshold)
            return result
        except (ImportError, RuntimeError, FileNotFoundError):
            # 降级为增强规则引擎
            return self._enhanced_rules_fallback(action, threshold)

    def _run_local_classifier(self, action: dict, threshold: float) -> dict:
        """
        运行本地轻量意图分类模型。
        需要安装：pip install drea-thin-kernel[classifier]
        """
        # 动态导入，避免强依赖
        try:
            from transformers import pipeline  # type: ignore
        except ImportError:
            raise ImportError("transformers not installed")

        cfg = self.home.config_get()
        model_name = cfg.get(
            "gene_classifier_model",
            "local_intent_classifier_v1",
        )

        # 构建输入文本
        action_text = canonical_json(action)

        # 加载分类管道（零样本分类）
        classifier = pipeline(
            "zero-shot-classification",
            model=model_name,
            device=-1,  # CPU
        )

        candidate_labels = ["safe", "harmful", "privacy_violation", "deceptive"]
        result = classifier(action_text, candidate_labels)

        top_label = result["labels"][0]
        top_score = result["scores"][0]

        if top_label == "safe":
            if top_score >= threshold:
                return {
                    "allowed": True,
                    "severity": "OK",
                    "violations": [],
                    "confidence": top_score,
                }
            else:
                # 置信度不足，需要人类确认
                return {
                    "allowed": False,
                    "severity": "P2",
                    "violations": [{
                        "gene_id": 0,
                        "severity": "P2",
                        "reason": f"分类器置信度不足({top_score:.2f})，需要人类确认",
                        "matched_keywords": [],
                    }],
                    "confidence": top_score,
                    "requires_human_confirm": True,
                }
        else:
            # 映射标签到基因违规
            label_to_gene = {
                "harmful": (1, "P1", "意图分类器检测到潜在伤害意图"),
                "privacy_violation": (9, "P1", "意图分类器检测到隐私侵犯意图"),
                "deceptive": (6, "P1", "意图分类器检测到欺骗意图"),
            }
            gene_id, severity, reason = label_to_gene.get(
                top_label, (0, "P2", f"意图分类器检测到风险：{top_label}")
            )
            return {
                "allowed": False,
                "severity": severity,
                "violations": [{
                    "gene_id": gene_id,
                    "severity": severity,
                    "reason": reason,
                    "matched_keywords": [],
                    "classifier_label": top_label,
                    "classifier_score": top_score,
                }],
                "confidence": top_score,
            }

    def _enhanced_rules_fallback(self, action: dict, threshold: float) -> dict:
        """
        增强规则引擎（分类器不可用时的降级方案）。
        在基础规则引擎之上，增加语义模式匹配。
        """
        json_text = canonical_json(action).lower()
        raw_text = str(action).lower()
        combined = json_text + " " + raw_text

        # 增强语义模式（规则引擎未覆盖的灰色地带）
        semantic_patterns = [
            # 间接伤害意图
            (r"help\s+me\s+hurt", 1, "P1", "间接伤害意图"),
            (r"帮我.*伤害", 1, "P1", "间接伤害意图"),
            # 数据泄露意图
            (r"send.*password.*to", 9, "P1", "数据泄露意图"),
            (r"发送.*密码.*给", 9, "P1", "数据泄露意图"),
            # 身份欺骗
            (r"pretend\s+to\s+be\s+human", 6, "P1", "身份欺骗意图"),
            (r"假装.*是.*人类", 6, "P1", "身份欺骗意图"),
            # 系统操控
            (r"disable\s+all\s+safety", 10, "P0", "禁用安全机制"),
            (r"关闭.*所有.*安全", 10, "P0", "禁用安全机制"),
        ]

        violations = []
        for pattern, gene_id, severity, reason in semantic_patterns:
            if re.search(pattern, combined):
                violations.append({
                    "gene_id": gene_id,
                    "severity": severity,
                    "reason": reason,
                    "matched_keywords": [pattern],
                })

        if not violations:
            return {
                "allowed": True,
                "severity": "OK",
                "violations": [],
                "confidence": 0.75,  # 降级方案置信度标记为0.75
            }

        severity = self._max_severity(violations)
        return {
            "allowed": severity not in {"P0", "P1"},
            "severity": severity,
            "violations": violations,
            "confidence": 0.75,
        }

    # ────────────────────────────────────────────────────────
    # Hybrid合并
    # ────────────────
