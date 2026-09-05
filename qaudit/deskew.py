"""倾斜校正：扫描件轻微歪斜会让表格线提取与单元格切分失准。

判据取自表格线本身——质量记录都是规整表格，横线的主方向就是页面的水平方向。
无表格线时退回文字行主方向。角度过小不做旋转：重采样本身会损失清晰度，
得不偿失。
"""
from __future__ import annotations

import cv2
import numpy as np

MIN_ANGLE = 0.4    # 小于此角度不校正，重采样的损失大于收益
MAX_ANGLE = 15.0   # 超过此角度多半是判错了（或整页方向不对），不做旋转


def estimate_skew(image: np.ndarray) -> float:
    """估计页面倾角（度）。正值表示需要逆时针旋转纠正。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15
    )
    h, w = binary.shape[:2]

    angle = _angle_from_lines(binary, w)
    if angle is None:
        angle = _angle_from_text(binary)
    return float(angle or 0.0)


def _angle_from_lines(binary: np.ndarray, width: int) -> float | None:
    """用表格横线定方向：长直线的角度中位数最稳。"""
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, width // 30), 1))
    horiz = cv2.dilate(cv2.erode(binary, kernel), kernel)
    lines = cv2.HoughLinesP(
        horiz, 1, np.pi / 720, threshold=120,
        minLineLength=max(80, width // 8), maxLineGap=12,
    )
    if lines is None or len(lines) < 3:
        return None
    # OpenCV 4 返回 (N,1,4)，OpenCV 5 返回 (N,4)，统一成 (N,4)
    segments = np.asarray(lines).reshape(-1, 4)
    angles = []
    for x1, y1, x2, y2 in segments:
        if x2 == x1:
            continue
        deg = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(deg) <= MAX_ANGLE:
            angles.append(deg)
    return float(np.median(angles)) if len(angles) >= 3 else None


def _angle_from_text(binary: np.ndarray) -> float | None:
    """无表格线时，用文字块的最小外接矩形定方向。"""
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 5))
    blocks = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    coords = cv2.findNonZero(blocks)
    if coords is None or len(coords) < 500:
        return None
    angle = cv2.minAreaRect(coords)[2]
    if angle < -45:
        angle += 90
    elif angle > 45:
        angle -= 90
    return angle if abs(angle) <= MAX_ANGLE else None


def correct(image: np.ndarray, angle: float | None = None) -> tuple[np.ndarray, float]:
    """按估计角度旋转校正，返回（图像, 实际校正角度）。

    角度不足阈值时原样返回——不做无谓的重采样。
    """
    angle = estimate_skew(image) if angle is None else angle
    if abs(angle) < MIN_ANGLE or abs(angle) > MAX_ANGLE:
        return image, 0.0
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rotated = cv2.warpAffine(
        image, matrix, (w, h), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,  # 补边用边缘像素，避免引入黑边被当成表格线
    )
    return rotated, angle
