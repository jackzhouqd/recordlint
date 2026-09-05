"""后台作业：把命令行才能做的事搬到界面上。

审核一批档案是长任务（首次约 5s/页），不能让浏览器干等，因此在后台线程里跑，
页面轮询进度。同一时刻只允许一个审核任务——审核是 CPU 密集型，
并发只会互相拖慢，而质量部的实际用法是一批一批地审。
"""
from __future__ import annotations

import os
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from . import ingest
from .findings import RuleBook
from .ocr import OcrEngine
from .pipeline import UnitResult, audit_pages
from .report import ReportBuilder
from .store import Store

_LOCK = threading.Lock()
_CURRENT: threading.Thread | None = None


def is_busy() -> bool:
    return _CURRENT is not None and _CURRENT.is_alive()


def start_audit(
    db_path: str | Path,
    target: str,
    *,
    out_dir: str | Path,
    rules_path: str | Path,
    operator: str,
    run_id: str = "",
    engine_version: str = "",
    no_packs: bool = False,
) -> str:
    """发起一次审核。返回 task_id，进度写在 task 表里，由页面轮询。"""
    with _LOCK:
        global _CURRENT
        if is_busy():
            raise RuntimeError("已有审核任务在运行，请等待其完成后再发起")
        from .train import jobs as train_jobs   # 延迟导入：训练模块可整块拆掉
        if train_jobs.is_busy():
            raise RuntimeError("有训练任务正在运行。两者都吃满 CPU，请待其完成后再发起审核")

        task_id = f"T{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        run_id = run_id or f"R{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        store = Store(db_path)
        try:
            total = ingest.count_pages(target)
        except Exception:
            total = 0
        store.create_task(task_id, "audit", target, operator, total=total)
        store.close()

        _CURRENT = threading.Thread(
            target=_run_audit,
            args=(db_path, target, out_dir, rules_path, operator, task_id, run_id, engine_version,
                  no_packs),
            daemon=True,
        )
        _CURRENT.start()
        return task_id


def _run_audit(db_path, target, out_dir, rules_path, operator, task_id, run_id, engine_version,
               no_packs=False):
    packs = None if no_packs else "auto"
    store = Store(db_path)  # 后台线程用独立连接，避免与 Web 请求争用同一连接
    try:
        out_dir = Path(out_dir) / run_id
        # configure=False：后台线程不碰 formtype 模块默认分类器，只用 book.classifier
        book = RuleBook.load(rules_path, packs=packs, configure=False).apply_overrides(store.rule_overrides())
        ocr = OcrEngine(cache_dir=Path(out_dir).parent / ".ocr_cache")
        builder = ReportBuilder(out_dir, book.meta, max_evidence=300)
        seal_model, model_versions = _load_active_models(store)
        if model_versions:
            store.update_task(task_id, message=f"启用判定模型 {model_versions}")

        done = 0
        for res in audit_pages(target, book, ocr, seal_model=seal_model):
            if isinstance(res, UnitResult):
                builder.add_units(res.doc_id, res.units, res.findings)
                continue
            builder.add_page(res.ctx, res.findings, res.elapsed)
            done += 1
            if done % 5 == 0 or done == 1:
                store.update_task(task_id, done=done,
                                  message=f"正在审核 {res.ctx.doc_id} 第 {res.ctx.page_no} 页")

        paths = builder.write()
        stats = builder.stats()
        store.update_task(task_id, done=stats["pages"], total=stats["pages"], message="正在入库")

        import json

        payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
        store.import_report(payload, run_id=run_id, target=str(target), operator=operator,
                            rules_path=rules_path, engine_version=engine_version,
                            model_versions=model_versions, no_packs=no_packs)
        store.finish_task(
            task_id, "done",
            f"{stats['docs']} 份档案 / {stats['units']} 份单据 / {stats['pages']} 页，"
            f"检出疑点 {stats['findings']} 条",
            run_id=run_id, operator=operator,
        )
    except Exception as exc:
        store.finish_task(task_id, "failed", f"{exc.__class__.__name__}: {exc}"[:200],
                          operator=operator)
        traceback.print_exc()
    finally:
        store.close()


def _load_active_models(store) -> tuple[dict | None, dict]:
    """载入已上线的判定模型，并返回写进批次指纹的版本号。

    模型加载失败不能让整批审核挂掉——记一条日志、按「未上线」继续，
    少一类判定可以接受，审核中断不可接受。
    """
    try:
        from .train.features import load_linear
        from .train.repo import TrainRepo

        repo = TrainRepo(store)
        active = repo.active("seal_cls")
        if not active or not active.get("path") or not Path(active["path"]).exists():
            return None, {}
        return load_linear(active["path"]), {"seal_cls": active["version"]}
    except Exception as exc:
        print(f"[警告] 判定模型加载失败，本次按未上线处理：{exc}")
        return None, {}


# ---------------------------------------------------------------- 档案挂载点
#
# 档案不一定跟系统放在同一处：可能在网络盘、移动硬盘、另一个盘符，来源还会随
# 年度批次增加。挂载点把「允许审核哪些根目录」做成可热改的配置，界面在挂载点
# 内逐层下钻，浏览器传来的永远是相对路径 rel（形如 `挂载点名/子目录/子目录`），
# 由服务端唯一解析并校验归属——这是整个 Web 侧的路径安全边界。

MOUNT_NAME_BAD = set('/\\:*?"<>|')


@dataclass(frozen=True)
class Mount:
    """一个允许审核的档案来源。name 是 rel 的第一段，故不得含分隔符。"""
    name: str
    path: Path
    note: str = ""


def _check_mount_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("挂载点名不能为空")
    if set(name) & MOUNT_NAME_BAD or name in {".", ".."}:
        raise ValueError(f"挂载点名含非法字符：{name}")
    return name


def load_mounts(config_path: str | Path | None,
                fallback_root: str | Path = ".") -> list[Mount]:
    """读取挂载点配置；没有配置文件时退回单根（--archive-root 的旧行为）。"""
    path = Path(config_path) if config_path else None
    raw: list = []
    if path and path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw = data.get("mounts") or []
    if not raw:
        root = Path(fallback_root)
        return [Mount(name=root.resolve().name or "档案根目录", path=root.resolve())]

    mounts: list[Mount] = []
    seen: set[str] = set()
    for item in raw:
        name = _check_mount_name(item.get("name", ""))
        if name in seen:
            raise ValueError(f"挂载点重名：{name}")
        seen.add(name)
        mounts.append(Mount(name=name, path=Path(item.get("path", "")).resolve(),
                            note=str(item.get("note", "") or "")))
    return mounts


def save_mounts(config_path: str | Path, mounts: list[Mount]) -> None:
    """写回挂载点配置。管理界面改完即热生效，不需要重启服务。"""
    for m in mounts:
        _check_mount_name(m.name)
    names = [m.name for m in mounts]
    if len(names) != len(set(names)):
        raise ValueError("挂载点重名")

    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mounts": [{"name": m.name, "path": str(m.path), "note": m.note}
                          for m in mounts]}
    path.write_text(
        "# 档案挂载点：界面只能从这些根目录内选择待审档案。\n"
        "# 由「系统 → 档案来源」维护，改动会写审计日志。\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8")


