# 树木标本鉴定供数 API

基于 **FastAPI + SQLite** 的标本鉴定供数服务。同一件树木标本可以有多份鉴定，
名录历次换版可能**拆种**（一个物种代码 → 多个代码）或**并种**（多个代码 → 同一代码）。
服务按现行名录对每份**有效鉴定**逐版转换，取候选物种交集后给出入库结论。

## 一、业务规则与结论状态

- **登记**：提交 `标本号 + 名录版号 + 物种代码`，系统校验版本与代码确实存在，返回鉴定编号。
- **撤回**：按鉴定编号软撤回（保留历史记录），重复撤回返回 409。
- **发布新版**：请求体必须给出**前版每一个代码在新版的一个或多个去向**：
  - 拆种：一个源代码映射到多个目标代码；
  - 并种：多个源代码映射到同一个目标代码。
  - 缺映射、去向不在新版清单、版号重复等都会让整笔发布**事务回滚**，继续按原名录供数。
- **退役过渡版**（`POST /versions/{版号}/retire`）：把不再需要的过渡名录撤出日常取数链。
  - 仅受理**既非首版、也非现行版**、且**没有有效鉴定登记在该版**的版本；
  - 退役成功后：原版清单、发布时的代码去向、鉴定档案全部**留档，仍按原接口可查**；
  - 日常供数改走**跨过退役版的直接映射**（前驱 → 后继两段合成一段）；
  - 失败（首版/现行版/已有退役/仍有有效鉴定）时，供数转换与鉴定登记**照旧**；
  - 重提已退役版号、向已退役版登记，均返回 409 并明确告知“已退役”。
- **查询结论**（`GET /specimens/{标本号}/resolution`）：

| 状态 | 含义 | `resolved_species_code` |
|---|---|---|
| `unique`（候选唯一） | 所有有效鉴定逐版转换后的交集只有 1 个代码，可入库 | 给出该代码 |
| `review`（待复核） | 交集有多个代码 | `null` |
| `conflict`（鉴定冲突） | 交集为空 | `null` |
| `unidentified`（未鉴定） | 没有任何有效鉴定（从未登记或均已撤回） | `null` |

响应同时返回现行版号、每份有效鉴定的**逐版转换链**和候选代码，便于人工复核。
退役后转换链中不再出现被退役版本，直接由前驱指向后继。

## 二、一致性设计（换版期间仍可登记/撤回）

- SQLite 开启 **WAL 模式**：写事务（发布新版、退役、登记）进行时，读请求不被阻塞。
- 每个查询在**单条连接、单个只读事务**内完成全部读取；SQLite 快照隔离保证
  本次查询看到的始终是同一已提交状态，响应字段 `snapshot_consistent=true`。
- 发布新版、退役、登记、撤回均使用 `BEGIN IMMEDIATE` 显式事务 + `busy_timeout=30s`，
  写者之间排队等待；失败执行 `ROLLBACK`，旧版指针与供数链不变。
- 鉴定记录只存登记时的版本与代码，查询时沿**日常供数链**逐段套用映射转换，
  因此**任何历史版本的鉴定都能追溯到现行版**，退役前后的结论逐份保持一致。
- **供数链与留档分离**：
  - `code_mappings` 保存发布时的原始去向，**只增不改**，退役也不会删改；
  - `serving_mappings` 是日常取数实际使用的映射：未退役相邻版之间等同发布映射；
    某版退役时，把“前驱→该版→后继”两段**合成为“前驱→后继”一段**，随后删去
    与该版相连的两段。连续退役相邻旧版时，合成会逐次传递，始终直达现行版。
- **登记与退役的先后语义**（二者都是 `BEGIN IMMEDIATE` 写事务，串行化）：
  - 登记先提交且仍有效 → 退役检查看到有效鉴定，拒绝退役；
  - 退役先提交 → 登记检查看到 `retired_at`，拒绝向该版登记。
  两者不会同时成功，查询也只会看到退役前或退役后的**完整**状态。

## 三、目录结构

