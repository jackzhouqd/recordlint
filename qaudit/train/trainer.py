"""印章状态分类器训练。

评估方法上有一个必须守住的点：合成样本派生自同一枚真实印章，若随机切分
会让同源样本同时进训练集和验证集，准确率会被严重高估。本模块一律按 source
（源印章）分组切分，并把实际组数写进模型元信息，界面上显式展示。

训练依赖 sklearn；**导出的是线性模型的 npz 权重**，推理只用 numpy，
部署侧不引入任何新依赖。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .. import ingest
from .features import LABEL_CN, LABEL_ORDER, featurize

Log = Callable[[str], None]


@dataclass(frozen=True)
class Dataset:
    X: np.ndarray
    y: np.ndarray
    groups: np.ndarray
    human: np.ndarray


def load_dataset(root: Path, pairs: list[tuple[str, str, str, int]], log: Log) -> Dataset:
    """pairs 为 (file, label, source, synthetic)，来自 TrainRepo.labeled_pairs()。"""
    X, y, g, hum = [], [], [], []
    missing = 0
    for rel, label, source, synthetic in pairs:
        if label not in LABEL_ORDER:
            continue
        img = ingest.imread_unicode(root / rel)
        if img is None:
            missing += 1
            continue
        X.append(featurize(img))
        y.append(label)
        g.append(source)
        hum.append(not synthetic)
    if missing:
        log(f"[警告] {missing} 个样本文件读取失败，已跳过")
    if not X:
        raise RuntimeError("没有可用样本：请先入库样本并完成标注（或先合成退化样本）")
    return Dataset(np.vstack(X), np.array(y), np.array(g), np.array(hum))


def train(root: Path, pairs: list[tuple[str, str, str, int]], *, version: str,
          folds: int = 5, seed: int = 42, log: Log = print) -> dict:
    """训练并导出。返回写入 model 表所需的元信息。"""
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import classification_report, confusion_matrix
    from sklearn.model_selection import StratifiedGroupKFold
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    ds = load_dataset(root, pairs, log)
    human = int(ds.human.sum())
    groups = len(set(ds.groups.tolist()))
    counts = {lab: int((ds.y == lab).sum()) for lab in LABEL_ORDER if (ds.y == lab).any()}
    log(f"[数据] {len(ds.y)} 条（人工标注 {human}，合成 {len(ds.y) - human}），"
        f"特征 {ds.X.shape[1]} 维，源印章 {groups} 组")
    log(f"[类别] " + "，".join(f"{LABEL_CN.get(k, k)} {v}" for k, v in counts.items()))
    if human == 0:
        log("[警告] 尚无人工标注样本，本次结果只能作为「流程跑通 + 能力上界」参考，不可用于验收")

    n_splits = _usable_folds(ds, folds, log)
    candidates = {
        "logreg": make_pipeline(StandardScaler(with_mean=True),
                                LogisticRegression(max_iter=2000, C=1.0)),
        "rf": RandomForestClassifier(n_estimators=300, min_samples_leaf=2, n_jobs=-1,
                                     random_state=seed),
    }

    cv = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    results: dict[str, dict] = {}
    labels_seen: list[str] = []
    matrix: list[list[int]] = []
    for name, model in candidates.items():
        accs, y_true, y_pred = [], [], []
        for tr, va in cv.split(ds.X, ds.y, groups=ds.groups):
            model.fit(ds.X[tr], ds.y[tr])
            pred = model.predict(ds.X[va])
            accs.append(float((pred == ds.y[va]).mean()))
            y_true.extend(ds.y[va].tolist())
            y_pred.extend(pred.tolist())
        report = classification_report(y_true, y_pred, zero_division=0, output_dict=True)
        results[name] = {"acc_mean": float(np.mean(accs)), "acc_std": float(np.std(accs)),
                         "report": report}
        log(f"[{name}] 分组交叉验证准确率 {np.mean(accs):.3f} ± {np.std(accs):.3f}")
        if name == "logreg":
            labels_seen = [lab for lab in LABEL_ORDER if lab in set(y_true)]
            matrix = confusion_matrix(y_true, y_pred, labels=labels_seen).tolist()

    best = max(results, key=lambda k: results[k]["acc_mean"])
    log(f"[选型] 基线最优：{best}（{results[best]['acc_mean']:.3f}）")

    # 无论谁最优，导出的都是线性模型——部署侧只需 numpy
    linear = candidates["logreg"]
    linear.fit(ds.X, ds.y)
    scaler = linear.named_steps["standardscaler"]
    clf = linear.named_steps["logisticregression"]
    root.mkdir(parents=True, exist_ok=True)
    model_path = root / f"seal_cls_{version}.npz"
    np.savez(model_path,
             mean=scaler.mean_.astype(np.float32), scale=scaler.scale_.astype(np.float32),
             coef=clf.coef_.astype(np.float32), intercept=clf.intercept_.astype(np.float32),
             classes=np.array(clf.classes_))
    log(f"[导出] {model_path.name}（{model_path.stat().st_size // 1024} KB，numpy 可直接推理）")

    metrics = {
        "cv": {k: {"acc_mean": v["acc_mean"], "acc_std": v["acc_std"]} for k, v in results.items()},
        "best": best,
        "report": results["logreg"]["report"],
        "confusion": {"labels": labels_seen, "matrix": matrix},
        "counts": counts,
        "folds": n_splits,
        "split": "StratifiedGroupKFold by source seal（同源样本不跨训练/验证边界）",
        "caveat": "若样本以合成为主，此准确率是上界；真实缺陷更难，"
                  "验收必须在人工标注的真实验证集上另行测量。",
    }
    (root / f"seal_cls_{version}_meta.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")

    return {"path": str(model_path), "samples": int(len(ds.y)), "human": human,
            "groups": groups, "accuracy": results[best]["acc_mean"], "metrics": metrics}


def _usable_folds(ds: Dataset, wanted: int, log: Log) -> int:
    """样本少时 StratifiedGroupKFold 会直接报错，这里退到可用折数并说明原因。"""
    per_label = min(int((ds.y == lab).sum()) for lab in set(ds.y.tolist()))
    n_groups = len(set(ds.groups.tolist()))
    usable = max(2, min(wanted, per_label, n_groups))
    if usable != wanted:
        log(f"[折数] 最小类别样本 {per_label} 条、源印章 {n_groups} 组，"
            f"交叉验证折数由 {wanted} 降为 {usable}")
    return usable
