# 盲样分类复核 API（Blind Classification Review）

用 Python + FastAPI + SQLite 实现的盲样分类复核 HTTP API。完整流程：

1. **建批**：提交允许类别与样本（代号 + 真实类别），创建响应中一次性返回**两份评审凭据 + 一份仲裁凭据**（仅此一次，数据库只存哈希）。
2. **双评审独立分类**：两名评审各持凭据提交覆盖全部样本的分类；**交卷后不可修改**。
3. **盲态**：封存前普通查询只返回样本代号与阶段；评审只能查到自己的提交状态，看不到真值，也看不到对方分类。
4. **仲裁**：两份交卷齐全后，仲裁者凭专属凭据查看分歧样本及双方分类，逐项从两者中选择最终分类；一致样本直接取共同分类。**无分歧时第二份交卷即直接封存。**
5. **封存揭盲**：分歧全部裁定后一次性封存。查询返回逐样本最终分类、真值、两名评审各自正确数。封存结果落库后不再变化。
6. **揭盲勘误**：封存后若发现样本真值录错，仲裁者凭原凭据提交**期望修订号**与若干样本的更正真值，生成新的勘误版本。**版本 0 是封存原件**，后续版本只替换指定样本的真值；最终分类与两份评审分类永远不变。按版本查询会以该版真值重算逐样本正误与三方正确数，并列出本版相对上一版更正的样本。原有批次/结果查询始终返回封存原件。
7. **独立复评**：对**已封存**批次，其仲裁者凭原仲裁凭据发起独立复评并指定一个**已存在的真值修订号**；服务以该版样本（代号、顺序、类别与真值）创建一个全新批次，并发放**两份全新评审凭据 + 一份全新仲裁凭据**。复评完全沿用交卷 → 裁定 → 封存流程；封存前普通查询与评审查询不暴露来源批次、快照真值及上一轮分类。复评封存后可查**跨轮对照**：按样本顺序列出两轮最终分类、变化标记、各自正误与两轮正确总数，全部按选定修订号的真值计算；来源轮分类始终取自来源批次的封存原件。来源批次此后继续勘误不影响复评快照与对照。

## 快速开始（单条命令）

```bash
docker compose up --build
```

服务监听本地 **8000** 端口，SQLite 数据保存在命名卷 `blindreview-data` 中，容器重启后数据仍在。

不使用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000   # 数据默认写入 ./data/blindreview.db
```

数据库路径可用环境变量 `DB_PATH` 覆盖。

## API 一览

| 方法 | 路径 | 凭据 | 说明 |
|---|---|---|---|
| POST | `/batches` | 无 | 创建批次，响应中返回三份凭据（仅此一次） |
| GET | `/batches/{id}` | 无 | 盲态：仅代号 + 阶段；封存后：完整揭盲结果 |
| GET | `/batches/{id}/submission` | `X-Reviewer-Token` | 评审查询自己的提交状态（只见自己的分类） |
| POST | `/batches/{id}/submissions` | `X-Reviewer-Token` | 评审交卷（覆盖全部样本，交卷后不可改） |
| GET | `/batches/{id}/disagreements` | `X-Arbiter-Token` | 仲裁者查看分歧样本及双方分类 |
| POST | `/batches/{id}/adjudications` | `X-Arbiter-Token` | 仲裁者逐项裁定分歧，齐全后自动封存 |
| GET | `/batches/{id}/results` | 无 | 揭盲结果（仅封存后可用，永远返回版本 0 封存原件） |
| POST | `/batches/{id}/errata` | `X-Arbiter-Token` | 提交勘误：期望修订号 + 更正真值，生成下一版本（仅封存后） |
| GET | `/batches/{id}/errata` | 无 | 列出版本链（0 = 封存原件，及每次勘误更正的样本） |
| GET | `/batches/{id}/errata/{rev}` | 无 | 按版本查询：以该版真值重算的逐样本正误与三方正确数 |
| POST | `/batches/{id}/re-reviews` | `X-Arbiter-Token` | 对已封存批次按指定真值修订号发起独立复评，返回新批次 ID 与三份全新凭据 |
| GET | `/batches/{id}/comparison` | 无 | 复评封存后的跨轮对照（普通批次 404；复评未封存 409） |
| GET | `/healthz` | 无 | 健康检查 |

阶段流转：`collecting`（收卷中）→ `adjudicating`（仲裁中，有分歧时）→ `sealed`（已封存揭盲）。

## 可执行的调用示例

以下示例假设服务运行在 `http://localhost:8000`。也可以直接运行端到端脚本 `./scripts/demo.sh`（需要 curl 与 python3），它会自动走完全部流程。

