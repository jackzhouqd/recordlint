"""数据集构建工具测试。

锁住一个真实踩过的坑：档案名含中文时，cv2.imwrite 在 Windows 下会静默失败，
曾导致 4396 枚样本里 3187 枚没写进磁盘而清单照常生成。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from make_dataset import _crop, _safe, imwrite_jpg  # noqa: E402


@pytest.mark.parametrize(
    "name",
    ["齿轮箱组件（PN-3801G)22-02-10-7", "泵体（PN-3951A)10(03)", "batch-0001"],
)
def test_safe_name_is_ascii(name):
    out = _safe(name)
    assert out.isascii(), f"文件名必须为纯 ASCII，实际 {out}"
    assert all(c.isalnum() or c in "-_." for c in out)


def test_safe_name_is_unique_for_similar_docs():
    """长档案名截断后仍必须唯一，否则样本互相覆盖。"""
    a = _safe("泵体组件（PN-2801D)22-02-10-6")
    b = _safe("泵体组件（PN-2801D)22-02-10-8")
    assert a != b


def test_imwrite_handles_unicode_path(tmp_path: Path):
    img = np.full((40, 40, 3), 200, dtype=np.uint8)
    target = tmp_path / "中文目录" / "样本.jpg"
    target.parent.mkdir(parents=True)
    imwrite_jpg(target, img)
    assert target.exists() and target.stat().st_size > 0


def test_crop_pads_and_caps_size():
    img = np.full((600, 600, 3), 255, dtype=np.uint8)
    crop = _crop(img, (100, 100, 400, 400), pad_ratio=0.15)
    assert crop is not None
    assert max(crop.shape[:2]) <= 256, "裁图长边应统一上限，便于标注与训练"


def test_crop_rejects_tiny_region():
    img = np.full((60, 60, 3), 255, dtype=np.uint8)
    assert _crop(img, (0, 0, 3, 3), pad_ratio=0.0) is None
