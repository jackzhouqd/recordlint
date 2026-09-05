"""训练侧的后台作业：样本入库、退化合成、模型训练。

复用审核作业同样的形态（后台线程 + task 表 + 页面轮询），但用独立的锁，
并与审核互斥——两者都是 CPU 密集型，同时跑只会互相拖慢。

日志落成文件，页面按需 tail。训练要跑几分钟，把日志塞进 task.message
会让「刚才那句警告」被后面的输出冲掉。
"""
from __future__ import annotations

import random
import threading
import traceback
from datetime import datetime
from pathlib import Path

from .. import ingest, jobs as audit_jobs, seal
from ..store import Store
from . import imaging
from .features import LABEL_CN
from .repo import TrainRepo

_LOCK = threading.Lock()
_CURRENT: threading.Thread | None = None

LOG_TAIL = 400   # 页面最多展示的日志行数


def is_busy() -> bool:
    return _CURRENT is not None and _CURRENT.is_alive()


def dataset_root(out_dir: str | Path) -> Path:
    return Path(out_dir) / "dataset"


def log_path(root: Path, task_id: str) -> Path:
    return root / "logs" / f"{task_id}.log"


def read_log(root: Path, task_id: str, tail: int = LOG_TAIL) -> str:
    path = log_path(root, task_id)
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])


def _start(db_path, root: Path, kind: str, target: str, operator: str, total: int,
           worker, args: tuple) -> str:
    with _LOCK:
        global _CURRENT
        if is_busy():
            raise RuntimeError("已有训练任务在运行，请等待其完成后再发起")
        if audit_jobs.is_busy():
            raise RuntimeError("有审核任务正在运行。两者都吃满 CPU，请待其完成后再发起训练任务")

        task_id = f"M{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        store = Store(db_path)
        store.create_task(task_id, kind, target, operator, total=total)
        store.close()
        log_path(root, task_id).parent.mkdir(parents=True, exist_ok=True)

        _CURRENT = threading.Thread(target=_wrap, args=(worker, db_path, root, task_id) + args,
                                    daemon=True)
        _CURRENT.start()
        return task_id


def _wrap(worker, db_path, root: Path, task_id: str, *args) -> None:
    store = Store(db_path)   # 后台线程独立连接，避免与 Web 请求争用
    path = log_path(root, task_id)
    handle = path.open("a", encoding="utf-8")

    def log(line: str) -> None:
        handle.write(line + "\n")
        handle.flush()
        store.update_task(task_id, message=line[:180])

    try:
        summary = worker(store, root, task_id, log, *args)
        log(f"[完成] {summary}")
        store.finish_task(task_id, "done", summary, operator=args[0] if args else "")
    except Exception as exc:
        msg = f"{exc.__class__.__name__}: {exc}"
        log(f"[失败] {msg}")
        store.finish_task(task_id, "failed", msg[:200])
        traceback.print_exc()
    finally:
        handle.close()
        store.close()


# ---------------------------------------------------------------- 样本入库

def start_import(db_path, out_dir, target: str, operator: str, *, limit: int = 0,
                 pad: float = 0.15, include_frames: bool = False) -> str:
    root = dataset_root(out_dir)
    total = 0
    try:
        total = ingest.count_pages(target)
    except Exception:
        pass
    if limit:
        total = min(total, limit) if total else limit
    return _start(db_path, root, "sample_import", str(target), operator, total,
                  _run_import, (operator, str(target), limit, pad, include_frames))


def _run_import(store, root: Path, task_id: str, log, operator: str, target: str,
                limit: int, pad: float, include_frames: bool) -> str:
    repo = TrainRepo(store)
    log(f"[开始] 从 {target} 裁出印章样本")
    rows, pages, skipped_frames = [], 0, 0
    for page in ingest.iter_pages(target, 2200):
        if limit and pages >= limit:
            break
        pages += 1
        for k, s in enumerate(seal.detect(page.image)):
            if s.is_frame and not include_frames:
                skipped_frames += 1
                continue   # 红色线框（复印确认章）不是印章
            crop = imaging.crop_seal(page.image, s.bbox, pad)
            if crop is None:
                continue
            sid = f"{imaging.safe_name(page.doc_id)}_p{page.page_no:04d}_{k:02d}"
            rel = f"seals/{sid}.jpg"
            imaging.imwrite_jpg(root / rel, crop)
            rows.append({"sample_id": sid, "file": rel, "doc_id": page.doc_id,
                         "page_no": page.page_no, "bbox": [int(v) for v in s.bbox],
                         "source": sid, "synthetic": False,
                         "angle": round(s.angle, 2), "fill_ratio": round(s.fill_ratio, 4),
                         "rectangularity": round(s.rectangularity, 3)})
        if pages % 20 == 0:
            store.update_task(task_id, done=pages)
            log(f"  已处理 {pages} 页，累计裁出 {len(rows)} 枚")

    # 自检：清单与磁盘必须一一对应。曾因 cv2.imwrite 遇中文路径静默失败而丢过 72% 的样本。
    missing = [r["file"] for r in rows if not (root / r["file"]).exists()]
    if missing:
        raise RuntimeError(f"{len(missing)} 个样本未写入磁盘，例如 {missing[:2]}")

    n = repo.add_samples(rows)
    store.update_task(task_id, done=pages, total=pages)
    store._log("sample_import", target, f"{pages} 页 → {n} 枚", operator)
    store.conn.commit()
    return f"{pages} 页 → 入库 {n} 枚印章（跳过红色线框 {skipped_frames} 处）"