### 1. 创建批次（凭据只在本响应中出现，请妥善保存）

```bash
curl -sS -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "allowed_categories": ["cat", "dog", "bird"],
    "samples": [
      {"code": "S01", "true_label": "cat"},
      {"code": "S02", "true_label": "dog"},
      {"code": "S03", "true_label": "bird"}
    ]
  }'
```

响应（节选）：

```json
{
  "batch_id": "9f2c...",
  "phase": "collecting",
  "reviewer1_token": "....",
  "reviewer2_token": "....",
  "arbiter_token": "....",
  "notice": "These credentials are shown only in this response and cannot be recovered."
}
```

下文用 `$BID`、`$R1`、`$R2`、`$ARB` 分别代指批次 ID 与三份凭据。

### 2. 盲态查询：只有代号与阶段

```bash
curl -sS http://localhost:8000/batches/$BID
# {"batch_id":"9f2c...","phase":"collecting","samples":["S01","S02","S03"]}
```

### 3. 两名评审分别交卷

```bash
curl -sS -X POST http://localhost:8000/batches/$BID/submissions \
  -H "X-Reviewer-Token: $R1" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "bird"}}'

curl -sS -X POST http://localhost:8000/batches/$BID/submissions \
  -H "X-Reviewer-Token: $R2" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "cat"}}'
# 第二份交卷后：有分歧 -> {"phase":"adjudicating"}；无分歧 -> {"phase":"sealed"}
```

评审只能查自己的提交状态：

```bash
curl -sS http://localhost:8000/batches/$BID/submission -H "X-Reviewer-Token: $R1"
```

重复交卷返回 `409`，原判不变。

### 4. 仲裁者查看分歧并逐项裁定

```bash
curl -sS http://localhost:8000/batches/$BID/disagreements -H "X-Arbiter-Token: $ARB"
# {"disagreements":[{"code":"S03","reviewer1_label":"bird","reviewer2_label":"cat"}], ...}

curl -sS -X POST http://localhost:8000/batches/$BID/adjudications \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"decisions": {"S03": "bird"}}'
# 分歧全部裁定 -> 一次性封存：{"status":"sealed","phase":"sealed"}
```

裁定必须**恰好覆盖全部分歧样本**，且每个选择只能是该样本两位评审之一的标签；封存后再次裁定返回 `409`。

### 5. 揭盲查询

```bash
curl -sS http://localhost:8000/batches/$BID/results
```

```json
{
  "phase": "sealed",
  "results": [
    {"code": "S01", "final_label": "cat",  "true_label": "cat",  "reviewer1_label": "cat",  "reviewer2_label": "cat", "agreed": true,  "reviewer1_correct": true, "reviewer2_correct": true},
    {"code": "S03", "final_label": "bird", "true_label": "bird", "reviewer1_label": "bird", "reviewer2_label": "cat", "agreed": false, "reviewer1_correct": true, "reviewer2_correct": false}
  ],
  "summary": {"sample_count": 3, "disagreement_count": 1, "reviewer1_correct": 3, "reviewer2_correct": 2, "final_correct": 3}
}
```

### 6. 封存后勘误：更正录错的真值

封存后发现 S02 的真值应为 `cat` 而非 `dog`，仲裁者凭原凭据提交勘误。`expected_revision` 必须等于当前修订号（初始为 0），否则整次拒绝并返回当前修订号：

```bash
curl -sS -X POST http://localhost:8000/batches/$BID/errata \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 0,
    "corrections": [
      {"code": "S02", "true_label": "cat"}
    ]
  }'
```

```json
{
  "batch_id": "9f2c...",
  "revision": 1,
  "created_at": "2026-10-06T...",
  "corrected": [{"code": "S02", "previous_true_label": "dog", "true_label": "cat"}]
}
```

校验规则（任一不满足即**整次拒绝**，不留下部分版本）：

- 仅接受本批次样本代号与允许类别，同次请求中代号不得重复（`422`）；
- 每条更正都必须实际改变该样本当前真值，否则视为无实际变化（`422`）；
- `expected_revision` 过期返回 `409`，响应体中带有 `current_revision`，据此重提即可；
- 仅封存后可勘误（否则 `409`）；仅仲裁凭据可用，评审凭据一律 `401`。

