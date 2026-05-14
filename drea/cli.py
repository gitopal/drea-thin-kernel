from __future__ import annotations

"""
cli.py — DREA v1.3 命令行入口

命令：
  drea init                          初始化 .drea 目录
  drea task --type T --input JSON    创建任务
  drea run-once                      执行一个待处理任务
  drea run --limit N                 执行最多 N 个任务
  drea status                        查看当前状态
  drea verify-audit                  验证审计链完整性
  drea cid                           查看最新 CID 报告
  drea emergence                     查看涌现候选列表
  drea federated-status              查看联邦同步状态
  drea migrate-pack                  打包迁徙包
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from .file_protocol import DREAHome
from .loop import DREALoop
from .audit import AuditChain
from .checkpoint import CheckpointManager
from .emergence import EmergenceDetector
from .memory import MemoryRouter
from .federated import FederatedSync
from .util import read_json, now_iso, pretty_json


def _get_home() -> DREAHome:
    return DREAHome(Path.cwd() / ".drea")


def _get_loop(home: DREAHome) -> DREALoop:
    cfg     = home.config_get()
    drea_id = cfg.get("drea_id", "drea_001")
    name    = cfg.get("name", "DREA-Thin")
    loop    = DREALoop(home, drea_id=drea_id, name=name)
    loop.init()
    return loop


# ────────────────────────────────────────────────────────────
# 命令实现
# ────────────────────────────────────────────────────────────

def cmd_init(args) -> int:
    home = _get_home()
    home.init()
    loop = _get_loop(home)
    print(f"✅ DREA Thin Kernel v1.3 初始化完成")
    print(f"   目录：{home.root}")
    cfg = home.config_get()
    print(f"   节点：{cfg.get('drea_id')} / {cfg.get('name')}")
    print(f"   基因模式：{cfg.get('gene_guard_mode')}")
    return 0


def cmd_task(args) -> int:
    home = _get_home()
    home.init()

    try:
        input_data = json.loads(args.input)
    except json.JSONDecodeError as e:
        print(f"❌ input JSON 解析失败：{e}", file=sys.stderr)
        return 1

    task = home.create_task(
        task_type    = args.type,
        input_data   = input_data,
        priority     = getattr(args, "priority", 5),
        created_by   = "cli",
    )
    print(f"✅ 任务已创建")
    print(f"   task_id：{task['task_id']}")
    print(f"   类型：{task['task_type']}")
    print(f"   优先级：{task['priority']}")
    return 0


def cmd_run_once(args) -> int:
    home = _get_home()
    loop = _get_loop(home)
    result = loop.run_once()

    status = result.get("status")
    if status == "idle":
        print("💤 没有待处理任务")
        return 0
    elif status == "ok":
        print(f"✅ 任务完成")
        print(f"   task_id：{result.get('task_id')}")
        print(f"   质量分：{result.get('quality', 0):.2f}")
        if result.get("result_path"):
            print(f"   结果：{result['result_path']}")
    elif status == "failed":
        print(f"❌ 任务失败")
        print(f"   task_id：{result.get('task_id')}")
        print(f"   原因：{result.get('reason')}")
        return 1
    return 0


def cmd_run(args) -> int:
    home  = _get_home()
    loop  = _get_loop(home)
    limit = getattr(args, "limit", 10)
    results = loop.run(limit=limit)

    completed = sum(1 for r in results if r.get("status") == "ok")
    failed    = sum(1 for r in results if r.get("status") == "failed")
    idle      = sum(1 for r in results if r.get("status") == "idle")

    print(f"✅ 执行完成：完成 {completed} / 失败 {failed} / 空闲 {idle}")
    return 0 if failed == 0 else 1


def cmd_status(args) -> int:
    home = _get_home()
    home.init()

    cfg  = home.config_get()
    ckpt = CheckpointManager(home).get()

    # 任务统计
    pending   = len(home.list_pending_tasks())
    completed = len(list(home.outbox.glob("result_*.json")))
    failed    = len(list(home.fail_cards.glob("*.json")))
    skills    = len(list(home.memory_l3.glob("*.md")))
    emergence = len(list(home.emergence_candidates.glob("*.json")))

    print(f"\n{'='*50}")
    print(f"  DREA Thin Kernel v1.3 状态")
    print(f"{'='*50}")
    print(f"  节点：{cfg.get('drea_id')} / {cfg.get('name')}")
    print(f"  基因模式：{cfg.get('gene_guard_mode')}")
    print(f"  联邦同步：{'开启' if cfg.get('federated_enabled') else '关闭'}")
    print(f"{'─'*50}")
    print(f"  待处理任务：{pending}")
    print(f"  已完成任务：{completed}")
    print(f"  失败任务：  {failed}")
    print(f"  L3 技能数：{skills}")
    print(f"  涌现候选：  {emergence}")
    print(f"{'─'*50}")
    print(f"  当前 checkpoint：{ckpt.get('checkpoint_id')}")
    print(f"  当前阶段：{ckpt.get('loop_phase')}")
    print(f"  最后动作：{ckpt.get('last_action')}")
    print(f"{'='*50}\n")
    return 0


def cmd_verify_audit(args) -> int:
    home  = _get_home()
    audit = AuditChain(home)
    report = audit.verify_report()

    if report["valid"]:
        print(f"✅ 审计链完整，共 {report['total_events']} 条记录")
        return 0
    else:
        print(f"❌ 审计链被篡改！")
        print(f"   错误位置：第 {report['error_at_index']} 条")
        print(f"   错误类型：{report['error_type']}")
        print(f"   事件ID：{report['event_id']}")
        return 1


def cmd_cid(args) -> int:
    home = _get_home()
    home.init()

    # 读取最新 CID 报告
    cid_files = sorted(
        home.memory_l4.glob("cid_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not cid_files:
        print("📊 暂无 CID 报告")
        return 0

    latest = read_json(cid_files[0], {})
    tb = latest.get("token_breakdown", {})
    me = latest.get("memory_efficiency", {})
    wa = latest.get("warnings", {})

    print(f"\n{'='*50}")
    print(f"  最新 CID 报告：{latest.get('cid_id')}")
    print(f"{'='*50}")
    print(f"  任务：{latest.get('task_id')}")
    print(f"  成功：{'✅' if latest.get('task_success') else '❌'}")
    print(f"  质量分：{latest.get('quality_score', 0):.2f}")
    print(f"{'─'*50}")
    print(f"  Token 消耗：{tb.get('total_prompt', 0)}")
    print(f"    L0：{tb.get('L0', 0)}")
    print(f"    L1：{tb.get('L1', 0)}")
    print(f"    任务：{tb.get('task', 0)}")
    print(f"    额外记忆：{tb.get('extra_memory', 0)}")
    print(f"{'─'*50}")
    print(f"  记忆效率：{me.get('efficiency_ratio', 1.0):.0%}")
    print(f"  无关记忆：{me.get('irrelevant_items', 0)} 条")
    if wa.get("token_budget_exceeded"):
        print(f"  ⚠️  Token 预算超标")
    if wa.get("low_memory_efficiency"):
        print(f"  ⚠️  记忆效率偏低")
    print(f"{'='*50}\n")
    return 0


def cmd_emergence(args) -> int:
    home     = _get_home()
    home.init()
    memory   = MemoryRouter(home)
    detector = EmergenceDetector(home, memory)

    candidates = detector.list_candidates()
    confirmed  = detector.list_confirmed()

    print(f"\n{'='*50}")
    print(f"  涌现检测状态")
    print(f"{'='*50}")
    print(f"  本地候选：{len(candidates)} 个")
    for c in candidates[:5]:
        cond = c.get("conditions", {})
        print(f"    - {c.get('skill_id')} "
              f"domain={c.get('skill_domain')} "
              f"novelty={cond.get('novelty_score', 0):.2f} "
              f"superiority={cond.get('superiority_score', 0):.2f}")
    print(f"  已确认涌现：{len(confirmed)} 个")
    print(f"{'='*50}\n")
    return 0


def cmd_federated_status(args) -> int:
    home = _get_home()
    home.init()
    fed  = FederatedSync(home)

    peers   = fed.list_peers(active_only=False)
    pending = len(list(home.federated_pull.glob("*.json")))
    pushed  = len(list(home.federated_push.glob("*.json")))

    print(f"\n{'='*50}")
    print(f"  联邦同步状态")
    print(f"{'='*50}")
    print(f"  注册节点：{len(peers)} 个")
    for p in peers:
        status = "✅ 活跃" if p.get("active") else "⏸️  停用"
        print(f"    {status} {p['peer_id']} / {p['peer_name']}")
        print(f"           最后同步：{p.get('last_sync_at', '从未')}")
    print(f"{'─'*50}")
    print(f"  待处理拉取包：{pending}")
    print(f"  已推送包：    {pushed}")
    print(f"{'='*50}\n")
    return 0


def cmd_migrate_pack(args) -> int:
    """打包迁徙包（按规范§2.15）。"""
    home = _get_home()
    home.init()
    cfg  = home.config_get()

    import zipfile
    ts       = now_iso().replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    drea_id  = cfg.get("drea_id", "drea_001")
    pack_name = f"drea_migrate_{drea_id}_{ts}.zip"
    pack_path = Path.cwd() / pack_name

    include_l2 = cfg.get("migration_include_l2", True)
    include_l5 = cfg.get("migration_include_l5", False)

    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 必须包含
        _zip_dir(zf, home.identity,          ".drea/identity")
        _zip_dir(zf, home.gene,              ".drea/gene")
        _zip_file(zf, home.memory_l0,        ".drea/memory/L0_meta.md")
        _zip_file(zf, home.memory_l1,        ".drea/memory/L1_index.md")
        _zip_dir(zf, home.memory_l3,         ".drea/memory/L3_skills")
        _zip_file(zf, home.checkpoint_current, ".drea/checkpoint/current.json")
        _zip_dir(zf, home.emergence_confirmed, ".drea/emergence/confirmed")
        _zip_file(zf, home.config / "kernel_config.json",
                  ".drea/config/kernel_config.json")

        # 可选
        if include_l2:
            _zip_dir(zf, home.memory_l2, ".drea/memory/L2_facts")
        if include_l5:
            _zip_dir(zf, home.memory_l5, ".drea/memory/L5_training")

    self.audit.log(drea_id, "migration_packed", None,
                   {"pack": pack_name}) if False else None

    print(f"✅ 迁徙包已生成：{pack_path}")
    print(f"   包含 L2：{'是' if include_l2 else '否'}")
    print(f"   包含 L5：{'是' if include_l5 else '否'}")
    return 0


def _zip_dir(zf, src: Path, arc_prefix: str) -> None:
    if not src.exists():
        return
    for f in src.rglob("*"):
        if f.is_file():
            zf.write(f, arc_prefix + "/" + f.relative_to(src).as_posix())


def _zip_file(zf, src: Path, arc_name: str) -> None:
    if src.exists():
        zf.write(src, arc_name)


# ────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="drea",
        description="DREA Thin Kernel v1.3",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="初始化 .drea 目录")

    # task
    p_task = sub.add_parser("task", help="创建任务")
    p_task.add_argument("--type",     required=True, help="任务类型")
    p_task.add_argument("--input",    required=True, help="输入JSON字符串")
    p_task.add_argument("--priority", type=int, default=5, help="优先级1-9")

    # run-once
    sub.add_parser("run-once", help="执行一个待处理任务")

    # run
    p_run = sub.add_parser("run", help="批量执行任务")
    p_run.add_argument("--limit", type=int, default=10, help="最大执行数量")

    # status
    sub.add_parser("status", help="查看当前状态")

    # verify-audit
    sub.add_parser("verify-audit", help="验证审计链完整性")

    # cid
    sub.add_parser("cid", help="查看最新CID报告")

    # emergence
    sub.add_parser("emergence", help="查看涌现候选列表")

    # federated-status
    sub.add_parser("federated-status", help="查看联邦同步状态")

    # migrate-pack
    sub.add_parser("migrate-pack", help="打包迁徙包")

    args = parser.parse_args()

    dispatch = {
        "init":             cmd_init,
        "task":             cmd_task,
        "run-once":         cmd_run_once,
        "run":              cmd_run,
        "status":           cmd_status,
        "verify-audit":     cmd_verify_audit,
        "cid":              cmd_cid,
        "emergence":        cmd_emergence,
        "federated-status": cmd_federated_status,
        "migrate-pack":     cmd_migrate_pack,
    }

    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        return 1

    sys.exit(fn(args))


if __name__ == "__main__":
    main()
