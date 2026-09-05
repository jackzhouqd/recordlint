"""倾斜校正测试。

本批档案实测倾角中位数 0.20°、90 分位 0.75°，因此校正默认关闭；
但客户实际扫描件可能歪斜明显，功能必须可用且不能反向添乱。
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit import deskew, ingest


def table_image(angle: float = 0.0, w: int = 900, h: int = 1200) -> np.ndarray:
    """造一张带表格线的页面，可指定倾角。"""
    img = np.full((h, w, 3), 255, dtype=np.uint8)
    for y in range(200, 1000, 80):
        cv2.line(img, (100, y), (800, y), (0, 0, 0), 2)
    for x in range(100, 801, 140):
        cv2.line(img, (x, 200), (x, 960), (0, 0, 0), 2)
    if angle:
        m = cv2.getRotationMatrix2D((w / 2, h / 2), -angle, 1.0)
        img = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC,
                             borderMode=cv2.BORDER_REPLICATE)
    return img


@pytest.mark.parametrize("angle", [-3.0, -1.5, 1.5, 3.0])
def test_estimate_skew_matches_applied_angle(angle):
    got = deskew.estimate_skew(table_image(angle))
    assert abs(got - angle) < 0.6, f"估计 {got:.2f}° 与实际 {angle}° 偏差过大"


def test_straight_page_reports_near_zero():
    assert abs(deskew.estimate_skew(table_image(0.0))) < 0.3


def test_correct_straightens_page():
    corrected, applied = deskew.correct(table_image(2.5))
    assert abs(applied - 2.5) < 0.6
    assert abs(deskew.estimate_skew(corrected)) < 0.6, "校正后应基本水平"


def test_small_angle_is_left_untouched():
    """角度过小不重采样——重采样的清晰度损失大于收益。"""
    img = table_image(0.1)
    out, applied = deskew.correct(img)
    assert applied == 0.0 and out is img


def test_absurd_angle_is_rejected():
    """超过阈值多半是判错了或整页方向不对，不做旋转。"""
    out, applied = deskew.correct(table_image(0.0), angle=40.0)
    assert applied == 0.0


def test_prepare_defaults_to_no_deskew():
    img = table_image(2.5)
    assert ingest.prepare(img, long_side=900) is not None
    plain = ingest.prepare(img, long_side=900, deskew_enabled=False)
    fixed = ingest.prepare(img, long_side=900, deskew_enabled=True)
    assert abs(deskew.estimate_skew(fixed)) < abs(deskew.estimate_skew(plain))


def test_blank_page_does_not_crash():
    blank = np.full((600, 600, 3), 255, dtype=np.uint8)
    assert deskew.estimate_skew(blank) == 0.0