### 7. 按版本查询勘误结果

```bash
# 版本链一览（版本 0 为封存原件）
curl -sS http://localhost:8000/batches/$BID/errata

# 按版本查询：保留原最终分类与两份评审分类，按该版真值重算正误
curl -sS http://localhost:8000/batches/$BID/errata/1
```

```json
{
  "revision": 1,
  "latest_revision": 1,
  "corrected": [{"code": "S02", "previous_true_label": "dog", "true_label": "cat"}],
  "results": [
    {"code": "S02", "final_label": "dog", "true_label": "cat", "reviewer1_label": "dog", "reviewer2_label": "dog", "agreed": true, "reviewer1_correct": false, "reviewer2_correct": false, "final_correct": false}
  ],
  "summary": {"sample_count": 3, "disagreement_count": 1, "reviewer1_correct": 2, "reviewer2_correct": 1, "final_correct": 2}
}
```

（`results` 为节选，实际包含全部样本；`summary` 为三方全量统计。）

`corrected` 列出本版相对上一版更正的样本（版本 0 为空）。`GET /batches/{id}` 与 `GET /batches/{id}/results` 不受勘误影响，始终返回版本 0 封存原件；`GET /batches/{id}/errata/0` 可查到与之一致的原始口径。

### 8. 发起独立复评（仲裁者，凭来源批次原仲裁凭据）

来源批次必须**已封存**，`truth_revision` 必须是来源批次**当前存在**的修订号（0 = 封存原件，可先通过 `GET /batches/$BID/errata` 查看版本链）。来源未封存返回 `409`；修订号不存在返回 `404`；凭据缺失/错误/使用评审凭据返回 `401`。

```bash
curl -sS -X POST http://localhost:8000/batches/$BID/re-reviews \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"truth_revision": 1}'
```

```json
{
  "batch_id": "f526...",
  "phase": "collecting",
  "sample_count": 3,
  "source_batch_id": "9f2c...",
  "source_truth_revision": 1,
  "reviewer1_token": "....",
  "reviewer2_token": "....",
  "arbiter_token": "....",
  "notice": "These credentials are shown only in this response and cannot be recovered."
}
```

新批次复制该修订号下全部样本的代号、顺序、允许类别与真值，另发的三份凭据与来源批次完全独立、互不通用（新凭据不能操作来源批次，旧凭据也不能操作复评）。下文用 `$RR`、`$RR1`、`$RR2`、`$RRARB` 代指复评批次与三份新凭据。

**封存前保持盲态**：普通查询仍只返回代号与阶段，不包含来源批次 ID、修订号、真值或旧轮分类；评审只能查自己的提交；`results` 与 `comparison` 在封存前一律 `409`。

```bash
curl -sS http://localhost:8000/batches/$RR
# {"batch_id":"f526...","phase":"collecting","samples":["S01","S02","S03"]}
```

### 9. 复评沿用原流程交卷、裁定、封存

复评没有任何特殊接口，直接复用交卷 / 分歧 / 裁定 / 封存：

```bash
# 两名新评审分别交卷（按修订号 1 的真值，S02 为 cat）
curl -sS -X POST http://localhost:8000/batches/$RR/submissions \
  -H "X-Reviewer-Token: $RR1" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "cat", "S03": "bird"}}'

curl -sS -X POST http://localhost:8000/batches/$RR/submissions \
  -H "X-Reviewer-Token: $RR2" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "cat", "S03": "cat"}}'
# 有分歧 -> adjudicating；无分歧 -> 第二份交卷直接 sealed

# 有分歧时由新仲裁凭据裁定（无分歧则跳过本步）
curl -sS http://localhost:8000/batches/$RR/disagreements -H "X-Arbiter-Token: $RRARB"
curl -sS -X POST http://localhost:8000/batches/$RR/adjudications \
  -H "X-Arbiter-Token: $RRARB" -H 'Content-Type: application/json' \
  -d '{"decisions": {"S03": "bird"}}'
```

封存后的 `GET /batches/$RR` 与 `GET /batches/$RR/results` 为普通揭盲结果（额外带 `source_batch_id`、`source_truth_revision` 两个溯源字段）。复评批次自身也可以按原规则继续勘误，但这不影响对照所用的选定真值。

### 10. 跨轮对照（仅复评封存后）

