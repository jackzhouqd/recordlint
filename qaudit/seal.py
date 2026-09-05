"""红色印章检测（HSV 色域 + 形态学，无需训练数据）。

判定所需属性：位置、倾斜角、面积、与其他印章/文本的重叠。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class Seal:
    """一枚红色印记。angle 为最小外接矩形倾角，归一到 [-45, 45]。"""

    bbox: tuple[int, int, int, int]
    angle: float
    area: int
    fill_ratio: float  # 红色像素占外接框比例，用于区分实心章与红线框（如“与原件一致”框）
    rectangularity: float = 0.0  # 轮廓面积/最小外接矩形面积，接近 1 才说明倾角可信

    @property
    def is_frame(self) -> bool:
        """红色矩形线框（复印确认章）填充率低。"""
        return self.fill_ratio < 0.18

    @property
    def angle_reliable(self) -> bool:
        """圆形印章的最小外接矩形角度无意义，只对方/多边形章判定歪斜。"""
        return self.rectangularity >= 0.80

    def center(self) -> tuple[float, float]:
        x, y, w, h = self.bbox
        return x + w / 2, y + h / 2


def red_mask(image: np.ndarray) -> np.ndarray:
    """提取红色像素掩膜。红色在 HSV 中跨 0°，需取两段。"""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower = cv2.inRange(hsv, np.array([0, 60, 60]), np.array([12, 255, 255]))
    upper = cv2.inRange(hsv, np.array([166, 60, 60]), np.array([180, 255, 255]))
    mask = cv2.bitwise_or(lower, upper)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)


def red_pixel_ratio(image: np.ndarray) -> float:
    mask = red_mask(image)
    return float(np.count_nonzero(mask)) / max(1, mask.size)


def detect(image: np.ndarray) -> list[Seal]:
    """检测页面上的红色印记。"""
    h, w = image.shape[:2]
    page_area = h * w
    mask = red_mask(image)
    # 连通印章内部笔画，使一枚章成为一个连通域。
    # 核必须“横宽纵窄”：质量证明单的检验章是逐行纵向密排的，各向同性的大核
    # 会把整列印章粘成一条，连通域面积超限后被整体丢弃，导致“整列无章”的误报。
    linked = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 5))
    )
    contours, _ = cv2.findContours(linked, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    seals: list[Seal] = []
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        box_area = cw * ch
        if box_area < page_area * 0.00025 or box_area > page_area * 0.12:
            continue
        aspect = cw / max(1, ch)
        if not 0.25 <= aspect <= 6.0:
            continue
        red_px = int(np.count_nonzero(mask[y : y + ch, x : x + cw]))
        if red_px < 60:
            continue
        rect = cv2.minAreaRect(cnt)
        angle = _normalize_angle(rect[2])
        rect_area = max(1.0, rect[1][0] * rect[1][1])
        seals.append(
            Seal(
                bbox=(int(x), int(y), int(cw), int(ch)),
                angle=angle,
                area=box_area,
                fill_ratio=red_px / max(1, box_area),
                rectangularity=float(cv2.contourArea(cnt) / rect_area),
            )
        )
    return merge_nested(seals)


def merge_nested(seals: list[Seal], min_containment: float = 0.9) -> list[Seal]:
    """把被其他印章外接框包含的碎片并回所属印章。

    印色偏淡时闭运算连不通圈内笔画，一枚圆章会碎成“外圈 + 圈内文字”多个
    连通域；圈内碎片被外圈外接框 100% 包含，会被 B02 当成“被完全覆盖的
    第二枚章”误报。红线框（复印确认章）不吸收内部组件——它框住的红色印文
    本就是独立组件，并入会抬高填充率使线框被误判为实体章。
    """
    kept: list[Seal] = []
    for s in sorted(seals, key=lambda s: s.area, reverse=True):
        host = next(
            (i for i, k in enumerate(kept)
             if not k.is_frame and overlap_ratio(s.bbox, k.bbox) >= min_containment),
            None,
        )
        if host is None:
            kept.append(s)
            continue
        k = kept[host]
        x1 = min(k.bbox[0], s.bbox[0])
        y1 = min(k.bbox[1], s.bbox[1])
        x2 = max(k.bbox[0] + k.bbox[2], s.bbox[0] + s.bbox[2])
        y2 = max(k.bbox[1] + k.bbox[3], s.bbox[1] + s.bbox[3])
        area = (x2 - x1) * (y2 - y1)
        red_px = k.fill_ratio * k.area + s.fill_ratio * s.area  # 碎片间像素不相交
        kept[host] = Seal(
            bbox=(x1, y1, x2 - x1, y2 - y1),
            angle=k.angle,
            area=area,
            fill_ratio=min(1.0, red_px / max(1, area)),
            rectangularity=k.rectangularity,
        )
    return sorted(kept, key=lambda s: (s.bbox[1], s.bbox[0]))


def _normalize_angle(angle: float) -> float:
    """OpenCV 的 minAreaRect 角度在不同版本区间不同，统一归到 [-45, 45]。"""
    a = angle % 90
    if a > 45:
        a -= 90
    return float(a)


def iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    return inter / float(aw * ah + bw * bh - inter)


def overlap_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """交集面积占 a 的比例。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return (x2 - x1) * (y2 - y1) / float(max(1, aw * ah))
