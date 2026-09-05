"""命令行入口。

  python -m qaudit.cli audit <档案路径> [--out 输出目录] [--limit N]
  python -m qaudit.cli eval  --gold 金标准.json --pred findings.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .findings import RuleBook
from .ocr import OcrEngine
from .pipeline import UnitResult, audit_pages
from .report import ReportBuilder

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES = ROOT / "config" / "rules.yaml"
DEFAULT_MOUNTS = ROOT / "config" / "archives.yaml"
ENGINE_VERSION = "1.2.0"


def cmd_audit(args: argparse.Namespace) -> int:
    book = RuleBook.load(args.rules, packs=None if getattr(args, "no_packs", False) else "auto")
    out_dir = Path(args.out)
    ocr = OcrEngine(cache_dir=out_dir / ".ocr_cache")
    builder = ReportBuilder(out_dir, book.meta, max_evidence=args.max_evidence)

    packs = "、".join(m.get("name", "") for m in book.packs) or "无"
    print(f"[规则库] {book.meta.get('name')} v{book.meta.get('version')}｜规则包 {packs}｜启用 {len(book.all_ids)} 条")

    n = 0
    for target in args.target:
        print(f"[输入]   {target}")
        for res in audit_pages(target, book, ocr, limit=args.limit, long_side=args.long_side,
                               deskew_enabled=args.deskew):
            if isinstance(res, UnitResult):
                builder.add_units(res.doc_id, res.units, res.findings)
                extra = f"，单据级疑点 {len(res.findings)}" if res.findings else ""
                print(f"  └─ {res.doc_id[:34]:<34} 切分出 {len(res.units)} 份单据{extra}")
                continue
            n += 1
            flag = f"疑点 {len(res.findings):>2}" if res.findings else "   -  "
            print(f"  [{n:>4}] {res.ctx.doc_id[:34]:<34} p{res.ctx.page_no:<3} {flag}  {res.elapsed:.1f}s")
            builder.add_page(res.ctx, res.findings, res.elapsed)

    paths = builder.write()
    st = builder.stats()
    print(
        f"\n[完成] {st['docs']} 份档案 / {st['pages']} 页，检出疑点 {st['findings']} 条"
        f"（CRITICAL {st['by_level']['CRITICAL']}，HIGH {st['by_level']['HIGH']}，"
        f"MEDIUM {st['by_level']['MEDIUM']}，LOW {st['by_level']['LOW']}）"
    )
    print(f"[报告] {paths['html']}\n[清单] {paths['json']}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """按 (doc_id, page_no, rule_id) 对齐金标准，输出召回率/误报率。"""
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    pred = json.loads(Path(args.pred).read_text(encoding="utf-8"))

    def key(item: dict) -> tuple:
        return (item["doc_id"], int(item["page_no"]), item["rule_id"])

    # 只按显式标注计分：正样本判召回，负样本判误报，未判定的既不算对也不算错。
    # 人工复核往往只判了一页里的部分疑点，把未判定当误报会低估准确率。
    positives = {key(x) for x in gold.get("findings", gold if isinstance(gold, list) else [])}
    negatives = {key(x) for x in gold.get("false_positives", [])}
    unsure = {key(x) for x in gold.get("unsure", [])}
    all_pred = {key(x) for x in pred.get("findings", [])}

    tp = len(positives & all_pred)
    fn = len(positives - all_pred)
    fp = len(negatives & all_pred)
    unjudged = len(all_pred - positives - negatives - unsure)

    recall = tp / max(1, tp + fn)
    precision = tp / max(1, tp + fp)
    print(f"金标准：正样本 {len(positives)} 条，负样本 {len(negatives)} 条，存疑 {len(unsure)} 条")
    print(f"系统输出：{len(all_pred)} 条，其中未经人工判定 {unjudged} 条（不计分）")
    print(f"命中 TP={tp}  误报 FP={fp}  漏检 FN={fn}")
    print(f"召回率 {recall:.1%}（漏检率 {1 - recall:.1%}）｜准确率 {precision:.1%}")
    for k in sorted(positives - all_pred):
        print(f"  [漏检] {k[0]} p{k[1]} {k[2]}")
    for k in sorted(negatives & all_pred):
        print(f"  [误报] {k[0]} p{k[1]} {k[2]}")
    return 0


def cmd_gold(args: argparse.Namespace) -> int:
    """把报告里导出的人工判定合并进金标准集。

    判真 → 计入 findings（正样本）；判假 → 计入 false_positives（回归用负样本）；
    两者所在页都记为“已复核页”，评测时才对这些页计分。存疑不计入，留待复议。
    """
    gold_path = Path(args.gold)
    gold: dict = {}
    if gold_path.exists():
        gold = json.loads(gold_path.read_text(encoding="utf-8"))

    findings = {(x["doc_id"], int(x["page_no"]), x["rule_id"]): x for x in gold.get("findings", [])}
    fps = {(x["doc_id"], int(x["page_no"]), x["rule_id"]) for x in gold.get("false_positives", [])}
    reviewed = {(d, int(p)) for d, p in gold.get("reviewed_pages", [])}
    unsure: set[tuple] = {
        (x["doc_id"], int(x["page_no"]), x["rule_id"]) for x in gold.get("unsure", [])
    }

    stats = {"true": 0, "false": 0, "unsure": 0}
    for path in args.review:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        reviewer = payload.get("reviewer") or args.reviewer or ""
        for item in payload.get("adjudications", []):
            k = (item["doc_id"], int(item["page_no"]), item["rule_id"])
            verdict = item.get("verdict")
            stats[verdict] = stats.get(verdict, 0) + 1
            if verdict == "true":
                findings[k] = {
                    "doc_id": k[0], "page_no": k[1], "rule_id": k[2],
                    "note": item.get("note", ""), "reviewer": reviewer,
                }
                fps.discard(k)
                unsure.discard(k)
                reviewed.add((k[0], k[1]))
            elif verdict == "false":
                fps.add(k)
                findings.pop(k, None)
                unsure.discard(k)
                reviewed.add((k[0], k[1]))
            else:
                unsure.add(k)

    gold.setdefault(
        "_说明", "金标准集：由质量部人工判定累积而成，是验收的唯一判据。规则可改，本文件不可单方修改。"
    )
    gold["findings"] = sorted(findings.values(), key=lambda x: (x["doc_id"], x["page_no"], x["rule_id"]))
    gold["false_positives"] = [
        {"doc_id": d, "page_no": p, "rule_id": r} for d, p, r in sorted(fps)
    ]
    gold["unsure"] = [{"doc_id": d, "page_no": p, "rule_id": r} for d, p, r in sorted(unsure)]
    gold["reviewed_pages"] = [[d, p] for d, p in sorted(reviewed)]

    out_path = Path(args.out or args.gold)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(gold, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"合并判定：判真 {stats['true']}｜判假 {stats['false']}｜存疑 {stats['unsure']}")
    print(
        f"金标准现有：正样本 {len(gold['findings'])} 条，负样本 {len(gold['false_positives'])} 条，"
        f"已复核 {len(gold['reviewed_pages'])} 页"
    )
    print(f"[写出] {out_path}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """把一次 audit 的结果导入 SQLite，供服务端复核与历史查询。"""
    from .store import Store

    payload = json.loads(Path(args.report).read_text(encoding="utf-8"))
    run_id = args.run_id or f"R{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    store = Store(args.db)
    info = store.import_report(
        payload, run_id=run_id, target=args.target or str(Path(args.report).parent),
        operator=args.operator, rules_path=args.rules, engine_version=ENGINE_VERSION,
        no_packs=args.no_packs,
    )
    store.close()
    print(f"[导入] 批次 {info.run_id}：{info.pages} 页 / {info.units} 单据 / {info.findings} 条疑点")
    print(f"[数据库] {args.db}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve
    from .store import Store

    store = Store(args.db)
    if not store.has_users():
        print("[警告] 尚未创建任何账号，登录页将无法通过。请先执行：")
        print("        python -m qaudit.cli user add <用户名> --role admin --db " + str(args.db))
    store.close()
    serve(args.db, host=args.host, port=args.port, auto_port=not args.strict_port,
          archive_root=args.archive_root, rules_path=args.rules,
          out_dir=args.out, engine_version=ENGINE_VERSION,
          mounts_path=args.mounts, allow_custom_path=args.allow_custom_path,
          no_packs=args.no_packs)
    return 0


def cmd_user(args: argparse.Namespace) -> int:
    """用户管理。口令不走命令行参数，避免落进命令历史。"""
    import getpass

    from .store import ROLES, Store

    store = Store(args.db)
    try:
        if args.action == "list":
            print(f"{'用户名':<16}{'姓名':<12}{'角色':<10}{'状态':<6}{'创建时间':<22}锁定至")
            for u in store.users():
                print(f"{u['username']:<16}{u['display_name']:<12}{u['role']:<10}"
                      f"{'启用' if u['enabled'] else '停用':<6}{u['created_at']:<22}"
                      f"{u['locked_until'] or '—'}")
            return 0

        if args.action in ("add", "passwd"):
            pwd = getpass.getpass("请输入口令（至少8位，输入不回显）：")
            if pwd != getpass.getpass("请再次输入口令："):
                print("[错误] 两次输入不一致", file=sys.stderr)
                return 2
            if args.action == "add":
                store.create_user(args.username, pwd, role=args.role,
                                  display_name=args.name, operator=args.operator)
                print(f"[完成] 已创建用户 {args.username}（{ROLES[args.role]}）")
            else:
                store.set_password(args.username, pwd, operator=args.operator)
                print(f"[完成] 已重置 {args.username} 的口令，该账号既有会话已失效")
            return 0

        if args.action in ("enable", "disable"):
            store.set_user_state(args.username, enabled=(args.action == "enable"),
                                 operator=args.operator)
            print(f"[完成] {args.username} 已{'启用' if args.action == 'enable' else '停用'}")
            return 0

        if args.action == "role":
            store.set_user_state(args.username, role=args.role, operator=args.operator)
            print(f"[完成] {args.username} 角色已改为 {args.role}")
            return 0
    except (ValueError, KeyError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="qaudit", description="RecordLint 质量记录自动预审")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="审核档案并生成疑点清单")
    a.add_argument("target", nargs="+", help="档案文件或目录，可给多个")
    a.add_argument("--out", default="out", help="输出目录（默认 out）")
    a.add_argument("--rules", default=str(DEFAULT_RULES), help="规则库 yaml（通用层；同级 packs/ 下的规则包自动加载）")
    a.add_argument("--no-packs", action="store_true", help="不加载任何规则包，只用通用层")
    a.add_argument("--limit", type=int, default=None, help="最多审核页数")
    a.add_argument("--long-side", type=int, default=2200, help="页面归一化长边像素")
    a.add_argument("--deskew", action="store_true",
                   help="开启倾斜校正（会使 OCR 缓存失效，需全量重识别）")
    a.add_argument("--max-evidence", type=int, default=600,
                   help="报告内嵌截图上限，超出的疑点只列文字（全量批处理时调小）")
    a.set_defaults(func=cmd_audit)

    e = sub.add_parser("eval", help="与金标准集比对，输出漏检率")
    e.add_argument("--gold", required=True)
    e.add_argument("--pred", required=True)
    e.set_defaults(func=cmd_eval)

    g = sub.add_parser("gold", help="把报告导出的人工判定合并进金标准集")
    g.add_argument("--review", nargs="+", required=True, help="报告导出的 review_*.json，可给多个")
    g.add_argument("--gold", required=True, help="金标准集文件（不存在则新建）")
    g.add_argument("--out", default=None, help="输出路径，默认原地更新 --gold")
    g.add_argument("--reviewer", default="", help="复核人（review 文件里未填时使用）")
    g.set_defaults(func=cmd_gold)

    i = sub.add_parser("import", help="把审核结果导入 SQLite 库")
    i.add_argument("report", help="findings.json 路径")
    i.add_argument("--db", default="qaudit.db", help="SQLite 数据库路径")
    i.add_argument("--run-id", default=None, help="批次号，默认按时间生成")
    i.add_argument("--target", default="", help="审核范围说明")
    i.add_argument("--operator", default="", help="执行人")
    i.add_argument("--rules", default=str(DEFAULT_RULES), help="规则库路径（登记指纹用）")
    i.add_argument("--no-packs", action="store_true", help="指纹只算通用层（与 audit --no-packs 配套）")
    i.set_defaults(func=cmd_import)

    s_ = sub.add_parser("serve", help="启动本地审核服务（浏览器复核 + 历史查询）")
    s_.add_argument("--db", default="qaudit.db")
    s_.add_argument("--host", default="127.0.0.1", help="默认只监听本机")
    s_.add_argument("--port", type=int, default=8000)
    s_.add_argument("--strict-port", action="store_true",
                    help="端口不可用时直接报错，默认会自动改用空闲端口")
    s_.add_argument("--mounts", default=str(DEFAULT_MOUNTS),
                    help="档案来源配置（挂载点），由界面「系统 → 档案来源」维护；"
                         "文件不存在时退回 --archive-root 单根")
    s_.add_argument("--allow-custom-path", action="store_true",
                    help="允许管理员在界面直接填本机绝对路径发起审核（逃生通道，默认关闭）")
    s_.add_argument("--archive-root", default="..",
                    help="档案根目录：界面上只能选择该目录下的档案")
    s_.add_argument("--rules", default=str(DEFAULT_RULES), help="规则库 yaml（通用层；同级 packs/ 下的规则包自动加载）")
    s_.add_argument("--no-packs", action="store_true", help="不加载任何规则包，只用通用层")
    s_.add_argument("--out", default="out", help="审核产出目录")
    s_.set_defaults(func=cmd_serve)

    u = sub.add_parser("user", help="用户与权限管理")
    u.add_argument("action", choices=["add", "list", "passwd", "enable", "disable", "role"])
    u.add_argument("username", nargs="?", default="", help="用户名（list 时可省略）")
    u.add_argument("--db", default="qaudit.db")
    u.add_argument("--role", default="reviewer", choices=["admin", "reviewer", "viewer"])
    u.add_argument("--name", default="", help="姓名")
    u.add_argument("--operator", default="", help="执行人（记入审计日志）")
    u.set_defaults(func=cmd_user)
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台默认 GBK，强制 UTF-8 避免乱码
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
