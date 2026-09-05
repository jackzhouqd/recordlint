"""训练相关的存储：样本、标注、模型版本。

单独一个仓储而不是塞进 store.py，是为了让训练模块可以整块拆掉而不影响审核主线；
表结构用 CREATE TABLE IF NOT EXISTS，对既有 qaudit.db 是纯增量，不动任何既有列。

标注**存库不存浏览器**：离线标注页的 localStorage 方案只能一个人用、
换台机器就丢、也答不出「这条是谁标的」。受监管制造场景下标注结果最终要进验收材料，
必须能追溯到人。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .features import LABEL_ORDER

SCHEMA = """
CREATE TABLE IF NOT EXISTS seal_sample (
    sample_id  TEXT PRIMARY KEY,
    file       TEXT NOT NULL,          -- 相对样本库根目录的路径
    doc_id     TEXT DEFAULT '',
    page_no    INTEGER DEFAULT 0,
    bbox       TEXT DEFAULT '',
    source     TEXT NOT NULL,          -- 源印章 id；合成样本指向其基底，分组切分靠它
    synthetic  INTEGER DEFAULT 0,
    synth_kind TEXT DEFAULT '',
    angle      REAL DEFAULT 0,
    fill_ratio REAL DEFAULT 0,
    rectangularity REAL DEFAULT 0,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sample_source ON seal_sample(source);
CREATE INDEX IF NOT EXISTS idx_sample_syn ON seal_sample(synthetic);

CREATE TABLE IF NOT EXISTS seal_label (
    sample_id  TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    labeler    TEXT DEFAULT '',
    labeled_at TEXT,
    note       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_label_label ON seal_label(label);

CREATE TABLE IF NOT EXISTS model (
    model_id   TEXT PRIMARY KEY,       -- 形如 seal_cls@v3
    kind       TEXT NOT NULL,
    version    TEXT NOT NULL,
    path       TEXT DEFAULT '',
    trained_at TEXT,
    trainer    TEXT DEFAULT '',
    samples    INTEGER DEFAULT 0,
    human      INTEGER DEFAULT 0,
    groups     INTEGER DEFAULT 0,
    accuracy   REAL DEFAULT 0,
    metrics    TEXT DEFAULT '',
    status     TEXT DEFAULT 'draft'    -- draft / active / retired
);
"""

NOW = "%Y.%m.%d %H:%M:%S"

# 少于这个数量的人工标注不允许上线：全合成样本上的准确率是能力上界，
# 不能当验收依据。数值写在这里而不是散落在界面里，便于质量部统一收紧。
MIN_HUMAN_LABELS = 60

# 自动标注人，不计入人工标注量
AUTO_LABELERS = ("合成", "几何粗筛")


class TrainRepo:
    """训练侧仓储。复用 Store 的连接与审计日志。"""

    def __init__(self, store):
        self.store = store
        self.conn = store.conn
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------ 样本

    def add_samples(self, rows: list[dict]) -> int:
        """幂等写入：同一枚章重复裁剪不会产生重复样本。"""
        if not rows:
            return 0
        now = datetime.now().strftime(NOW)
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO seal_sample (sample_id, file, doc_id, page_no, bbox,"
                " source, synthetic, synth_kind, angle, fill_ratio, rectangularity, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r["sample_id"], r["file"], r.get("doc_id", ""), int(r.get("page_no", 0)),
                  json.dumps(r.get("bbox")) if r.get("bbox") else "",
                  r.get("source") or r["sample_id"], 1 if r.get("synthetic") else 0,
                  r.get("synth_kind", ""), float(r.get("angle", 0)),
                  float(r.get("fill_ratio", 0)), float(r.get("rectangularity", 0)), now)
                 for r in rows])
        return len(rows)

    def samples(self, *, label: str = "", synthetic: str = "", doc_id: str = "",
                limit: int = 200, offset: int = 0) -> list[dict]:
        sql = ["SELECT s.*, l.label, l.labeler, l.labeled_at FROM seal_sample s",
               "LEFT JOIN seal_label l ON l.sample_id = s.sample_id", "WHERE 1=1"]
        args: list[Any] = []
        if label == "__todo__":
            sql.append("AND l.label IS NULL")
        elif label == "__done__":
            sql.append("AND l.label IS NOT NULL")
        elif label:
            sql.append("AND l.label = ?"); args.append(label)
        if synthetic == "real":
            sql.append("AND s.synthetic = 0")
        elif synthetic == "synth":
            sql.append("AND s.synthetic = 1")
        if doc_id:
            sql.append("AND s.doc_id = ?"); args.append(doc_id)
        sql.append("ORDER BY s.synthetic, s.sample_id LIMIT ? OFFSET ?")
        args += [limit, offset]
        return [dict(r) for r in self.conn.execute(" ".join(sql), args)]

    def count_samples(self, **kw) -> int:
        kw.pop("limit", None)
        kw.pop("offset", None)
        return len(self.samples(limit=10 ** 9, **kw))

    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) total, SUM(synthetic) synth FROM seal_sample").fetchone()
        by_label = {r["label"]: r["n"] for r in self.conn.execute(
            "SELECT label, COUNT(*) n FROM seal_label GROUP BY label")}
        labeled = sum(by_label.values())
        total = int(row["total"] or 0)
        synth = int(row["synth"] or 0)
        groups = self.conn.execute(
            "SELECT COUNT(DISTINCT source) n FROM seal_sample").fetchone()["n"]
        docs = self.conn.execute(
            "SELECT COUNT(DISTINCT doc_id) n FROM seal_sample WHERE doc_id != ''").fetchone()["n"]
        return {"total": total, "synthetic": synth, "real": total - synth,
                "labeled": labeled, "todo": total - labeled, "groups": int(groups or 0),
                "docs": int(docs or 0),
                "by_label": {k: by_label.get(k, 0) for k in LABEL_ORDER}}

    def docs(self) -> list[str]:
        return [r["doc_id"] for r in self.conn.execute(
            "SELECT DISTINCT doc_id FROM seal_sample WHERE doc_id != '' ORDER BY doc_id")]

    # ------------------------------------------------------------ 标注

    def set_label(self, sample_id: str, label: str, labeler: str, note: str = "") -> None:
        if label and label not in LABEL_ORDER:
            raise ValueError(f"非法标签: {label}")
        with self.conn:
            if not label:
                self.conn.execute("DELETE FROM seal_label WHERE sample_id=?", (sample_id,))
            else:
                self.conn.execute(
                    "INSERT OR REPLACE INTO seal_label VALUES (?,?,?,?,?)",
                    (sample_id, label, labeler, datetime.now().strftime(NOW), note))
            self.store._log("label", sample_id, f"label={label or '撤销'}", labeler)

    def labeler_stats(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT labeler, COUNT(*) n, MAX(labeled_at) last FROM seal_label"
            " GROUP BY labeler ORDER BY n DESC")]

    def labeled_pairs(self) -> list[tuple[str, str, str, int]]:
        """(file, label, source, synthetic) —— 训练取数用。"""
        return [(r["file"], r["label"], r["source"], int(r["synthetic"])) for r in self.conn.execute(
            "SELECT s.file, s.source, s.synthetic, l.label FROM seal_sample s"
            " JOIN seal_label l ON l.sample_id = s.sample_id")]

    def ok_sources(self) -> list[dict]:
        """人工确认为「合格」的样本，作为合成退化的基底。

        尚无人工标注时退回几何粗筛——形状规整、填充充分的更可能是完整正章。
        """
        rows = [dict(r) for r in self.conn.execute(
            "SELECT s.* FROM seal_sample s JOIN seal_label l ON l.sample_id = s.sample_id"
            " WHERE l.label='ok' AND s.synthetic=0")]
        if rows:
            return rows
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM seal_sample WHERE synthetic=0 AND rectangularity >= 0.75"
            " AND fill_ratio >= 0.2")]

    def has_human_labels(self) -> int:
        """真人标注的真实样本数。

        必须排除「合成」与「几何粗筛」这两个自动标注人——否则粗筛出的基底会把
        计数撑起来，MIN_HUMAN_LABELS 这道上线门槛就形同虚设。
        """
        marks = ",".join("?" * len(AUTO_LABELERS))
        return self.conn.execute(
            "SELECT COUNT(*) n FROM seal_label l JOIN seal_sample s ON s.sample_id=l.sample_id"
            f" WHERE s.synthetic=0 AND l.labeler NOT IN ({marks})",
            tuple(AUTO_LABELERS)).fetchone()["n"]

    # ------------------------------------------------------------ 模型

    def next_version(self, kind: str = "seal_cls") -> str:
        n = self.conn.execute("SELECT COUNT(*) n FROM model WHERE kind=?", (kind,)).fetchone()["n"]
        return f"v{int(n) + 1}"

    def add_model(self, *, kind: str, version: str, path: str, trainer: str,
                  samples: int, human: int, groups: int, accuracy: float,
                  metrics: dict) -> str:
        model_id = f"{kind}@{version}"
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO model (model_id, kind, version, path, trained_at,"
                " trainer, samples, human, groups, accuracy, metrics, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,'draft')",
                (model_id, kind, version, path, datetime.now().strftime(NOW), trainer,
                 samples, human, groups, accuracy,
                 json.dumps(metrics, ensure_ascii=False)))
            self.store._log("train", model_id,
                            f"样本 {samples}（人工 {human}）/ 源印章 {groups} 组 / "
                            f"准确率 {accuracy:.3f}", trainer)
        return model_id

    def models(self, kind: str = "") -> list[dict]:
        sql = "SELECT * FROM model"
        args: list[Any] = []
        if kind:
            sql += " WHERE kind=?"; args.append(kind)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY trained_at DESC", args)]

    def model(self, model_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM model WHERE model_id=?", (model_id,)).fetchone()
        return dict(row) if row else None

    def active(self, kind: str = "seal_cls") -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM model WHERE kind=? AND status='active' LIMIT 1", (kind,)).fetchone()
        return dict(row) if row else None

    def set_status(self, model_id: str, status: str, operator: str = "") -> None:
        """上线是排他的：同类模型同时只允许一个 active，否则批次指纹会含糊。"""
        if status not in ("draft", "active", "retired"):
            raise ValueError(f"非法状态: {status}")
        m = self.model(model_id)
        if m is None:
            raise KeyError(f"模型不存在: {model_id}")
        if status == "active" and m["human"] < MIN_HUMAN_LABELS:
            raise ValueError(
                f"人工标注仅 {m['human']} 枚，少于上线门槛 {MIN_HUMAN_LABELS} 枚。"
                "全合成样本上的准确率是能力上界，不能作为验收依据。")
        with self.conn:
            if status == "active":
                self.conn.execute(
                    "UPDATE model SET status='retired' WHERE kind=? AND status='active'",
                    (m["kind"],))
            self.conn.execute("UPDATE model SET status=? WHERE model_id=?", (status, model_id))
            self.store._log("model_state", model_id, f"status={status}", operator)

    def active_versions(self) -> dict[str, str]:
        """写进批次指纹的 {kind: version}，历史结论据此重放。"""
        return {r["kind"]: r["version"] for r in self.conn.execute(
            "SELECT kind, version FROM model WHERE status='active'")}