# ---------------------------------------------------------------- 退化合成

def start_synth(db_path, out_dir, operator: str, *, ratio: float = 0.6, seed: int = 42) -> str:
    root = dataset_root(out_dir)
    return _start(db_path, root, "sample_synth", "退化样本合成", operator, 0,
                  _run_synth, (operator, ratio, seed))


def _run_synth(store, root: Path, task_id: str, log, operator: str,
               ratio: float, seed: int) -> str:
    repo = TrainRepo(store)
    base = repo.ok_sources()
    human = repo.has_human_labels()
    if not base:
        raise RuntimeError("没有可用的合成基底：请先入库样本，并标注若干「合格」样本")
    log(f"[基底] {len(base)} 枚"
        + ("（人工确认为合格）" if human else "（尚无人工标注，按几何特征粗筛）"))

    rng = random.Random(seed)
    rows, labels = [], []
    for s in base:
        img = ingest.imread_unicode(root / s["file"])
        if img is None:
            continue
        # 基底本身即「合格」类的正样本。粗筛来的基底标注人记为「几何粗筛」，
        # 不计入人工标注量，否则上线门槛形同虚设。
        labels.append((s["sample_id"], "ok", "几何粗筛" if not human else ""))
        for kind, fn in imaging.SYNTH_MAKERS.items():
            if rng.random() > ratio:
                continue
            sid = f"{s['sample_id']}__{kind}"
            rel = f"synth/{sid}.jpg"
            imaging.imwrite_jpg(root / rel, fn(img, rng))
            rows.append({"sample_id": sid, "file": rel, "doc_id": s["doc_id"],
                         "page_no": s["page_no"], "source": s["sample_id"],
                         "synthetic": True, "synth_kind": kind})
            labels.append((sid, kind, "合成"))
    repo.add_samples(rows)
    for sid, label, who in labels:
        if who or not _is_labeled(repo, sid):
            repo.set_label(sid, label, who or "合成")
    store.conn.commit()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["synth_kind"]] = counts.get(r["synth_kind"], 0) + 1
    detail = "，".join(f"{LABEL_CN.get(k, k)} {v}" for k, v in counts.items())
    return f"合成 {len(rows)} 枚退化样本（{detail}），基底 {len(base)} 枚"


def _is_labeled(repo: TrainRepo, sample_id: str) -> bool:
    return repo.conn.execute(
        "SELECT 1 FROM seal_label WHERE sample_id=?", (sample_id,)).fetchone() is not None


# ---------------------------------------------------------------- 训练

def start_train(db_path, out_dir, operator: str, *, folds: int = 5, seed: int = 42) -> str:
    root = dataset_root(out_dir)
    return _start(db_path, root, "train", "印章状态分类器训练", operator, 0,
                  _run_train, (operator, folds, seed))


def _run_train(store, root: Path, task_id: str, log, operator: str,
               folds: int, seed: int) -> str:
    from . import trainer

    repo = TrainRepo(store)
    pairs = repo.labeled_pairs()
    if not pairs:
        raise RuntimeError("没有已标注样本：请先在「标注」页打标签，或先合成退化样本")
    version = repo.next_version("seal_cls")
    log(f"[版本] 本次训练产出 seal_cls@{version}")
    result = trainer.train(root, pairs, version=version, folds=folds, seed=seed, log=log)
    model_id = repo.add_model(kind="seal_cls", version=version, path=result["path"],
                              trainer=operator, samples=result["samples"],
                              human=result["human"], groups=result["groups"],
                              accuracy=result["accuracy"], metrics=result["metrics"])
    store.conn.commit()
    return (f"{model_id} 训练完成：准确率 {result['accuracy']:.1%}，"
            f"样本 {result['samples']} 条 / 源印章 {result['groups']} 组")