```
app/
  database.py    # SQLite 连接、WAL/外键/建表、旧库增量迁移（retired_at/serving_mappings）
  schemas.py     # 请求/响应模型与状态枚举
  services.py    # 换版事务、退役事务、登记/撤回、逐版转换与交集解析
  main.py        # FastAPI 路由（11 个端点）
scripts/
  seed.py        # 幂等写入 v1 初始名录（容器启动自动执行）
  demo.py        # HTTP 端到端演示：拆种/并种/撤回/换版失败/退役
tests/
  test_api.py          # 9 组接口测试，覆盖全部状态分支、回滚与退役
  test_retirement.py   # 退役专项：资格、留档、跨版供数、连续/逆序退役、并发
  test_concurrency.py  # 快照隔离与写锁等待的直连验证
docker/entrypoint.sh
Dockerfile
docker-compose.yml
```

## 四、启动方式

### Docker Compose（推荐交付方式）

```bash
docker compose up --build
# 服务: http://localhost:8000  文档: http://localhost:8000/docs
```

容器启动时自动建库并写入 v1 种子名录（5 个树种代码），数据保存在命名卷
`det-data` 中，重复启动不会重复播种。

### 本地 Python 运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
DET_DB_PATH=./data/det.db python -m scripts.seed
DET_DB_PATH=./data/det.db uvicorn app.main:app --reload
```

### 一键演示（服务启动后另开终端）

```bash
python scripts/demo.py          # 拆种、并种、撤回、换版失败、退役，内置断言
python -m pytest tests/test_api.py -q   # 接口自动化测试
python tests/test_retirement.py         # 退役专项场景（含并发先后语义）
python tests/test_concurrency.py        # 快照隔离直连验证
```

## 五、在 /docs 手工演示拆种、并种、撤回、退役

种子数据 v1 含代码：`PIN01 PIN02 QRC01 QRC02 CUN01`。

1. **登记鉴定**：`POST /identifications`
   - 标本 `T1` → v1 的 `PIN01`（后面演示拆种）
   - 标本 `T2` 分别登记 v1 的 `QRC01` 和 `QRC02`（后面演示并种消解冲突）
2. **查询**：`GET /specimens/T1/resolution` → `unique`，入库代码 `PIN01`；
   `T2` 两份鉴定无交集 → `conflict`。
3. **发布 v2**（`POST /versions`，体现拆种与并种）：

```json
{
  "version_code": "v2",
  "name": "第二版",
  "species": [
    {"species_code": "PIN01A", "scientific_name": "Pinus massoniana A"},
    {"species_code": "PIN01B", "scientific_name": "Pinus massoniana B"},
    {"species_code": "PIN02",  "scientific_name": "Pinus tabuliformis"},
    {"species_code": "QRC99",  "scientific_name": "Quercus sl"},
    {"species_code": "CUN01",  "scientific_name": "Cunninghamia lanceolata"}
  ],
  "mappings": {
    "PIN01": ["PIN01A", "PIN01B"],
    "PIN02": ["PIN02"],
    "QRC01": ["QRC99"],
    "QRC02": ["QRC99"],
    "CUN01": ["CUN01"]
  }
}
```

4. **再查询**：
   - `T1`：`PIN01` 拆成两个候选 → `review`，候选 `PIN01A/PIN01B`，不可入库；
   - `T2`：两条链并到 `QRC99`，交集唯一 → 由 `conflict` 变 `unique`，入库 `QRC99`。
5. **撤回**：`POST /identifications/{id}/withdraw` 撤回 `T1` 的鉴定后再查 →
   `unidentified`；给 `T1` 用现行版代码重新登记（如 v2 的 `PIN02`）→ 恢复 `unique`。
6. **换版失败**：再发一个 v2' 但 `mappings` 漏掉任一前版代码 → 返回 400，
   `GET /versions/current` 仍是 `v2`，查询结论完全不变。

## 五点五、在 /docs 演示退役（承接上面的 v3 之后）

1. **造一个过渡版与后继版**（以当前现行版清单做恒等换版即可）：
   - `POST /versions` 发布 `vT`（过渡版）和 `vN`（新现行版），映射均取
     `{代码: [该代码]}`；
   - 任选标本 `GET /specimens/{标本号}/resolution` 记下当前结论。
2. **登记阻止退役**：`POST /identifications` 向 `vT` 登记一份 → 再
   `POST /versions/vT/retire` → 409 “仍有 1 份有效鉴定登记在该版”。
   此时查询结论、既有转换链完全不变。
3. **撤回后再退役**：`POST /identifications/{id}/withdraw` → 再
   `POST /versions/vT/retire` → 200，返回 `retired_at` 与 `serving_from_version`/
   `serving_to_version`。
4. **原资料留档**（仍在原接口）：
   - `GET /versions/vT/species` → 原版物种清单；
   - `GET /versions/vT/mappings/outgoing` → 发布时的原始去向；
   - `GET /identifications?specimen_no=...` → 该版鉴定档案。
5. **供数改走直接映射**：`GET /versions/{前驱版}/mappings/serving` → 变为
   `前驱 → vN` 一段（跨过 `vT`）；再查该标本，`chain` 中不再出现 `vT`，
   但每份鉴定的 `candidates_at_current` 与标本结论与退役前一致。
6. **重提已退役版**：再次 `POST /versions` 用 `vT` 版号 → 409 “已退役”；
   向 `vT` 登记 → 409 “不能向该版登记鉴定”。
7. **连续退役**：再发布两个过渡版重复上述步骤，连续退役相邻旧版，结论仍逐份一致。

## 六、主要端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/versions/bootstrap` | 初始化首个名录（无需映射） |
| POST | `/versions` | 发布新版（校验全部前版代码的去向，原子事务） |
| POST | `/versions/{v}/retire` | 按版号退役过渡版（非首版、非现行版、无有效鉴定；原子事务） |
| GET | `/versions` / `/versions/current` | 版本列表（含 `retired_at`）/ 现行版号 |
| GET | `/versions/{v}/species` | 某版物种清单（退役后仍可查） |
| GET | `/versions/{v}/mappings/outgoing` | 某版发布时的原始去向（退役后仍可查） |
| GET | `/versions/{v}/mappings/serving` | 某版在日常供数链上的直接去向（跨过退役版） |
| POST | `/identifications` | 登记鉴定（拒绝向已退役版登记） |
| GET | `/identifications?specimen_no=&status=` | 鉴定列表/过滤（含退役版档案） |
| POST | `/identifications/{id}/withdraw` | 按编号撤回 |
| GET | `/specimens/{no}/resolution` | 现行版号 + 候选交集结论 + 转换链 |

