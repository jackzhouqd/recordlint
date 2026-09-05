"""SQLite 存储层：审核批次、单据、页、疑点、人工判定、版本指纹。

选型理由：单机部署 + 服务化架构。用标准库 sqlite3 而不是 PostgreSQL，
是因为离线安装介质与运维成本最低；表结构按服务端形态设计，将来换库不改上层。

受监管制造场景的硬要求是**可复现**：每条疑点都绑定五个版本指纹
（页面 / 规则库 / 引擎 / OCR 模型 / 判定模型），任何一条历史结论都能重放验证。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS run (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    operator      TEXT DEFAULT '',
    target        TEXT NOT NULL,
    rules_version TEXT,
    rules_hash    TEXT,
    engine_version TEXT,
    ocr_model     TEXT,
    model_versions TEXT,
    pages         INTEGER DEFAULT 0,
    units         INTEGER DEFAULT 0,
    findings      INTEGER DEFAULT 0,
    seconds       REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS page (
    page_key   TEXT PRIMARY KEY,          -- run_id|doc_id|page_no
    run_id     TEXT NOT NULL,
    doc_id     TEXT NOT NULL,
    page_no    INTEGER NOT NULL,
    form_type  TEXT,
    source     TEXT,                      -- 原始文件路径（只引用不复制）
    text_lines INTEGER, seals INTEGER, findings INTEGER
);
CREATE INDEX IF NOT EXISTS idx_page_run ON page(run_id, doc_id, page_no);

CREATE TABLE IF NOT EXISTS unit (
    unit_key   TEXT PRIMARY KEY,          -- run_id|unit_id
    run_id     TEXT NOT NULL,
    unit_id    TEXT NOT NULL,
    doc_id     TEXT NOT NULL,
    form_type  TEXT,
    start_page INTEGER, end_page INTEGER, page_count INTEGER,
    declared_total INTEGER,
    keys       TEXT,
    pages      TEXT DEFAULT ''           -- JSON 页号数组。单据的页集合允许不连续，
                                         -- 交错装订时首末页号之间夹着别的单据
);
CREATE INDEX IF NOT EXISTS idx_unit_run ON unit(run_id, doc_id);

CREATE TABLE IF NOT EXISTS finding (
    finding_key TEXT PRIMARY KEY,         -- run_id|doc_id|page_no|rule_id|序号
    run_id  TEXT NOT NULL,
    doc_id  TEXT NOT NULL,
    page_no INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    level   TEXT, title TEXT, clause TEXT, message TEXT,
    bbox    TEXT, evidence TEXT, confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_finding_run ON finding(run_id, level);
CREATE INDEX IF NOT EXISTS idx_finding_rule ON finding(rule_id);
CREATE INDEX IF NOT EXISTS idx_finding_doc ON finding(doc_id, page_no);

CREATE TABLE IF NOT EXISTS adjudication (
    finding_key TEXT PRIMARY KEY,
    run_id  TEXT NOT NULL,
    doc_id  TEXT NOT NULL,
    page_no INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    verdict TEXT NOT NULL,                -- true / false / unsure
    reviewer TEXT DEFAULT '',
    note    TEXT DEFAULT '',
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adj_run ON adjudication(run_id, verdict);

CREATE TABLE IF NOT EXISTS user (
    username     TEXT PRIMARY KEY,
    display_name TEXT DEFAULT '',
    role         TEXT NOT NULL,          -- admin / reviewer / viewer
    salt         BLOB NOT NULL,
    pwd_hash     BLOB NOT NULL,
    enabled      INTEGER DEFAULT 1,
    created_at   TEXT,
    failed_count INTEGER DEFAULT 0,
    locked_until TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS session (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_at TEXT, expires_at TEXT, client TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_user ON session(username);

CREATE TABLE IF NOT EXISTS rule_override (
    rule_id    TEXT PRIMARY KEY,
    enabled    INTEGER,                  -- NULL 表示不覆盖
    level      TEXT,
    changed_by TEXT, changed_at TEXT, reason TEXT DEFAULT '',
    params     TEXT DEFAULT ''           -- JSON，只存被改动的参数项
);

CREATE TABLE IF NOT EXISTS task (
    task_id    TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,            -- audit / gold
    status     TEXT NOT NULL,            -- running / done / failed / canceled
    target     TEXT, run_id TEXT, operator TEXT,
    total      INTEGER DEFAULT 0,
    done       INTEGER DEFAULT 0,
    message    TEXT DEFAULT '',
    started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_status ON task(status, started_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, operator TEXT, action TEXT, target TEXT, detail TEXT
);
"""


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    started_at: str
    target: str
    pages: int
    units: int
    findings: int
    rules_version: str = ""
    operator: str = ""


