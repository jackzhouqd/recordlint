"""印章状态分类的手工特征与纯 numpy 推理。

放在运行时包里而不是 tools/ 下，是因为规则引擎在模型上线后要调用推理；
部署侧只需 numpy，不引入 sklearn/torch，部署形态与现在完全一致。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .. import seal

MASK_SIDE = 32   # 红色掩膜下采样边长，捕捉章面结构与朝向
GRAY_SIDE = 16   # 灰度下采样，补充笔画浓淡信息

LABEL_ORDER = ["ok", "chipped", "blurred", "faint", "upside_down", "not_seal"]
LABEL_CN = {
    "ok": "合格", "chipped": "缺角", "blurred": "模糊",
    "faint": "漏墨", "upside_down": "倒盖", "not_seal": "非印章",
}
# 判为「不合格」的候选标签。真正是否判违规由 rules.yaml 的 reject_labels 决定，
# 这里只是界面上的默认勾选项。
DEFECT_LABELS = ["chipped", "blurred", "faint", "upside_down"]


def featurize(img: np.ndarray) -> np.ndarray:
    """手工特征：章面结构 + 笔画浓淡 + 清晰度 + 几何。

    分开设计是有意的——清晰度必须用梯度类特征，红色掩膜本身丢掉了模糊信息。
    """
    mask = seal.red_mask(img)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    m_small = cv2.resize(mask, (MASK_SIDE, MASK_SIDE), interpolation=cv2.INTER_AREA) / 255.0
    g_small = cv2.resize(gray, (GRAY_SIDE, GRAY_SIDE), interpolation=cv2.INTER_AREA) / 255.0

    h, w = img.shape[:2]
    red_ratio = float(np.count_nonzero(mask)) / max(1, mask.size)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())          # 清晰度：模糊的关键判据
    edges = cv2.Canny(gray, 60, 160)
    edge_ratio = float(np.count_nonzero(edges)) / max(1, edges.size)

    n_cc, _, _, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    frag = (n_cc - 1) / max(1.0, red_ratio * mask.size / 100.0)      # 断裂程度：漏墨的判据

    quad = m_small.reshape(2, MASK_SIDE // 2, 2, MASK_SIDE // 2).mean(axis=(1, 3)).ravel()
    scalars = np.array(
        [w / max(1, h), red_ratio, np.log1p(lap_var) / 10.0, edge_ratio, np.log1p(frag) / 5.0,
         *quad],                                                     # 四象限密度：缺角的判据
        dtype=np.float32,
    )
    return np.concatenate([m_small.ravel().astype(np.float32),
                           g_small.ravel().astype(np.float32), scalars])


def feature_dim() -> int:
    return MASK_SIDE * MASK_SIDE + GRAY_SIDE * GRAY_SIDE + 5 + 4


# ---------------------------------------------------------------- 推理

def load_linear(model_path: str | Path) -> dict:
    z = np.load(str(model_path), allow_pickle=True)
    return {k: z[k] for k in ("mean", "scale", "coef", "intercept", "classes")}


def predict_one(model: dict, img: np.ndarray) -> tuple[str, float]:
    """纯 numpy 推理：部署侧无需 sklearn/torch。"""
    x = (featurize(img) - model["mean"]) / np.where(model["scale"] == 0, 1, model["scale"])
    logits = model["coef"] @ x + model["intercept"]
    e = np.exp(logits - logits.max())
    p = e / e.sum()
    i = int(p.argmax())
    return str(model["classes"][i]), float(p[i])
