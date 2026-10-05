#!/usr/bin/env python3
"""冒烟测试：对运行中的服务执行端到端校验。

用法:
    python3 scripts/smoke_test.py [BASE_URL]      # 默认 http://127.0.0.1:8000

覆盖：创建实验、分配均衡、盲评保密、单次提交/弃权/一次性、截止边界、
结算统计（二项尾概率手算比对）、三种判定、结果冻结回看。
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
FAILURES = []


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        FAILURES.append(name)


def odd_pos(cups):
    return int([p for p in "123" if list(cups.values()).count(cups[p]) == 1][0])


# ---- 主流程：1 小时后截止 ----
deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
st, exp = call("POST", "/api/experiments", {
    "sample_a": "A-001", "sample_b": "B-002",
    "panelists": ["张三", "李四", "王五", "赵六", "钱七", "孙八"],
    "deadline": deadline, "min_valid": 4})
check("创建实验", st == 201, f"st={st}")
eid, token = exp["experiment_id"], exp["admin_token"]
codes = {p["name"]: p["code"] for p in exp["panelists"]}
check("盲评码唯一", len(set(codes.values())) == 6)

st, prep = call("GET", f"/api/experiments/{eid}/preparation?token={token}")
positions, samples = {}, {}
for row in prep["sheet"]:
    p = odd_pos(row["cups"])
    positions[p] = positions.get(p, 0) + 1
    s = row["cups"][str(p)]
    samples[s] = samples.get(s, 0) + 1
check("异样杯位置均衡", sorted(positions.values()) == [2, 2, 2], str(positions))
check("异样样本均衡", sorted(samples.values()) == [3, 3], str(samples))

st, _ = call("GET", f"/api/experiments/{eid}")
check("无令牌 403", st == 403)
st, dash = call("GET", f"/api/experiments/{eid}?token={token}")
blob = json.dumps(dash, ensure_ascii=False)
check("仪表盘不泄露分配/答案",
      "odd_position" not in blob and "cups" not in blob and "choice" not in blob)

st, view = call("GET", f"/api/panel/{codes['张三']}")
blob = json.dumps(view, ensure_ascii=False)
check("评员页不泄露样本与分配",
      st == 200 and "A-001" not in blob and "B-002" not in blob and "odd" not in blob)

sheet = {r["name"]: r["cups"] for r in prep["sheet"]}
for n in ["张三", "李四", "王五"]:
    call("POST", f"/api/panel/{codes[n]}/submit", {"choice": odd_pos(sheet[n])})
call("POST", f"/api/panel/{codes['赵六']}/submit",
     {"choice": (odd_pos(sheet["赵六"]) % 3) + 1})
call("POST", f"/api/panel/{codes['钱七']}/submit", {"abstain": True})
st, _ = call("POST", f"/api/panel/{codes['张三']}/submit", {"choice": 1})
check("重复提交 409", st == 409)
st, _ = call("POST", f"/api/experiments/{eid}/settle?token={token}")
check("截止前结算 400", st == 400)
st, _ = call("GET", f"/api/experiments/{eid}/results?token={token}")
check("结算前查结果 409", st == 409)

# ---- 短截止：截止拒绝 + 结算 + 冻结 ----
dl2 = (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat()
st, exp2 = call("POST", "/api/experiments", {
    "sample_a": "X", "sample_b": "Y",
    "panelists": ["甲", "乙", "丙", "丁", "戊"],
    "deadline": dl2, "min_valid": 4})
eid2, token2 = exp2["experiment_id"], exp2["admin_token"]
codes2 = {p["name"]: p["code"] for p in exp2["panelists"]}
st, prep2 = call("GET", f"/api/experiments/{eid2}/preparation?token={token2}")
sheet2 = {r["name"]: r["cups"] for r in prep2["sheet"]}
for n in ["甲", "乙", "丙", "丁"]:
    call("POST", f"/api/panel/{codes2[n]}/submit", {"choice": odd_pos(sheet2[n])})
time.sleep(3.5)
st, _ = call("POST", f"/api/panel/{codes2['戊']}/submit", {"choice": 1})
check("截止后提交 409", st == 409)
st, res = call("POST", f"/api/experiments/{eid2}/settle?token={token2}")
check("结算 200", st == 200)
check("有效=4 答对=4", res["valid_count"] == 4 and res["correct_count"] == 4)
check("尾概率=1/81", abs(res["p_value"] - 1 / 81) < 1e-9, f"p={res['p_value']}")
check("判定可辨", res["verdict"] == "discriminable", res["verdict_text"])
st, res2 = call("POST", f"/api/experiments/{eid2}/settle?token={token2}")
check("结算幂等", res2["settled_at"] == res["settled_at"])
st, frozen = call("GET", f"/api/experiments/{eid2}/results?token={token2}")
check("冻结回看含分配与答卷",
      st == 200 and "cups" in json.dumps(frozen) and "choice" in json.dumps(frozen))

# ---- 判定分支：样本不足 / 未达阈值 ----
dl3 = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
st, exp3 = call("POST", "/api/experiments", {
    "sample_a": "P", "sample_b": "Q", "panelists": ["一", "二"],
    "deadline": dl3, "min_valid": 5})
c3 = {p["name"]: p["code"] for p in exp3["panelists"]}
call("POST", f"/api/panel/{c3['一']}/submit", {"choice": 1})
time.sleep(2.5)
st, res3 = call("POST", f"/api/experiments/{exp3['experiment_id']}/settle?token={exp3['admin_token']}")
check("样本不足判定", res3["verdict"] == "insufficient", res3["verdict_text"])

dl4 = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
st, exp4 = call("POST", "/api/experiments", {
    "sample_a": "M", "sample_b": "N", "panelists": ["a", "b", "c", "d", "e"],
    "deadline": dl4, "min_valid": 3})
eid4, token4 = exp4["experiment_id"], exp4["admin_token"]
c4 = {p["name"]: p["code"] for p in exp4["panelists"]}
st, prep4 = call("GET", f"/api/experiments/{eid4}/preparation?token={token4}")
sheet4 = {r["name"]: r["cups"] for r in prep4["sheet"]}
call("POST", f"/api/panel/{c4['a']}/submit", {"choice": odd_pos(sheet4["a"])})
for n in ["b", "c", "d", "e"]:
    call("POST", f"/api/panel/{c4[n]}/submit",
         {"choice": (odd_pos(sheet4[n]) % 3) + 1})
time.sleep(2.5)
st, res4 = call("POST", f"/api/experiments/{eid4}/settle?token={token4}")
check("未达阈值判定", res4["verdict"] == "not_significant",
      f"k={res4['correct_count']}/n={res4['valid_count']} p={res4['p_value']:.4f}")
check("尾概率手算一致", abs(res4["p_value"] - (1 - (2 / 3) ** 5)) < 1e-9)

for path, name in [("/", "首页"), ("/admin", "管理台"), (f"/t/{codes['张三']}", "评员页")]:
    with urllib.request.urlopen(BASE + path) as r:
        check(f"{name} 200", r.status == 200)

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败: {FAILURES}")
    sys.exit(1)
print("✅ 冒烟测试全部通过")