## 七、验证情况

- `pytest tests/test_api.py`：9 组全部通过，覆盖四种结论状态、拆种/并种、
  多跳转换（v1→v2→v3）、撤回与重复撤回、非法登记、发布失败回滚、版号冲突、
  退役资格/留档/供数改道/退役后登记拒绝、并发写入。
- `tests/test_retirement.py`（独立运行）：77 项断言，覆盖首版/现行版/不存在/
  已退役拒绝、有有效鉴定拒绝、撤回后允许、原资料留档、跨退役版直接映射、
  连续退役（正序与逆序）、登记与退役并发先后语义、退役前后查询单快照完整。
- `tests/test_concurrency.py`：直连验证读事务快照不被并发换版破坏、写锁排队等待。
- `scripts/demo.py`：对真实 uvicorn 进程跑完整 HTTP 流程（含退役），全部断言通过。

## 八、已知限制 / 未完成项

- 版本链为**线性**模型（每版最多一个后继、一个父版），不支持分支名录；
  映射只在相邻版本间维护，跨多版由供数链逐段（退役时合成）推导。
- 退役不可逆（`retired_at` 一旦写入即终态），重提已退役版号会被拒绝；
  如需恢复同内容应使用新的版号。
- 退役仅撤出供数链，不删除任何数据；`retired_at`、原始 `code_mappings`、
  物种清单与鉴定记录长期保留，尚无物理清理/归档导出接口。
- 撤回为软删除，未提供鉴定修改接口（业务上以“撤回 + 重新登记”代替）。
- 未做鉴权/多租户与分页；名录、鉴定体量按单机 SQLite 规模设计。
- 当前环境没有 Docker 守护进程，compose 文件已通过 YAML/语法校验，
  但未能在本机实际 `docker compose up` 构建运行（本地已用等价 uvicorn 流程验证）。
