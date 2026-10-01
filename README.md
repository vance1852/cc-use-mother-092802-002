# 管理体育与音乐活动的品牌权益履约服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。在基础层之上，**品牌权益履约模块**把多份赞助协议附件中的露出位置、独家品类、供货数量、现场活动窗口、物料交付和费用承诺收敛为可追溯计划：

- **协议版本化**：协议按版本组织（草稿/已签署/已被取代），场次、权益、费用、物料均按版本快照；只有草稿可改，变更时从已签署版本复制出新草稿，旧版本签署后归档，已签署内容与附件不可变。
- **签署/变更前冲突识别**：按「时间窗口重叠 × 区域重叠 × 资源重叠」扫描，覆盖排他品类（跨合作方）、露出位置、媒体资产投放位、现场活动分区，冲突以结构化 409 返回，拦截签署。
- **现场证据与验收**：证据按内容哈希去重（同协议、同版本、同权益下重复上传同一凭证只返回既有证据，绝不产生第二次验收）；验收支持全额、部分（比例或数量折算）、退回补交、争议和驳回。
- **争议只冻结相关费用**：争议挂起对应权益的费用，其他已完成权益继续结算；法务裁决 `upheld`（权益未成立）或 `rejected`（驳回，恢复全额可付）。
- **续期不继承旧场次**：续期协议只记录 `renewal_of` 链接，场次与权益必须重新规划。
- **付款可解释**：每笔付款必须按费用行全额分配，每条分配引用有效验收（accepted/partial）与被该验收覆盖的证据；金额不得超过按履约比例计算的可付额度，冻结费用拒绝付款。
- **三权分立**：法务（legal）管协议、版本、签署、争议裁决；市场（marketing/reviewer/operator）管计划条目、物料与证据验收；财务（finance）管付款与结算；审计（auditor）只读。市场视角看不到金额，结算报表仅法务/财务/审计可见。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由、离线验收和品牌权益履约领域；
- `tests/`：基础规则、事务边界、接口路由、品牌权益场景和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.acceptance
PYTHONPATH=src python3 -m beverage_ops_foundation.brand_acceptance
```

验收命令在临时 SQLite 数据库中演练完整链路，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。品牌验收覆盖：冲突签署被拦截、改区后签署、重复凭证抑制、部分验收、争议只冻结相关费用、付款证据链解释和续期不复制旧场次。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，所有写操作要求幂等的 `request_id`。

### 品牌接口（`/brand` 前缀）

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/brand/agreements` | legal | 建协议（可带 `renewal_of_agreement_id`） |
| GET | `/brand/agreements` | 全部 | 协议列表 |
| GET | `/brand/agreements/<id>` | 全部 | 当前版本完整计划（金额仅 legal/finance/auditor 可见） |
| POST | `/brand/agreements/<id>/revisions` | legal | 从已签署版本开新草稿（复制计划，物料重置） |
| POST | `/brand/agreements/<id>/sign` | legal | 校验并签署；冲突返回 409 + 冲突明细 |
| GET | `/brand/agreements/<id>/conflicts` | 全部 | 签署前预览冲突 |
| POST | `/brand/sessions` `/brand/entitlements` `/brand/fees` `/brand/materials` | legal/marketing | 草稿计划写入 |
| POST | `/brand/attachments` | legal/marketing | 草稿附件（哈希去重） |
| DELETE | `/brand/plan-items?agreement_id=&kind=&item_id=` | legal | 移除草稿条目 |
| POST | `/brand/evidence` | marketing/reviewer | 现场证据（内容哈希去重） |
| POST | `/brand/acceptances` | marketing/reviewer | accepted/partial/returned/disputed/rejected |
| POST | `/brand/material-deliveries` `/brand/material-confirmations` | marketing/reviewer | 物料交付与确认 |
| POST | `/brand/dispute-resolutions` | legal | upheld/rejected 裁决 |
| GET | `/brand/agreements/<id>/entitlements/<eid>/tracking` | 全部 | 证据、验收、争议追踪 |
| GET | `/brand/agreements/<id>/settlement` | legal/finance/auditor | 每费用行履约比例、冻结、可付/已付 |
| POST | `/brand/payments` | finance | 付款（必须全额分配到费用+验收+证据） |
| GET | `/brand/payments/<id>` | 全部 | 解释每笔钱对应的权益、验收与证据（金额受限） |

服务重启后 SQLite 中的业务状态和审计链继续保留。
