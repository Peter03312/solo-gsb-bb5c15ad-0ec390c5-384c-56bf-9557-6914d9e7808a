"""三角嗅辨盲评工作台 (Triangle Test Blind Evaluation Workbench).

FastAPI + SQLite 后端。职责：
- 负责人创建实验（两种样本编号、评员名单、截止时间、最少有效答卷数）
- 为每位评员生成一次性盲评码，三杯随机顺序，异样杯位置在全体分配中均衡
- 评员凭码提交一次（异样杯位置或弃权），提交后不可修改，截止后拒绝
- 截止前任何接口不泄露样本-杯位对应关系与他人答案
- 截止后负责人结算：有效答卷数、答对数、p=1/3 二项分布单侧尾概率与判定
- 结算与答卷并发时以服务端截止时间为界（BEGIN IMMEDIATE 串行化写事务）
"""
from __future__ import annotations

import json
import math
import os
import random
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR.parent / "data" / "app.db"))

SIGNIFICANCE_THRESHOLD = 0.05   # 显著性阈值
GUESS_PROBABILITY = 1.0 / 3.0   # 三杯随机猜中的概率

# 盲评码字母表：去除易混淆字符（0/O、1/I/L）
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id          TEXT PRIMARY KEY,
    sample_a    TEXT NOT NULL,
    sample_b    TEXT NOT NULL,
    deadline    TEXT NOT NULL,          -- ISO 8601, UTC
    min_valid   INTEGER NOT NULL,
    admin_token TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    settled_at  TEXT,
    result_json TEXT                    -- 结算时冻结的结果快照
);
CREATE TABLE IF NOT EXISTS panelists (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    code          TEXT NOT NULL UNIQUE,  -- 盲评码，仅本人可用
    odd_sample    TEXT NOT NULL CHECK (odd_sample IN ('A','B')),
    odd_position  INTEGER NOT NULL CHECK (odd_position IN (1,2,3)),
    choice        INTEGER CHECK (choice IN (1,2,3)),
    abstained     INTEGER NOT NULL DEFAULT 0,
    submitted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_panelists_exp ON panelists(experiment_id);
"""


# ---------------------------------------------------------------- 基础工具

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)


def get_db():
    """每请求一个连接；isolation_level=None 以便显式控制事务。

    check_same_thread=False：FastAPI 的依赖注入与端点可能运行在不同
    线程池线程；连接为请求级私有，写操作由 BEGIN IMMEDIATE 串行化，安全。
    """
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 写事务：提交与结算在 SQLite 层面串行化，
    保证“以服务端截止时间为界”的并发语义。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def binom_tail(n: int, k: int, p: float = GUESS_PROBABILITY) -> float:
    """P(X >= k)，X ~ Binomial(n, p)。用对数计算保证数值稳定。"""
    if n <= 0 or k <= 0:
        return 1.0
    log_p, log_q = math.log(p), math.log(1.0 - p)
    log_choose = lambda i: (math.lgamma(n + 1) - math.lgamma(i + 1)
                            - math.lgamma(n - i + 1))
    total = 0.0
    for i in range(k, n + 1):
        total += math.exp(log_choose(i) + i * log_p + (n - i) * log_q)
    return min(1.0, total)


def balanced_assignments(n: int) -> list[tuple[str, int]]:
    """生成 n 个 (异样样本, 异样杯位置) 分配。

    6 种组合 (A/B × 位置1/2/3) 先整轮铺开、余数随机抽取，再整体洗牌，
    使异样杯位置（及样本）在全体分配中尽量均衡。
    """
    cells = [(s, p) for s in ("A", "B") for p in (1, 2, 3)]
    rng = random.SystemRandom()
    assignments = cells * (n // len(cells)) + rng.sample(cells, n % len(cells))
    rng.shuffle(assignments)
    return assignments


# ---------------------------------------------------------------- 请求模型

class ExperimentCreate(BaseModel):
    sample_a: str = Field(min_length=1, max_length=100)
    sample_b: str = Field(min_length=1, max_length=100)
    panelists: list[str] = Field(min_length=1, max_length=500)
    deadline: datetime
    min_valid: int = Field(ge=1)


class SubmitBody(BaseModel):
    choice: Optional[int] = Field(default=None, ge=1, le=3)
    abstain: bool = False


# ---------------------------------------------------------------- 应用

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="三角嗅辨盲评工作台", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def no_store_middleware(request: Request, call_next):
    """禁止缓存，避免盲评页面/接口被缓存而泄露状态。"""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- 数据访问

def get_experiment(conn: sqlite3.Connection, exp_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="实验不存在")
    return row


def require_admin(exp: sqlite3.Row, token: Optional[str]) -> None:
    if not token or not secrets.compare_digest(exp["admin_token"], token):
        raise HTTPException(status_code=403, detail="管理令牌无效")


def list_panelists(conn: sqlite3.Connection, exp_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM panelists WHERE experiment_id = ? ORDER BY id", (exp_id,)
    ).fetchall()


def cups_layout(row: sqlite3.Row, sample_a: str, sample_b: str) -> dict[str, str]:
    """由分配推导三杯布局：异样杯位置放 odd_sample，其余两杯放另一样本。"""
    odd_code = sample_a if row["odd_sample"] == "A" else sample_b
    other_code = sample_b if row["odd_sample"] == "A" else sample_a
    return {
        str(pos): (odd_code if pos == row["odd_position"] else other_code)
        for pos in (1, 2, 3)
    }


# ---------------------------------------------------------------- 页面路由

@app.get("/", include_in_schema=False)
def page_index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
def page_admin():
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/t/{code}", include_in_schema=False)
def page_test(code: str):
    return FileResponse(STATIC_DIR / "test.html")


@app.get("/api/health")
def health():
    return {"ok": True, "server_time": iso(utcnow())}


# ---------------------------------------------------------------- 负责人接口

@app.post("/api/experiments", status_code=201)
def create_experiment(body: ExperimentCreate, conn: sqlite3.Connection = Depends(get_db)):
    sample_a, sample_b = body.sample_a.strip(), body.sample_b.strip()
    if not sample_a or not sample_b:
        raise HTTPException(422, "样本编号不能为空")
    if sample_a == sample_b:
        raise HTTPException(422, "两种样本编号必须不同")

    names: list[str] = []
    seen = set()
    for raw in body.panelists:
        name = raw.strip()
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    if not names:
        raise HTTPException(422, "评员名单不能为空")

    deadline = body.deadline
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    deadline = deadline.astimezone(timezone.utc)
    if deadline <= utcnow():
        raise HTTPException(422, "截止时间必须晚于当前时间")

    admin_token = secrets.token_urlsafe(24)
    assignments = balanced_assignments(len(names))

    with write_tx(conn):
        exp_id = None
        for _ in range(20):  # 主键冲突时重试
            candidate = secrets.token_hex(4)
            try:
                conn.execute(
                    "INSERT INTO experiments (id, sample_a, sample_b, deadline,"
                    " min_valid, admin_token, created_at) VALUES (?,?,?,?,?,?,?)",
                    (candidate, sample_a, sample_b, iso(deadline), body.min_valid,
                     admin_token, iso(utcnow())),
                )
            except sqlite3.IntegrityError:
                continue
            exp_id = candidate
            break
        if exp_id is None:  # pragma: no cover - 理论上不会到达
            raise HTTPException(500, "实验编号生成失败")
        codes: dict[str, str] = {}
        for name, (odd_sample, odd_position) in zip(names, assignments):
            for _ in range(20):  # UNIQUE 冲突时重试
                code = new_code()
                try:
                    conn.execute(
                        "INSERT INTO panelists (experiment_id, name, code, odd_sample,"
                        " odd_position) VALUES (?,?,?,?,?)",
                        (exp_id, name, code, odd_sample, odd_position),
                    )
                except sqlite3.IntegrityError:
                    continue
                codes[name] = code
                break
            else:  # pragma: no cover - 理论上不会到达
                raise HTTPException(500, "盲评码生成失败")

    return {
        "experiment_id": exp_id,
        "admin_token": admin_token,
        "admin_path": f"/admin?exp={exp_id}&token={admin_token}",
        "deadline": iso(deadline),
        "min_valid": body.min_valid,
        "panelists": [
            {"name": name, "code": codes[name], "test_path": f"/t/{codes[name]}"}
            for name in names
        ],
    }


@app.get("/api/experiments/{exp_id}")
def get_experiment_status(
    exp_id: str,
    token: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_db),
):
    """负责人仪表盘数据。截止/结算前只返回提交状态，不返回任何答案与杯位分配。"""
    exp = get_experiment(conn, exp_id)
    require_admin(exp, token)
    rows = list_panelists(conn, exp_id)
    now = utcnow()
    deadline = parse_dt(exp["deadline"])
    settled = exp["settled_at"] is not None
    return {
        "id": exp["id"],
        "sample_a": exp["sample_a"],
        "sample_b": exp["sample_b"],
        "deadline": exp["deadline"],
        "min_valid": exp["min_valid"],
        "created_at": exp["created_at"],
        "server_time": iso(now),
        "is_open": now <= deadline,
        "settled": settled,
        "settled_at": exp["settled_at"],
        "counts": {
            "total": len(rows),
            "submitted": sum(1 for r in rows if r["submitted_at"]),
        },
        "panelists": [
            {
                "name": r["name"],
                "code": r["code"],
                "test_path": f"/t/{r['code']}",
                "submitted": r["submitted_at"] is not None,
            }
            for r in rows
        ],
        "result": json.loads(exp["result_json"]) if settled else None,
    }


@app.get("/api/experiments/{exp_id}/preparation")
def get_preparation_sheet(
    exp_id: str,
    token: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_db),
):
    """倒样制备单（保密）：仅负责人凭管理令牌获取，用于物理倒样。

    这是截止前唯一暴露样本-杯位对应关系的接口；评员页面与仪表盘均不含此信息。
    """
    exp = get_experiment(conn, exp_id)
    require_admin(exp, token)
    rows = list_panelists(conn, exp_id)
    return {
        "experiment_id": exp_id,
        "sample_a": exp["sample_a"],
        "sample_b": exp["sample_b"],
        "confidential": True,
        "sheet": [
            {
                "name": r["name"],
                "code": r["code"],
                "cups": cups_layout(r, exp["sample_a"], exp["sample_b"]),
            }
            for r in rows
        ],
    }


@app.post("/api/experiments/{exp_id}/settle")
def settle_experiment(
    exp_id: str,
    token: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_db),
):
    """结算：仅截止后可用；幂等，重复调用返回冻结结果。"""
    with write_tx(conn):
        exp = get_experiment(conn, exp_id)
        require_admin(exp, token)
        if exp["settled_at"] is not None:
            return json.loads(exp["result_json"])
        now = utcnow()
        if now <= parse_dt(exp["deadline"]):
            raise HTTPException(400, "未到截止时间，不能结算")
        rows = list_panelists(conn, exp_id)
        result = compute_result(exp, rows, now)
        conn.execute(
            "UPDATE experiments SET settled_at = ?, result_json = ? WHERE id = ?",
            (iso(now), json.dumps(result, ensure_ascii=False), exp_id),
        )
        return result


@app.get("/api/experiments/{exp_id}/results")
def get_results(
    exp_id: str,
    token: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_db),
):
    """回看冻结结果（分配与答卷）。仅在结算后可用。"""
    exp = get_experiment(conn, exp_id)
    require_admin(exp, token)
    if exp["settled_at"] is None:
        raise HTTPException(409, "实验尚未结算，结果不可用")
    return json.loads(exp["result_json"])


def compute_result(exp: sqlite3.Row, rows: list[sqlite3.Row], settled_at: datetime) -> dict:
    submitted = [r for r in rows if r["submitted_at"] is not None]
    valid = [r for r in submitted if not r["abstained"]]
    n = len(valid)
    k = sum(1 for r in valid if r["choice"] == r["odd_position"])
    p_value = binom_tail(n, k)

    if n < exp["min_valid"]:
        verdict = "insufficient"
        verdict_text = (
            f"样本不足：有效答卷 {n} 份，未达到最少要求 {exp['min_valid']} 份，无法判定。"
        )
    elif p_value <= SIGNIFICANCE_THRESHOLD:
        verdict = "discriminable"
        verdict_text = (
            f"可辨：有效答卷 {n} 份（≥ {exp['min_valid']}），答对 {k} 份，"
            f"尾概率 p={p_value:.4g} ≤ {SIGNIFICANCE_THRESHOLD}，判定两种样本可辨。"
        )
    else:
        verdict = "not_significant"
        verdict_text = (
            f"证据未达阈值：有效答卷 {n} 份，答对 {k} 份，"
            f"尾概率 p={p_value:.4g} > {SIGNIFICANCE_THRESHOLD}，不能判定两种样本可辨。"
        )

    return {
        "experiment_id": exp["id"],
        "sample_a": exp["sample_a"],
        "sample_b": exp["sample_b"],
        "deadline": exp["deadline"],
        "min_valid": exp["min_valid"],
        "settled_at": iso(settled_at),
        "total_panelists": len(rows),
        "submitted_count": len(submitted),
        "abstained_count": len(submitted) - n,
        "valid_count": n,
        "correct_count": k,
        "guess_probability": GUESS_PROBABILITY,
        "significance_threshold": SIGNIFICANCE_THRESHOLD,
        "p_value": p_value,
        "verdict": verdict,
        "verdict_text": verdict_text,
        "panelists": [
            {
                "name": r["name"],
                "code": r["code"],
                "cups": cups_layout(r, exp["sample_a"], exp["sample_b"]),
                "odd_sample": r["odd_sample"],
                "odd_position": r["odd_position"],
                "choice": r["choice"],
                "abstained": bool(r["abstained"]),
                "correct": (
                    None if r["submitted_at"] is None or r["abstained"]
                    else r["choice"] == r["odd_position"]
                ),
                "submitted_at": r["submitted_at"],
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------- 评员接口

def get_panelist_by_code(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM panelists WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="盲评码无效")
    return row


@app.get("/api/panel/{code}")
def panel_view(code: str, conn: sqlite3.Connection = Depends(get_db)):
    """评员页面数据。绝不返回样本编号、杯位分配、对错或他人信息。"""
    p = get_panelist_by_code(conn, code)
    exp = get_experiment(conn, p["experiment_id"])
    now = utcnow()
    deadline = parse_dt(exp["deadline"])
    submitted = p["submitted_at"] is not None
    if submitted:
        status = "submitted"
    elif now > deadline:
        status = "closed"
    else:
        status = "open"
    return {
        "status": status,
        "deadline": exp["deadline"],
        "server_time": iso(now),
        "cups": [1, 2, 3],
        "submitted": submitted,
        "my_choice": p["choice"] if submitted else None,
        "abstained": bool(p["abstained"]) if submitted else None,
        "submitted_at": p["submitted_at"],
    }


@app.post("/api/panel/{code}/submit")
def panel_submit(
    code: str,
    body: SubmitBody,
    conn: sqlite3.Connection = Depends(get_db),
):
    """提交答卷：每人一次、提交后不可修改、截止后拒绝。

    与结算并发时以服务端截止时间为界：两者都在 BEGIN IMMEDIATE
    写事务内检查时间，SQLite 保证串行化，先到者先判定。
    """
    if body.abstain and body.choice is not None:
        raise HTTPException(422, "弃权与选择杯位不能同时提交")
    if not body.abstain and body.choice is None:
        raise HTTPException(422, "请选择异样杯位置（1/2/3）或弃权")

    with write_tx(conn):
        p = get_panelist_by_code(conn, code)
        exp = get_experiment(conn, p["experiment_id"])
        now = utcnow()
        if exp["settled_at"] is not None or now > parse_dt(exp["deadline"]):
            raise HTTPException(409, "已过截止时间，答卷被拒绝")
        if p["submitted_at"] is not None:
            raise HTTPException(409, "每位评员只能提交一次，您已提交过")
        conn.execute(
            "UPDATE panelists SET choice = ?, abstained = ?, submitted_at = ? WHERE id = ?",
            (None if body.abstain else body.choice, 1 if body.abstain else 0,
             iso(now), p["id"]),
        )
    return {"ok": True, "submitted_at": iso(now)}