# 角色与权限。受监管制造场景下判定必须能追溯到具体人员，因此判定一律绑定登录账号，
# 不接受前端自填姓名。
ROLES = {
    "admin": "管理员（用户管理、导入批次、复核）",
    "reviewer": "复核员（复核判定）",
    "viewer": "查阅员（只读）",
}
CAN_ADJUDICATE = frozenset({"admin", "reviewer"})
CAN_ADMIN = frozenset({"admin"})

MAX_FAILED = 5          # 连续失败次数上限
LOCK_MINUTES = 15       # 超过上限后的锁定时长
SESSION_HOURS = 8       # 会话有效期
TIME_FMT = "%Y.%m.%d %H:%M:%S"


def _hash_password(password: str, salt: bytes) -> bytes:
    """scrypt 口令散列。标准库实现，不引入新依赖。"""
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def file_hash(path: str | Path, packs: "str | list | None" = "auto") -> str:
    """规则库指纹 = 通用层 + 规则包（``packs``: "auto" 同级 packs/ 全部；None 只算通用层；
    或显式路径列表）。换一个规则包判定结果就不同，指纹必须跟着变。"""
    from .findings import rules_bundle_hash

    p = Path(path)
    if not p.exists():
        return ""
    return rules_bundle_hash(p, packs)