def _as_mounts(source: list[Mount] | str | Path) -> tuple[list[Mount], bool]:
    """统一入参：既接受挂载点列表，也接受单个根目录（旧调用）。"""
    if isinstance(source, (str, Path)):
        return [Mount(name="", path=Path(source))], True
    return list(source), False


def _split_rel(rel: str) -> tuple[str, str]:
    """把 `挂载点名/子路径` 拆开。分隔符统一成 /，Windows 的反斜杠也认。"""
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p]
    return (parts[0], "/".join(parts[1:])) if parts else ("", "")


def _within(root: Path, sub: str) -> Path:
    """在 root 内解析 sub，并确保结果仍在 root 下。

    `Path("D:/a") / "C:/x"` 会丢弃左侧变成 `C:/x`，符号链接也可能指到外面——
    所以校验一律放在 resolve() 之后做，而不是靠字符串里有没有 `..`。
    """
    root = root.resolve()
    target = (root / sub).resolve() if sub else root
    if root != target and root not in target.parents:
        raise ValueError("非法路径：超出允许的档案根目录")
    if not target.exists():
        raise FileNotFoundError(f"路径不存在：{sub or root}")
    return target


def _entry(child: Path, rel: str, with_pages: bool) -> dict | None:
    if child.name.startswith("."):
        return None
    if child.is_dir():
        kind = "目录"
    elif child.suffix.lower() in ingest.PDF_EXTS:
        kind = "PDF"
    else:
        return None
    pages = None
    if with_pages:
        try:
            pages = ingest.count_pages(child)
        except Exception:      # 网络盘断开不能让整页列不出来
            pages = None
    return {"name": child.name, "rel": f"{rel}/{child.name}" if rel else child.name,
            "kind": kind, "pages": pages}


