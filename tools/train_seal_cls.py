"""印章状态分类器训练（基线版，sklearn）。

用途：为二期「印章清晰/完整/倒盖」类规则提供可行性验证与基线模型。

  python tools/train_seal_cls.py train   --out out/dataset
  python tools/train_seal_cls.py predict --out out/dataset --file seals/xxx.jpg

设计约束（与主系统一致）：
- 训练可以依赖 sklearn，**部署不引入任何新依赖**：线性模型导出为 npz，
  推理用 numpy 十几行即可完成，和现有 onnxruntime/OpenCV 部署形态不冲突。
- 模型只回答“这枚章是什么状态”，是否判为不合格仍由 rules.yaml 决定。

评估方法上有一个必须守住的点：合成样本派生自同一枚真实印章，
若随机切分会让同源样本同时进训练集和验证集，准确率会被严重高估。
本脚本一律按 source（源印章）分组切分。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit import ingest  # noqa: E402
from qaudit.train.features import (  # noqa: E402
    GRAY_SIDE, LABEL_CN, LABEL_ORDER, MASK_SIDE, featurize, load_linear, predict_one,
)

# 特征与推理已提到 qaudit/train/features.py——规则引擎在模型上线后要调用推理，
# 不能留在 tools/ 下。本文件保留为命令行训练入口。


@dataclass(frozen=True)
class Dataset:
    X: np.ndarray
    y: np.ndarray
    groups: np.ndarray
    files: list[str]
    synthetic: np.ndarray
    human: np.ndarray  # 是否经人工标注确认。几何粗筛出的“合成基底”不算人工标注


# ---------------------------------------------------------------- 特征

def _read(out_dir: Path, rel: str) -> np.ndarray | None:
    return ingest.imread_unicode(out_dir / rel)


# ---------------------------------------------------------------- 数据

def load_dataset(out_dir: Path, *, include_synth: bool, include_labeled: bool) -> Dataset:
    rows: list[tuple[str, str, str, bool, bool]] = []  # (file, label, group, synthetic, human)

    labels_file = out_dir / "labels.json"
    if include_labeled and labels_file.exists():
        manifest = {s["id"]: s for s in json.loads((out_dir / "manifest.json").read_text("utf-8"))}
        for sid, label in json.loads(labels_file.read_text("utf-8")).items():
            if sid in manifest and label in LABEL_ORDER:
                rows.append((manifest[sid]["file"], label, sid, False, True))

    synth_file = out_dir / "synth.json"
    if include_synth and synth_file.exists():
        for r in json.loads(synth_file.read_text("utf-8")):
            rows.append((r["file"], r["label"], r["source"], bool(r["synthetic"]), False))

    if not rows:
        raise SystemExit("[错误] 没有可用样本：先跑 make_dataset.py synth 或完成人工标注后 merge")

    seen: set[tuple[str, str]] = set()
    X, y, g, files, syn, hum = [], [], [], [], [], []
    for rel, label, group, is_syn, is_human in rows:
        if (rel, label) in seen:   # 人工标注优先，避免与合成基底重复计入
            continue
        seen.add((rel, label))
        img = _read(out_dir, rel)
        if img is None:
            continue
        X.append(featurize(img))
        y.append(label)
        g.append(group)
        files.append(rel)
        syn.append(is_syn)
        hum.append(is_human)
    return Dataset(np.vstack(X), np.array(y), np.array(g), files, np.array(syn), np.array(hum))


# ---------------------------------------------------------------- 训练

def cmd_train(args: argparse.Namespace) -> int:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import classification_report, confusion_matrix
    from sklearn.model_selection import StratifiedGroupKFold
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    out_dir = Path(args.out)
    ds = load_dataset(out_dir, include_synth=not args.no_synth, include_labeled=True)
    human = int(ds.human.sum())
    synth = int(ds.synthetic.sum())
    seedbase = len(ds.y) - human - synth
    print(f"[数据] {len(ds.y)} 条 = 人工标注 {human} + 几何粗筛基底 {seedbase} + 合成退化 {synth}，"
          f"特征 {ds.X.shape[1]} 维，源印章 {len(set(ds.groups))} 枚")
    if human == 0:
        print("[警告] 尚无人工标注样本，本次结果只能作为“流程跑通 + 能力上界”参考，不可用于验收")
    counts = {lab: int((ds.y == lab).sum()) for lab in LABEL_ORDER if (ds.y == lab).any()}
    print(f"[类别] {counts}")

    candidates = {
        "logreg": make_pipeline(
            StandardScaler(with_mean=True),
            LogisticRegression(max_iter=2000, C=1.0),  # sklearn 默认即多分类 softmax
        ),
        "rf": RandomForestClassifier(
            n_estimators=300, min_samples_leaf=2, n_jobs=-1, random_state=args.seed
        ),
    }

    # 按源印章分组切分：同一枚真实章派生的各种退化样本不得跨越训练/验证边界
    cv = StratifiedGroupKFold(n_splits=args.folds, shuffle=True, random_state=args.seed)
    results: dict[str, dict] = {}
    for name, model in candidates.items():
        accs, y_true_all, y_pred_all = [], [], []
        for tr, va in cv.split(ds.X, ds.y, groups=ds.groups):
            model.fit(ds.X[tr], ds.y[tr])
            pred = model.predict(ds.X[va])
            accs.append(float((pred == ds.y[va]).mean()))
            y_true_all.extend(ds.y[va])
            y_pred_all.extend(pred)
        results[name] = {
            "acc_mean": float(np.mean(accs)),
            "acc_std": float(np.std(accs)),
            "report": classification_report(y_true_all, y_pred_all, zero_division=0, output_dict=True),
        }
        print(f"\n[{name}] 分组交叉验证准确率 {np.mean(accs):.3f} ± {np.std(accs):.3f}")
        print(classification_report(y_true_all, y_pred_all, zero_division=0, digits=3))
        labs = [l for l in LABEL_ORDER if l in set(y_true_all)]
        cm = confusion_matrix(y_true_all, y_pred_all, labels=labs)
        print("混淆矩阵（行=真实，列=预测）: " + "  ".join(LABEL_CN[l] for l in labs))
        for l, row in zip(labs, cm):
            print(f"  {LABEL_CN[l]:<4}" + "".join(f"{v:>7d}" for v in row))

    best = max(results, key=lambda k: results[k]["acc_mean"])
    print(f"\n[选型] 基线最优：{best}（{results[best]['acc_mean']:.3f}）")

    # 导出线性模型：部署侧只需 numpy，不引入 sklearn/torch
    linear = candidates["logreg"]
    linear.fit(ds.X, ds.y)
    scaler = linear.named_steps["standardscaler"]
    clf = linear.named_steps["logisticregression"]
    model_path = out_dir / "seal_cls_linear.npz"
    np.savez(
        model_path,
        mean=scaler.mean_.astype(np.float32),
        scale=scaler.scale_.astype(np.float32),
        coef=clf.coef_.astype(np.float32),
        intercept=clf.intercept_.astype(np.float32),
        classes=np.array(clf.classes_),
    )
    meta = {
        "feature": {"mask_side": MASK_SIDE, "gray_side": GRAY_SIDE, "dim": int(ds.X.shape[1])},
        "cv": {k: {"acc_mean": v["acc_mean"], "acc_std": v["acc_std"]} for k, v in results.items()},
        "samples": {"total": len(ds.y), "human_labeled": human, "seed_base": seedbase,
                    "synthetic": synth, "by_label": counts},
        "split": "StratifiedGroupKFold by source seal（同源样本不跨训练/验证边界）",
        "caveat": "若样本以合成为主，此准确率是上界；真实缺陷更难，"
                  "验收必须在人工标注的真实验证集上另行测量。",
    }
    (out_dir / "seal_cls_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"[导出] {model_path}（numpy 可直接推理）\n[元信息] {out_dir / 'seal_cls_meta.json'}")
    return 0


# ---------------------------------------------------------------- 推理

def cmd_predict(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    model = load_linear(out_dir / "seal_cls_linear.npz")
    targets = args.file or [r["file"] for r in
                            json.loads((out_dir / "synth.json").read_text("utf-8"))[:10]]
    for rel in targets:
        img = _read(out_dir, rel)
        if img is None:
            print(f"  {rel}: 读取失败")
            continue
        label, conf = predict_one(model, img)
        print(f"  {rel:<60} → {LABEL_CN.get(label, label)}  {conf:.1%}")
    return 0


# ---------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="train_seal_cls", description="印章状态分类基线训练")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("train", help="训练并导出基线模型")
    t.add_argument("--out", default="out/dataset")
    t.add_argument("--folds", type=int, default=5)
    t.add_argument("--seed", type=int, default=42)
    t.add_argument("--no-synth", action="store_true", help="只用人工标注的真实样本")
    t.set_defaults(func=cmd_train)

    p = sub.add_parser("predict", help="对样本推理（验证导出模型可用）")
    p.add_argument("--out", default="out/dataset")
    p.add_argument("--file", nargs="*", help="相对 out 目录的图片路径")
    p.set_defaults(func=cmd_predict)
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
