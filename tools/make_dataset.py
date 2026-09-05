"""印章样本数据集构建工具。

用途：把已自动检出的印章从档案里批量裁出来，生成可离线标注的网页，
      再把标注结果合并成训练集。全过程离线，数据不离开本机。

  python tools/make_dataset.py crop  samples/synthetic --out out/dataset
  python tools/make_dataset.py synth --out out/dataset          # 合成退化样本
  python tools/make_dataset.py pages --out out/dataset          # 生成标注网页
  python tools/make_dataset.py merge --out out/dataset          # 合并标注→训练集

标注单位是“枚”不是“页”：印章已由 seal.detect() 定位好，标注只需打标签，
不需要画框，成本比常规目标检测标注低一个数量级。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit import ingest, seal  # noqa: E402
from qaudit.train.imaging import (  # noqa: E402
    LABELS, SYNTH_MAKERS, crop_seal as _crop, imwrite_jpg, safe_name as _safe,
    syn_blurred as _syn_blurred, syn_chipped as _syn_chipped,
    syn_faint as _syn_faint, syn_upside_down as _syn_upside_down,
)

TEMPLATE = Path(__file__).resolve().parent / "annotate.html"

# 裁图、写盘、退化合成的实现已提到 qaudit/train/imaging.py——界面上的训练工作台
# 也要用同一套逻辑，两份实现迟早走偏。本文件保留为命令行入口。


@dataclass(frozen=True)
class SealSample:
    id: str
    file: str
    doc_id: str
    page_no: int
    bbox: tuple[int, int, int, int]
    angle: float
    fill_ratio: float
    rectangularity: float


# ---------------------------------------------------------------- crop

def cmd_crop(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    img_dir = out_dir / "seals"
    img_dir.mkdir(parents=True, exist_ok=True)

    samples: list[SealSample] = []
    pages = 0
    for page in ingest.iter_pages(args.target, args.long_side):
        if args.limit and pages >= args.limit:
            break
        pages += 1
        detected = seal.detect(page.image)
        for k, s in enumerate(detected):
            if s.is_frame and not args.include_frames:
                continue  # 红色线框（复印确认章）不是印章
            crop = _crop(page.image, s.bbox, args.pad)
            if crop is None:
                continue
            sid = f"{_safe(page.doc_id)}_p{page.page_no:04d}_{k:02d}"
            rel = f"seals/{sid}.jpg"
            imwrite_jpg(out_dir / rel, crop)
            samples.append(
                SealSample(sid, rel, page.doc_id, page.page_no, tuple(int(v) for v in s.bbox),
                           round(s.angle, 2), round(s.fill_ratio, 4), round(s.rectangularity, 3))
            )
        if pages % 100 == 0:
            print(f"  已处理 {pages} 页，累计裁出 {len(samples)} 枚")

    # 自检：清单与磁盘必须一一对应。曾因 cv2.imwrite 遇中文路径静默失败而丢过 72% 的样本。
    missing = [s.file for s in samples if not (out_dir / s.file).exists()]
    if missing:
        print(f"[错误] {len(missing)} 个样本未写入磁盘，例如 {missing[:2]}", file=sys.stderr)
        return 3

    manifest = out_dir / "manifest.json"
    manifest.write_text(
        json.dumps([asdict(s) for s in samples], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"[完成] {pages} 页 → {len(samples)} 枚印章（已校验全部落盘）")
    print(f"[清单] {manifest}")
    return 0


# ---------------------------------------------------------------- synth

def cmd_synth(args: argparse.Namespace) -> int:
    """从真实清晰印章合成退化样本。

    倒盖/缺角/模糊/漏墨都能可靠合成，因此训练集不需要人工标注，
    人工只标验证集即可——这是把标注成本从“千页”降到“百枚”的关键。
    """
    out_dir = Path(args.out)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    labels_file = out_dir / "labels.json"
    ok_ids: set[str] = set()
    if labels_file.exists():
        labeled = json.loads(labels_file.read_text(encoding="utf-8"))
        ok_ids = {k for k, v in labeled.items() if v == "ok"}
        print(f"[基底] 使用人工确认为“合格”的 {len(ok_ids)} 枚作为合成基底")
    if not ok_ids:
        # 尚未标注时，用几何特征粗筛出形状规整的作为基底
        ok_ids = {s["id"] for s in manifest if s["rectangularity"] >= 0.75 and s["fill_ratio"] >= 0.2}
        print(f"[基底] 未见人工标注，按几何特征粗筛 {len(ok_ids)} 枚作为合成基底")

    rng = random.Random(args.seed)
    syn_dir = out_dir / "synth"
    syn_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []

    makers = SYNTH_MAKERS
    base = [s for s in manifest if s["id"] in ok_ids]
    for s in base:
        img = ingest.imread_unicode(out_dir / s["file"])
        if img is None:
            continue
        records.append({"file": s["file"], "label": "ok", "source": s["id"], "synthetic": False})
        for name, fn in makers.items():
            if rng.random() > args.ratio:
                continue
            variant = fn(img, rng)
            rel = f"synth/{s['id']}__{name}.jpg"
            imwrite_jpg(out_dir / rel, variant)
            records.append({"file": rel, "label": name, "source": s["id"], "synthetic": True})

    path = out_dir / "synth.json"
    path.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    counts: dict[str, int] = {}
    for r in records:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    print(f"[完成] 合成样本 {len(records)} 条：{counts}")
    print(f"[清单] {path}")
    return 0


# ---------------------------------------------------------------- pages

def cmd_pages(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    template = TEMPLATE.read_text(encoding="utf-8")

    per = args.per_page
    total_pages = (len(manifest) + per - 1) // per
    written = []
    for i in range(total_pages):
        chunk = manifest[i * per : (i + 1) * per]
        items = []
        for s in chunk:
            data = (out_dir / s["file"]).read_bytes()
            items.append(
                {
                    "id": s["id"],
                    "img": base64.b64encode(data).decode(),
                    "doc": s["doc_id"],
                    "page": s["page_no"],
                }
            )
        html = (
            template.replace("__DATA__", json.dumps(items, ensure_ascii=False))
            .replace("__PAGE_NO__", str(i + 1))
            .replace("__PAGE_TOTAL__", str(total_pages))
            .replace("__LABELS__", json.dumps(LABELS, ensure_ascii=False))
        )
        path = out_dir / f"annotate_{i + 1:03d}.html"
        path.write_text(html, encoding="utf-8")
        written.append(path)
        print(f"  {path.name}  {len(chunk)} 枚  {path.stat().st_size // 1024} KB")
    print(f"[完成] {total_pages} 个标注页，共 {len(manifest)} 枚印章")
    return 0


# ---------------------------------------------------------------- merge

def cmd_merge(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    merged: dict[str, str] = {}
    files = sorted(out_dir.glob("labels_*.json")) + (
        [Path(p) for p in args.extra] if args.extra else []
    )
    if not files:
        print("[错误] 未找到标注导出文件（out/dataset/labels_*.json）", file=sys.stderr)
        return 2
    for f in files:
        payload = json.loads(Path(f).read_text(encoding="utf-8"))
        merged.update(payload.get("labels", payload))
        print(f"  合并 {Path(f).name}：{len(payload.get('labels', payload))} 条")

    (out_dir / "labels.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    manifest = {s["id"]: s for s in json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))}
    rng = random.Random(args.seed)
    by_label: dict[str, list[str]] = {}
    for sid, label in merged.items():
        by_label.setdefault(label, []).append(sid)

    train, val = [], []
    for label, ids in by_label.items():
        ids = sorted(ids)
        rng.shuffle(ids)
        cut = max(1, int(len(ids) * (1 - args.val_ratio)))
        for sid in ids[:cut]:
            if sid in manifest:
                train.append({"file": manifest[sid]["file"], "label": label, "id": sid})
        for sid in ids[cut:]:
            if sid in manifest:
                val.append({"file": manifest[sid]["file"], "label": label, "id": sid})

    dataset = {
        "labels": {v[0]: v[1] for v in LABELS.values()},
        "counts": {k: len(v) for k, v in sorted(by_label.items())},
        "train": train,
        "val": val,
        "note": "val 为人工标注的真实样本，训练可另行加入 synth.json 的合成样本；"
                "验收评测只允许使用 val。",
    }
    path = out_dir / "dataset.json"
    path.write_text(json.dumps(dataset, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[完成] 标注 {len(merged)} 条 → 训练 {len(train)} / 验证 {len(val)}")
    print(f"[分布] {dataset['counts']}")
    print(f"[数据集] {path}")
    return 0


# ---------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="make_dataset", description="印章样本数据集构建")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crop", help="从档案批量裁出印章小图")
    c.add_argument("target", help="档案目录或文件")
    c.add_argument("--out", default="out/dataset")
    c.add_argument("--pad", type=float, default=0.15, help="裁图外扩比例，留边才看得出缺角")
    c.add_argument("--limit", type=int, default=None, help="最多处理页数")
    c.add_argument("--long-side", type=int, default=2200)
    c.add_argument("--include-frames", action="store_true", help="连红色线框一起裁出")
    c.set_defaults(func=cmd_crop)

    s = sub.add_parser("synth", help="合成退化样本（倒盖/缺角/模糊/漏墨）")
    s.add_argument("--out", default="out/dataset")
    s.add_argument("--ratio", type=float, default=0.6, help="每类退化的生成概率")
    s.add_argument("--seed", type=int, default=42)
    s.set_defaults(func=cmd_synth)

    p = sub.add_parser("pages", help="生成离线标注网页")
    p.add_argument("--out", default="out/dataset")
    p.add_argument("--per-page", type=int, default=300, help="每页嵌入多少枚印章")
    p.set_defaults(func=cmd_pages)

    m = sub.add_parser("merge", help="合并标注导出，切分训练/验证集")
    m.add_argument("--out", default="out/dataset")
    m.add_argument("--extra", nargs="*", help="额外的标注文件路径")
    m.add_argument("--val-ratio", type=float, default=0.3)
    m.add_argument("--seed", type=int, default=42)
    m.set_defaults(func=cmd_merge)
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
