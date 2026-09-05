"""OCR 封装（RapidOCR / onnxruntime，模型随包，可完全离线）。

带磁盘缓存：同一页只识别一次，规则调试时可反复迭代而不重复付出 OCR 代价。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class TextLine:
    """一行 OCR 结果。bbox 为外接矩形 (x, y, w, h)。"""

    text: str
    bbox: tuple[int, int, int, int]
    score: float

    @property
    def cx(self) -> float:
        return self.bbox[0] + self.bbox[2] / 2

    @property
    def cy(self) -> float:
        return self.bbox[1] + self.bbox[3] / 2

    @property
    def x2(self) -> int:
        return self.bbox[0] + self.bbox[2]

    @property
    def y2(self) -> int:
        return self.bbox[1] + self.bbox[3]


class OcrEngine:
    """惰性初始化的 OCR 引擎，附带页级缓存。"""

    def __init__(self, cache_dir: str | Path | None = None):
        self._engine: Any = None
        self._cache_dir = Path(cache_dir) if cache_dir else None
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _lazy_engine(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        return self._engine

    @staticmethod
    def _key(image: np.ndarray) -> str:
        h = hashlib.sha1()
        h.update(str(image.shape).encode())
        h.update(image[::7, ::7].tobytes())  # 抽样即可唯一标识一页
        return h.hexdigest()[:20]

    def run(self, image: np.ndarray) -> list[TextLine]:
        cache_file = None
        if self._cache_dir is not None:
            cache_file = self._cache_dir / f"{self._key(image)}.json"
            if cache_file.exists():
                try:
                    raw = json.loads(cache_file.read_text(encoding="utf-8"))
                    return [TextLine(r["text"], tuple(r["bbox"]), r["score"]) for r in raw]
                except Exception:
                    pass  # 缓存损坏则重算

        lines = self._recognize(image)

        if cache_file is not None:
            payload = [{"text": l.text, "bbox": list(l.bbox), "score": l.score} for l in lines]
            cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return lines

    def _recognize(self, image: np.ndarray) -> list[TextLine]:
        result, _ = self._lazy_engine()(image)
        if not result:
            return []
        lines: list[TextLine] = []
        for item in result:
            box, text, score = item[0], item[1], item[2]
            pts = np.asarray(box, dtype=np.float32)
            x, y = pts[:, 0].min(), pts[:, 1].min()
            w, h = pts[:, 0].max() - x, pts[:, 1].max() - y
            lines.append(
                TextLine(
                    text=str(text).strip(),
                    bbox=(int(x), int(y), int(w), int(h)),
                    score=float(score),
                )
            )
        return lines


def full_text(lines: list[TextLine]) -> str:
    """按阅读顺序拼接整页文本，供文档级规则使用。"""
    ordered = sorted(lines, key=lambda l: (l.cy // 20, l.cx))
    return "\n".join(l.text for l in ordered)


def find_lines(lines: list[TextLine], keyword: str) -> list[TextLine]:
    return [l for l in lines if keyword in l.text]
