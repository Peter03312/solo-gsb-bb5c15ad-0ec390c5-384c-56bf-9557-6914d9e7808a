# 三角嗅辨盲评工作台

基于 **FastAPI + SQLite + 轻量前端**（原生 HTML/JS，无构建步骤）的三点检验（Triangle Test）盲评系统，供实验负责人组织“两种气味样本是否可辨”的盲评实验。

## 功能

- **负责人建实验**：输入两种样本编号、评员名单、截止时间、最少有效答卷数。
- **盲评码**：系统为每位评员生成仅本人可用的一次性盲评码（8 位，去除易混淆字符）。
- **三杯随机**：每人三杯，两杯为同一样本、一杯为异样杯；异样杯位置（及异样样本）在全体分配中按 6 种组合整轮铺开 + 随机余数，尽量均衡。
- **单次作答**：评员凭码只能提交一次（异样杯位置 1/2/3 或弃权），提交后不可修改；截止后拒绝答卷。
- **全程盲态**：截止前任何页面/接口均不泄露样本-杯位对应关系与他人答案（评员页连样本编号都不返回）；所有响应 `Cache-Control: no-store`。
- **截止结算**：负责人截止后一键结算，报告有效答卷数 n、答对数 k、随机猜中概率 p₀=1/3 时的二项分布单侧尾概率 P(X≥k)。
- **判定规则**：`n ≥ 最少有效数 且 P(X≥k) ≤ 0.05` 判为**可辨**；`n` 不足判**样本不足**；否则判**证据未达阈值**。
- **并发语义**：提交与结算均在 `BEGIN IMMEDIATE` 写事务内以**服务端时间**判定，SQLite 串行化保证“以截止时间为界”，先到先判。
- **结果冻结**：结算结果快照入库，之后可回看全部分配与答卷，不再变化。

## 快速开始

### 方式一：Docker Compose（推荐，一键启动）

```bash
docker compose up --build -d
```

数据持久化在命名卷 `triangle-data` 中。停止：`docker compose down`。

### 方式二：Docker

```bash
docker build -t triangle-test-workbench .
docker run -d --name triangle-test -p 8000:8000 -v triangle-data:/data triangle-test-workbench
```

### 方式三：本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 地址与端口

| 入口 | 地址 | 说明 |
|---|---|---|
| 首页（创建实验） | http://localhost:8000/ | 负责人建实验、获取盲评码 |
| 管理台 | http://localhost:8000/admin?exp=<实验ID>&token=<管理令牌> | 创建成功后由系统生成，请妥善保存 |
| 评员页 | http://localhost:8000/t/<盲评码> | 逐一分发给对应评员 |
| API 文档 | http://localhost:8000/docs | FastAPI 自动文档 |
| 健康检查 | http://localhost:8000/api/health | 含服务器时间 |

默认端口 **8000**，可在 `docker-compose.yml` 的 `ports` 中改映射（如 `"8080:8000"`）。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DB_PATH` | 容器内 `/data/app.db`；本地 `data/app.db` | SQLite 数据库文件路径 |

显著性阈值（0.05）与猜中概率（1/3）为方法学常量，定义在 `app/main.py` 顶部。

## 使用流程

1. 负责人在首页创建实验 → 保存**管理链接**，把**盲评码/评员链接**私下分发给各评员。
2. （物理实验必需）管理台点击“查看倒样制备单”获取每人的杯位-样本对应关系，**保密倒样**。
3. 评员打开自己的链接，嗅辨三杯后选择异样杯或弃权并提交；提交后不可修改。
4. 到截止时间后（以服务器时间为准），负责人在管理台点击“截止结算”。
5. 查看判定与统计；结果冻结后可随时回看分配与答卷明细。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/experiments` | 创建实验，返回管理令牌与盲评码 |
| GET | `/api/experiments/{id}?token=` | 实验状态（结算前不含分配与答案） |
| GET | `/api/experiments/{id}/preparation?token=` | 倒样制备单（保密） |
| POST | `/api/experiments/{id}/settle?token=` | 截止后结算（幂等） |
| GET | `/api/experiments/{id}/results?token=` | 回看冻结结果（仅结算后） |
| GET | `/api/panel/{code}` | 评员页数据（无样本/分配信息） |
| POST | `/api/panel/{code}/submit` | 提交 `{choice:1..3}` 或 `{abstain:true}` |

## 判定方法学

三杯中随机猜对异样杯的概率 p₀=1/3。设有效答卷 n 份（弃权不计入）、答对 k 份，则

> P(X ≥ k)，X ~ Binomial(n, 1/3)

为单侧尾概率（对数空间计算，数值稳定）。仅当 n 达到最少有效答卷数 **且** 该概率 ≤ 0.05 时判定两种样本**可辨**。

## 验证

```bash
python3 scripts/smoke_test.py            # 对 http://127.0.0.1:8000 执行端到端冒烟测试
python3 scripts/smoke_test.py http://host:port
```

覆盖：分配均衡、盲态不泄露、单次提交、截止边界、并发提交一致性、尾概率手算比对、三种判定分支、结果冻结回看。

## 项目结构

```
app/
  main.py            # FastAPI 应用：模型、接口、统计与判定
  static/
    index.html       # 首页：创建实验
    admin.html       # 管理台：进度、制备单、结算、冻结结果
    test.html        # 评员页：三杯盲评提交
scripts/smoke_test.py
Dockerfile
docker-compose.yml
requirements.txt
```