```bash
curl -sS http://localhost:8000/batches/$RR/comparison
```

```json
{
  "batch_id": "f526...",
  "phase": "sealed",
  "source_batch_id": "9f2c...",
  "source_revision": 1,
  "source_sealed_at": "2026-10-06T...",
  "rereview_sealed_at": "2026-10-07T...",
  "results": [
    {"code": "S01", "true_label": "cat", "source_final_label": "cat", "rereview_final_label": "cat",  "changed": false, "source_correct": true,  "rereview_correct": true},
    {"code": "S02", "true_label": "cat", "source_final_label": "dog", "rereview_final_label": "cat",  "changed": true,  "source_correct": false, "rereview_correct": true},
    {"code": "S03", "true_label": "bird", "source_final_label": "bird", "rereview_final_label": "bird", "changed": false, "source_correct": true,  "rereview_correct": true}
  ],
  "summary": {"sample_count": 3, "changed_count": 1, "source_correct": 2, "rereview_correct": 3}
}
```

- 逐样本按**样本顺序**排列；`changed` 标记两轮最终分类是否不同。
- `source_correct` / `rereview_correct` 与两个正确总数（`summary.source_correct`、`summary.rereview_correct`）**一律按发起复评时选定的真值修订号**计算；`source_final_label` 永远取来源批次 `finals` 封存原件。
- 复评创建后来源批次再勘误（甚至产生更新修订号），复评快照与本对照**都不变化**；对照只反映创建那一刻选定的修订状态。
- 普通批次调用该接口返回 `404`；复评未封存返回 `409`。

## 设计要点

- **凭据**：`secrets.token_urlsafe(32)` 生成，数据库只存 SHA-256 哈希，仅在创建响应中明文返回一次；评审凭据与仲裁凭据角色互不通用（401）。
- **不可改写**：交卷与裁定均有唯一约束与状态机保护，重复提交返回 `409` 且原数据不变。
- **交错请求安全**：所有写操作在进程级锁内以 `BEGIN IMMEDIATE` 事务执行；校验失败整体回滚。封存时一次性写入不可变的 `finals` 结果表，揭盲查询只读该表——不会提前揭盲，也不会出现两份不同结果。
- **勘误版本链**：勘误只追加 `errata` / `errata_items` 表，封存原件（`finals`）永不动。修订号从 0（封存原件）起严格递增；提交时校验 `expected_revision` 等于当前修订号，并发更正因此在同一事务内顺次生效、落后者以 `409 + current_revision` 拒绝，任何失败整体回滚、不留部分版本。按版本查询时用该版真值重算正误，最终分类与评审分类始终取自封存原件。
- **独立复评快照**：发起复评在单个 `BEGIN IMMEDIATE` 事务内完成校验与整批复制（类别 + 每个样本在选定修订号下的真值），与勘误并发时也只会取到**同一修订状态的全部样本**；来源未封存（409）、修订号不存在（404）、凭据无效（401）均整体回滚，不留部分批次。复制后复评与来源批次再无数据耦合，来源继续勘误不会改变快照。
- **复评盲态与对照**：复评封存前复用盲态查询，溯源信息（来源批次、修订号）只出现在创建响应（发给已授权的来源仲裁者）与封存后的揭盲载荷中。跨轮对照在复评封存后才可用：来源轮分类取自来源 `finals` 封存原件，真值取自选定修订号，逐样本输出变化标记与各自正误，并汇总两轮正确总数。
- **持久化**：SQLite 文件（WAL 模式）落盘，Compose 挂载命名卷，重启后各勘误版本、复评批次与跨轮对照仍可查询；旧版数据库在打开时自动补加复评溯源列（幂等迁移）。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

覆盖：完整流程（有/无分歧）、重复提交不改写、盲态信息隔离、凭据角色隔离、参数校验、并发重复交卷仅一人成功、交错请求不提前揭盲、重启后数据一致；勘误的版本链查询、校验整次拒绝、修订号过期返回当前值、并发更正仅一条生效、封存原件不受勘误影响；复评的指定修订快照、全新凭据角色隔离、来源未封存/修订不存在拒绝、失败不留部分批次、与勘误并发的整版一致性、封存前来源与旧轮分类不泄露、复评沿用交卷/裁定/封存、封存后跨轮对照（变化标记/正误/两轮总数）、来源后续勘误不影响快照与对照、旧库迁移与重启后可查。