class Store:
    """薄封装。所有写操作都记审计日志——受监管制造场景要能回答“谁在什么时候动了什么”。"""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """幂等增量迁移：只加列，不改既有列，老库直接可用。

        CREATE TABLE IF NOT EXISTS 对已存在的表不会补列，因此新增列必须在这里显式处理。
        """
        for table, column, ddl in (
            ("rule_override", "params", "ALTER TABLE rule_override ADD COLUMN params TEXT DEFAULT ''"),
            ("unit", "pages", "ALTER TABLE unit ADD COLUMN pages TEXT DEFAULT ''"),
        ):
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(ddl)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ 写入

    def import_report(
        self,
        payload: dict,
        *,
        run_id: str,
        target: str,
        operator: str = "",
        rules_path: str | Path | None = None,
        engine_version: str = "",
        model_versions: dict | None = None,
        no_packs: bool = False,
    ) -> RunInfo:
        """把一次 audit 产出的 findings.json 导入库。"""
        stats = payload.get("stats", {})
        meta = payload.get("rulebook", {})
        now = datetime.now().strftime("%Y.%m.%d %H:%M:%S")

        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO run (run_id, started_at, operator, target, rules_version,"
                " rules_hash, engine_version, ocr_model, model_versions, pages, units, findings, seconds)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, payload.get("generated_at", now), operator, str(target),
                    str(meta.get("version", "")),
                    file_hash(rules_path, packs=None if no_packs else "auto") if rules_path else "",
                    engine_version, "RapidOCR-PP-OCRv4",
                    json.dumps(model_versions or {}, ensure_ascii=False),
                    stats.get("pages", 0), stats.get("units", 0), stats.get("findings", 0),
                    stats.get("seconds", 0),
                ),
            )
            self.conn.execute("DELETE FROM page WHERE run_id=?", (run_id,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO page VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (f"{run_id}|{r['doc_id']}|{r['page_no']}", run_id, r["doc_id"], r["page_no"],
                     r.get("form_type"), r.get("source", ""), r.get("text_lines"), r.get("seals"),
                     r.get("findings"))
                    for r in payload.get("pages", [])
                ],
            )
            self.conn.execute("DELETE FROM unit WHERE run_id=?", (run_id,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO unit (unit_key, run_id, unit_id, doc_id, form_type,"
                " start_page, end_page, page_count, declared_total, keys, pages)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (f"{run_id}|{u['unit_id']}", run_id, u["unit_id"], u["doc_id"], u.get("form_type"),
                     u.get("start_page"), u.get("end_page"), u.get("page_count"),
                     u.get("declared_total"), json.dumps(u.get("keys", {}), ensure_ascii=False),
                     json.dumps(u.get("pages") or [], ensure_ascii=False))
                    for u in payload.get("units", [])
                ],
            )
            self.conn.execute("DELETE FROM finding WHERE run_id=?", (run_id,))
            rows = []
            seq: dict[tuple, int] = {}
            for f in payload.get("findings", []):
                base = (f["doc_id"], f["page_no"], f["rule_id"])
                seq[base] = seq.get(base, 0) + 1
                key = f"{run_id}|{base[0]}|{base[1]}|{base[2]}|{seq[base]}"
                rows.append(
                    (key, run_id, f["doc_id"], f["page_no"], f["rule_id"], f.get("level"),
                     f.get("title"), f.get("clause"), f.get("message"),
                     json.dumps(f.get("bbox")), f.get("evidence", ""), f.get("confidence", 1.0))
                )
            self.conn.executemany("INSERT OR REPLACE INTO finding VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self._log("import", str(target), f"run={run_id} findings={len(rows)}", operator)

        return RunInfo(
            run_id=run_id, started_at=now, target=str(target),
            pages=stats.get("pages", 0), units=stats.get("units", 0),
            findings=len(rows), rules_version=str(meta.get("version", "")), operator=operator,
        )

    def adjudicate(self, finding_key: str, verdict: str, reviewer: str = "", note: str = "") -> None:
        if verdict not in ("true", "false", "unsure", ""):
            raise ValueError(f"非法判定: {verdict}")
        row = self.conn.execute(
            "SELECT run_id, doc_id, page_no, rule_id FROM finding WHERE finding_key=?", (finding_key,)
        ).fetchone()
        if row is None:
            raise KeyError(f"疑点不存在: {finding_key}")
        with self.conn:
            if verdict == "":
                self.conn.execute("DELETE FROM adjudication WHERE finding_key=?", (finding_key,))
            else:
                self.conn.execute(
                    "INSERT OR REPLACE INTO adjudication VALUES (?,?,?,?,?,?,?,?,?)",
                    (finding_key, row["run_id"], row["doc_id"], row["page_no"], row["rule_id"],
                     verdict, reviewer, note, datetime.now().strftime("%Y.%m.%d %H:%M:%S")),
                )
            self._log("adjudicate", finding_key, f"verdict={verdict or '撤销'}", reviewer)

    def log_event(self, action: str, target: str, detail: str = "", operator: str = "") -> None:
        """供 Web 层记录不落在既有写操作里的动作（如档案来源变更），随即提交。"""
        self._log(action, target, detail, operator)
        self.conn.commit()

    def _log(self, action: str, target: str, detail: str, operator: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit_log (at, operator, action, target, detail) VALUES (?,?,?,?,?)",
            (datetime.now().strftime("%Y.%m.%d %H:%M:%S"), operator, action, target, detail),
        )

    # ------------------------------------------------------------ 规则覆盖

    def rule_overrides(self) -> dict[str, dict]:
        """界面上对规则库做的调整。

        rules.yaml 是带注释的基线，界面改动不回写 yaml，而是记成覆盖项——
        既保住了注释与版本基线，又让每次调整都有人、有时间、有理由可查。
        """
        return {r["rule_id"]: dict(r) for r in self.conn.execute("SELECT * FROM rule_override")}

    def set_rule_override(self, rule_id: str, *, enabled: bool | None = None,
                          level: str | None = None, params: dict | None = None,
                          operator: str = "", reason: str = "") -> None:
        """写入规则覆盖项。

        params 只存**被改动的参数项**，不是整份参数快照——否则规则库基线里新增
        或调整一个参数默认值时，这条覆盖会把老值永久钉死。
        传 params={} 表示清空参数覆盖，传 None 表示本次不动参数。
        """
        cur = self.rule_overrides().get(rule_id, {})
        new_enabled = cur.get("enabled") if enabled is None else (1 if enabled else 0)
        new_level = level if level is not None else cur.get("level")
        if params is None:
            new_params = cur.get("params") or ""
        else:
            new_params = json.dumps(params, ensure_ascii=False) if params else ""
        with self.conn:
            if new_enabled is None and not new_level and not new_params:
                self.conn.execute("DELETE FROM rule_override WHERE rule_id=?", (rule_id,))
            else:
                self.conn.execute(
                    "INSERT OR REPLACE INTO rule_override"
                    " (rule_id, enabled, level, changed_by, changed_at, reason, params)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (rule_id, new_enabled, new_level, operator,
                     datetime.now().strftime(TIME_FMT), reason, new_params),
                )
            detail = f"enabled={new_enabled} level={new_level}"
            if params is not None:
                detail += f" params={new_params or '（已清空）'}"
            self._log("rule_change", rule_id, f"{detail} 理由={reason or '未填'}", operator)

    def clear_rule_override(self, rule_id: str, operator: str = "") -> None:
        with self.conn:
            self.conn.execute("DELETE FROM rule_override WHERE rule_id=?", (rule_id,))
            self._log("rule_reset", rule_id, "恢复规则库基线", operator)

    # ------------------------------------------------------------ 后台任务

    def create_task(self, task_id: str, kind: str, target: str, operator: str,
                    total: int = 0) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO task (task_id, kind, status, target, operator, total, done,"
                " started_at) VALUES (?,?,'running',?,?,?,0,?)",
                (task_id, kind, target, operator, total, datetime.now().strftime(TIME_FMT)),
            )
            self._log("task_start", task_id, f"{kind} {target}", operator)

    def update_task(self, task_id: str, *, done: int | None = None, total: int | None = None,
                    message: str | None = None) -> None:
        sets, args = [], []
        for col, val in (("done", done), ("total", total), ("message", message)):
            if val is not None:
                sets.append(f"{col}=?")
                args.append(val)
        if not sets:
            return
        args.append(task_id)
        with self.conn:
            self.conn.execute(f"UPDATE task SET {','.join(sets)} WHERE task_id=?", args)

    def finish_task(self, task_id: str, status: str, message: str = "",
                    run_id: str = "", operator: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE task SET status=?, message=?, run_id=?, finished_at=? WHERE task_id=?",
                (status, message, run_id, datetime.now().strftime(TIME_FMT), task_id),
            )
            self._log("task_finish", task_id, f"{status} {message}"[:120], operator)

    def task(self, task_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def tasks(self, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM task ORDER BY started_at DESC LIMIT ?", (limit,))]

    def reconcile_tasks(self) -> int:
        """服务启动时清理孤儿任务。

        任务状态活在数据库里，执行线程活在进程里。进程被杀 / 机器重启 / 服务重启后，
        DB 里的 running 永远不会有人来改，界面上就会挂着一个永远转圈的任务，
        并且「正在审核」的提示再也消不掉。启动时统一收敛一次。
        """
        with self.conn:
            cur = self.conn.execute(
                "UPDATE task SET status='failed', message=?, finished_at=?"
                " WHERE status='running'",
                ("服务重启，任务已中断（进度未保存，请重新发起）",
                 datetime.now().strftime(TIME_FMT)),
            )
            if cur.rowcount:
                self._log("task_reconcile", "startup", f"中断 {cur.rowcount} 个孤儿任务")
        return cur.rowcount

    def cancel_task(self, task_id: str, operator: str = "") -> None:
        """手动把任务标记为已取消。

        只改状态不杀线程——后台线程没有安全的中断点，强杀可能让样本库写到一半。
        真在跑的任务会继续跑完并覆盖这个状态；卡死的任务则就此从界面上消失。
        """
        row = self.conn.execute("SELECT status FROM task WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"任务不存在: {task_id}")
        if row["status"] != "running":
            raise ValueError(f"任务已是 {row['status']} 状态，无需取消")
        with self.conn:
            self.conn.execute(
                "UPDATE task SET status='canceled', message=?, finished_at=? WHERE task_id=?",
                ("已由操作人手动标记为取消", datetime.now().strftime(TIME_FMT), task_id))
            self._log("task_cancel", task_id, "手动取消", operator)

    def running_task(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM task WHERE status='running' ORDER BY started_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ 用户与会话

    def create_user(self, username: str, password: str, *, role: str = "reviewer",
                    display_name: str = "", operator: str = "") -> None:
        if role not in ROLES:
            raise ValueError(f"非法角色: {role}（可选 {'/'.join(ROLES)}）")
        if len(password) < 8:
            raise ValueError("口令至少 8 位")
        if not username or len(username) > 40:
            raise ValueError("用户名不合法")
        salt = secrets.token_bytes(16)
        with self.conn:
            self.conn.execute(
                "INSERT INTO user (username, display_name, role, salt, pwd_hash, enabled, created_at)"
                " VALUES (?,?,?,?,?,1,?)",
                (username, display_name or username, role, salt,
                 _hash_password(password, salt), datetime.now().strftime(TIME_FMT)),
            )
            self._log("user_create", username, f"role={role}", operator)

    def set_password(self, username: str, password: str, operator: str = "") -> None:
        if len(password) < 8:
            raise ValueError("口令至少 8 位")
        salt = secrets.token_bytes(16)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE user SET salt=?, pwd_hash=?, failed_count=0, locked_until='' WHERE username=?",
                (salt, _hash_password(password, salt), username),
            )
            if cur.rowcount == 0:
                raise KeyError(f"用户不存在: {username}")
            self.conn.execute("DELETE FROM session WHERE username=?", (username,))  # 改密即下线
            self._log("user_passwd", username, "口令已重置", operator)

    def set_user_state(self, username: str, *, role: str | None = None,
                       enabled: bool | None = None, operator: str = "") -> None:
        if role is not None and role not in ROLES:
            raise ValueError(f"非法角色: {role}")
        with self.conn:
            if role is not None:
                self.conn.execute("UPDATE user SET role=? WHERE username=?", (role, username))
            if enabled is not None:
                self.conn.execute("UPDATE user SET enabled=? WHERE username=?",
                                  (1 if enabled else 0, username))
                if not enabled:
                    self.conn.execute("DELETE FROM session WHERE username=?", (username,))
            self._log("user_update", username,
                      f"role={role or '-'} enabled={enabled if enabled is not None else '-'}",
                      operator)

    def users(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT username, display_name, role, enabled, created_at, failed_count, locked_until"
            " FROM user ORDER BY username")]

    def has_users(self) -> bool:
        return self.conn.execute("SELECT COUNT(*) c FROM user").fetchone()["c"] > 0

    def authenticate(self, username: str, password: str) -> dict | None:
        """校验口令。连续失败达上限即临时锁定，防止口令穷举。"""
        row = self.conn.execute("SELECT * FROM user WHERE username=?", (username,)).fetchone()
        if row is None:
            _hash_password(password, b"0" * 16)  # 等时开销，避免用响应快慢探测用户名
            return None
        if not row["enabled"]:
            return None
        locked = row["locked_until"] or ""
        if locked and datetime.now() < datetime.strptime(locked, TIME_FMT):
            raise PermissionError(f"账号已锁定至 {locked}")

        ok = hmac.compare_digest(_hash_password(password, row["salt"]), row["pwd_hash"])
        with self.conn:
            if ok:
                self.conn.execute(
                    "UPDATE user SET failed_count=0, locked_until='' WHERE username=?", (username,))
            else:
                failed = int(row["failed_count"]) + 1
                until = ""
                if failed >= MAX_FAILED:
                    until = (datetime.now() + timedelta(minutes=LOCK_MINUTES)).strftime(TIME_FMT)
                    failed = 0
                self.conn.execute(
                    "UPDATE user SET failed_count=?, locked_until=? WHERE username=?",
                    (failed, until, username))
                self._log("login_failed", username, f"失败计数={failed}")
        return dict(row) if ok else None

    def create_session(self, username: str, client: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now()
        with self.conn:
            self.conn.execute("DELETE FROM session WHERE expires_at < ?", (now.strftime(TIME_FMT),))
            self.conn.execute(
                "INSERT INTO session VALUES (?,?,?,?,?)",
                (token, username, now.strftime(TIME_FMT),
                 (now + timedelta(hours=SESSION_HOURS)).strftime(TIME_FMT), client),
            )
            self._log("login", username, "登录成功", username)
        return token

    def session_user(self, token: str) -> dict | None:
        if not token:
            return None
        row = self.conn.execute(
            "SELECT s.expires_at, u.* FROM session s JOIN user u ON u.username = s.username"
            " WHERE s.token = ?", (token,)).fetchone()
        if row is None or not row["enabled"]:
            return None
        if datetime.now() > datetime.strptime(row["expires_at"], TIME_FMT):
            self.delete_session(token)
            return None
        return dict(row)

    def delete_session(self, token: str, operator: str = "") -> None:
        with self.conn:
            self.conn.execute("DELETE FROM session WHERE token=?", (token,))
            if operator:
                self._log("logout", operator, "退出登录", operator)

    # ------------------------------------------------------------ 查询

    def runs(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM adjudication a WHERE a.run_id=r.run_id) AS judged"
            " FROM run r ORDER BY started_at DESC")]

    def run(self, run_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def findings(
        self, run_id: str, *, level: str = "", rule_id: str = "", doc_id: str = "",
        judged: str = "", limit: int = 200, offset: int = 0,
    ) -> list[dict]:
        sql = [
            "SELECT f.*, a.verdict, a.reviewer, a.note FROM finding f",
            "LEFT JOIN adjudication a ON a.finding_key = f.finding_key",
            "WHERE f.run_id = ?",
        ]
        args: list[Any] = [run_id]
        if level:
            sql.append("AND f.level = ?"); args.append(level)
        if rule_id:
            sql.append("AND f.rule_id = ?"); args.append(rule_id)
        if doc_id:
            sql.append("AND f.doc_id = ?"); args.append(doc_id)
        if judged == "todo":
            sql.append("AND a.verdict IS NULL")
        elif judged == "done":
            sql.append("AND a.verdict IS NOT NULL")
        sql.append(
            "ORDER BY CASE f.level WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1"
            " WHEN 'MEDIUM' THEN 2 ELSE 3 END, f.doc_id, f.page_no LIMIT ? OFFSET ?"
        )
        args += [limit, offset]
        return [dict(r) for r in self.conn.execute(" ".join(sql), args)]

    def count_findings(self, run_id: str, **kw) -> int:
        rows = self.findings(run_id, limit=10**9, **kw)
        return len(rows)

    def summary(self, run_id: str) -> dict:
        by_level = {r["level"]: r["n"] for r in self.conn.execute(
            "SELECT level, COUNT(*) n FROM finding WHERE run_id=? GROUP BY level", (run_id,))}
        by_rule = [dict(r) for r in self.conn.execute(
            "SELECT rule_id, COUNT(*) n FROM finding WHERE run_id=? GROUP BY rule_id ORDER BY n DESC",
            (run_id,))]
        by_verdict = {r["verdict"]: r["n"] for r in self.conn.execute(
            "SELECT verdict, COUNT(*) n FROM adjudication WHERE run_id=? GROUP BY verdict", (run_id,))}
        docs = [dict(r) for r in self.conn.execute(
            "SELECT doc_id, COUNT(*) n FROM finding WHERE run_id=? GROUP BY doc_id ORDER BY n DESC",
            (run_id,))]
        return {"by_level": by_level, "by_rule": by_rule, "by_verdict": by_verdict, "docs": docs}

    def page_source(self, run_id: str, doc_id: str, page_no: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM page WHERE run_id=? AND doc_id=? AND page_no=?", (run_id, doc_id, page_no)
        ).fetchone()
        return dict(row) if row else None

    def units(self, run_id: str, doc_id: str = "") -> list[dict]:
        sql = "SELECT * FROM unit WHERE run_id=?"
        args: list[Any] = [run_id]
        if doc_id:
            sql += " AND doc_id=?"; args.append(doc_id)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY doc_id, start_page", args)]

    def history(self, doc_id: str) -> list[dict]:
        """同一份档案历次审核的结论——回答“这批档案以前审出过什么”。"""
        return [dict(r) for r in self.conn.execute(
            "SELECT r.run_id, r.started_at, r.rules_version, COUNT(f.finding_key) n"
            " FROM run r LEFT JOIN finding f ON f.run_id=r.run_id AND f.doc_id=?"
            " GROUP BY r.run_id HAVING n > 0 ORDER BY r.started_at DESC", (doc_id,))]

    def audit_log(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]

    def rule_hit_counts(self) -> dict[str, int]:
        """全部批次的规则累计命中，用于规则库页面展示实际效果。"""
        return {r["rule_id"]: r["n"] for r in self.conn.execute(
            "SELECT rule_id, COUNT(*) n FROM finding GROUP BY rule_id")}

    # ------------------------------------------------------------ 金标准与评测

    def merge_goldset(self, run_id: str, gold_path: str | Path, operator: str = "") -> dict:
        """把本批人工判定并入金标准集。

        判真 → 正样本；判假 → 负样本（回归用）；存疑不计入，留待复议。
        """
        gold_path = Path(gold_path)
        gold: dict = {}
        if gold_path.exists():
            gold = json.loads(gold_path.read_text(encoding="utf-8"))
        positives = {(x["doc_id"], int(x["page_no"]), x["rule_id"]): x
                     for x in gold.get("findings", [])}
        negatives = {(x["doc_id"], int(x["page_no"]), x["rule_id"])
                     for x in gold.get("false_positives", [])}
        unsure = {(x["doc_id"], int(x["page_no"]), x["rule_id"]) for x in gold.get("unsure", [])}

        stats = {"true": 0, "false": 0, "unsure": 0}
        for item in self.export_adjudications(run_id):
            key = (item["doc_id"], int(item["page_no"]), item["rule_id"])
            verdict = item["verdict"]
            stats[verdict] = stats.get(verdict, 0) + 1
            if verdict == "true":
                positives[key] = {"doc_id": key[0], "page_no": key[1], "rule_id": key[2],
                                  "note": item.get("note", ""), "reviewer": item.get("reviewer", "")}
                negatives.discard(key)
                unsure.discard(key)
            elif verdict == "false":
                negatives.add(key)
                positives.pop(key, None)
                unsure.discard(key)
            else:
                unsure.add(key)

        gold.setdefault("_说明", "金标准集：由质量部人工判定累积而成，是验收的唯一判据。")
        gold["findings"] = sorted(positives.values(),
                                  key=lambda x: (x["doc_id"], x["page_no"], x["rule_id"]))
        gold["false_positives"] = [{"doc_id": d, "page_no": p, "rule_id": r}
                                   for d, p, r in sorted(negatives)]
        gold["unsure"] = [{"doc_id": d, "page_no": p, "rule_id": r} for d, p, r in sorted(unsure)]
        gold_path.parent.mkdir(parents=True, exist_ok=True)
        gold_path.write_text(json.dumps(gold, ensure_ascii=False, indent=1), encoding="utf-8")
        with self.conn:
            self._log("gold_merge", str(gold_path),
                      f"判真 {stats['true']}／判假 {stats['false']}／存疑 {stats['unsure']}", operator)
        return stats

    def evaluate(self, run_id: str, gold_path: str | Path) -> dict:
        """按显式标注计分：判真算召回，判假算误报，存疑与未判定都不计分。"""
        gold_path = Path(gold_path)
        gold = json.loads(gold_path.read_text(encoding="utf-8")) if gold_path.exists() else {}
        positives = {(x["doc_id"], int(x["page_no"]), x["rule_id"]) for x in gold.get("findings", [])}
        negatives = {(x["doc_id"], int(x["page_no"]), x["rule_id"])
                     for x in gold.get("false_positives", [])}
        unsure = {(x["doc_id"], int(x["page_no"]), x["rule_id"]) for x in gold.get("unsure", [])}
        pred = {(r["doc_id"], int(r["page_no"]), r["rule_id"])
                for r in self.findings(run_id, limit=10 ** 9)}

        tp = len(positives & pred)
        fn = len(positives - pred)
        fp = len(negatives & pred)
        return {
            "positives": len(positives), "negatives": len(negatives),
            "tp": tp, "fp": fp, "fn": fn,
            "unjudged": len(pred - positives - negatives - unsure),
            "recall": tp / max(1, tp + fn), "precision": tp / max(1, tp + fp),
        }

    def export_adjudications(self, run_id: str) -> list[dict]:
        """导出人工判定，供 `qaudit gold` 合并进金标准集。"""
        return [dict(r) for r in self.conn.execute(
            "SELECT doc_id, page_no, rule_id, verdict, reviewer, note FROM adjudication"
            " WHERE run_id=?", (run_id,))]