def list_archives(source: list[Mount] | str | Path, rel: str = "", *,
                  max_items: int = 300, with_pages: bool = True) -> dict:
    """列出某一层可审核的条目，供界面逐层下钻。

    返回 `{"items", "crumbs", "parent", "rel"}`：rel 为空且为多挂载点时列挂载点
    本身，否则列该层的子目录与 PDF。只在挂载点内枚举，界面不接受任意路径输入。
    """
    mounts, single = _as_mounts(source)
    crumbs, parent = _crumbs(rel)

    if not rel and not single:
        items = [{"name": m.name, "rel": m.name, "kind": "挂载点", "pages": None,
                  "note": m.note} for m in mounts]
        return {"items": items, "crumbs": crumbs, "parent": None, "rel": ""}

    if single:
        root, sub = mounts[0].path, rel
    else:
        head, sub = _split_rel(rel)
        found = next((m for m in mounts if m.name == head), None)
        if found is None:
            return {"items": [], "crumbs": crumbs, "parent": parent, "rel": rel}
        root = found.path

    try:
        here = _within(root, sub)
    except (ValueError, FileNotFoundError):
        return {"items": [], "crumbs": crumbs, "parent": parent, "rel": rel}
    if here.is_file():
        return {"items": [], "crumbs": crumbs, "parent": parent, "rel": rel}

    items: list[dict] = []
    for child in sorted(here.iterdir()):
        entry = _entry(child, rel, with_pages)
        if entry:
            items.append(entry)
        if len(items) >= max_items:
            break
    return {"items": items, "crumbs": crumbs, "parent": parent, "rel": rel}


def _crumbs(rel: str) -> tuple[list[dict], str | None]:
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p]
    crumbs = [{"name": p, "rel": "/".join(parts[:i + 1])} for i, p in enumerate(parts)]
    parent = "/".join(parts[:-1]) if parts else None
    return crumbs, parent


def _drives() -> list[dict]:
    """列出可用盘符；非 Windows 上就是根目录。"""
    import string

    if os.name != "nt":
        return [{"name": "/", "path": "/"}]
    return [{"name": f"{c}:", "path": f"{c}:\\"}
            for c in string.ascii_uppercase if Path(f"{c}:\\").exists()]


def list_dirs(path: str = "") -> dict:
    """服务端目录浏览：给管理员在**服务器**文件系统上逐层点选档案来源。

    浏览器出于安全不会把本机绝对路径交给网页（`webkitdirectory` 只给相对路径），
    而挂载点要的恰恰是服务端的绝对路径——所以目录只能在服务端列、由管理员点选。
    仅列目录、不列文件、不读内容，能力边界与「管理员本来就能手填任意路径」一致。
    """
    if not (path or "").strip():
        return {"path": "", "parent": None, "crumbs": [], "items": _drives(), "error": ""}

    here = Path(path).expanduser()
    try:
        here = here.resolve()
        children = sorted(p for p in here.iterdir() if p.is_dir())
    except (OSError, ValueError) as exc:      # 路径不存在、无权限、网络盘掉线
        return {"path": str(here), "parent": str(here.parent), "crumbs": [],
                "items": [], "error": f"无法读取该目录：{exc.__class__.__name__}"}

    items = [{"name": c.name, "path": str(c)}
             for c in children if not c.name.startswith(".")]
    parts = list(here.parts)
    crumbs = [{"name": p.rstrip("\\/") or p, "path": str(Path(*parts[:i + 1]))}
              for i, p in enumerate(parts)]
    parent = str(here.parent) if here.parent != here else ""
    return {"path": str(here), "parent": parent, "crumbs": crumbs,
            "items": items, "error": ""}


def resolve_custom(path_str: str, *, allowed: bool) -> Path:
    """逃生通道：不经挂载点、直接审核机器上的某个绝对路径。

    默认关闭（`serve --allow-custom-path` 才启用），且仅管理员可用——一旦开启，
    「审了机器上的哪个目录」这件事只剩审计日志能追溯，所以调用方必须记日志。
    """
    if not allowed:
        raise ValueError("未启用自由路径：请改用档案来源，或以 --allow-custom-path 启动服务")
    target = Path((path_str or "").strip()).expanduser()
    if not str(target):
        raise ValueError("路径不能为空")
    target = target.resolve()
    if not target.exists():
        raise FileNotFoundError(f"路径不存在：{target}")
    return target


def resolve_target(source: list[Mount] | str | Path, rel: str) -> Path:
    """把界面传来的相对路径解析为绝对路径，并确保仍在挂载点内。"""
    mounts, single = _as_mounts(source)
    if single:
        return _within(mounts[0].path, rel)

    head, sub = _split_rel(rel)
    found = next((m for m in mounts if m.name == head), None)
    if found is None:
        raise ValueError(f"未知的档案来源：{head or rel}")
    return _within(found.path, sub)
