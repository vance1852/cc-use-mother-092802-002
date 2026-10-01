# 管理体育与音乐活动的品牌权益履约基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

`brand_rights/` 在基础层之上建设了完整的**品牌权益履约领域**：把分散在多份附件中的协议版本、场次、区域、媒体资产、排他范围、物料交付和费用承诺收敛为可追溯计划，在签署或变更前识别时间窗与范围冲突，现场证据支持部分验收、退回补交与争议，争议只冻结相关费用，并为法务、市场、财务提供各自权限内的明细与付款解释。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/brand_rights/`：协议版本、权益计划、冲突检测、现场验收、争议冻结、费用结算与逐笔付款解释；
- `tests/`：基础规则、事务边界、接口路由、冲突引擎和端到端验收测试。

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
PYTHONPATH=src python3 -m brand_rights.acceptance
```

基础验收核对主体登记、幂等回执与审计链。品牌权益验收在临时库中串起赛车 / 足球 / 音乐节三类赞助：签署前拦截两家合作方互相冲突的啤酒排他权、部分验收按比例计付、争议只冻结相关费用而其他权益继续结算、续期不自动延续旧场次、重复上传同一凭证不产生第二次验收，最后解释每笔付款对应的有效权益与证据。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

基础服务与品牌权益服务共用同一个 SQLite 库，可分别启动在两个端口：

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m brand_rights.api     --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8081
```

健康检查使用 `GET /health`（基础服务）。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

### 角色

品牌权益在基础角色之上使用 `legal`（法务）、`marketing`（市场）、`finance`（财务）三个业务角色，`admin` 全权、`auditor` 只读全部。法务负责协议、版本、排他范围与争议裁决；市场负责权益、场次、媒体物料和现场验收；财务负责费用承诺与付款。非财务角色只能看到费用状态而看不到金额，也不能读取付款解释。

### 品牌权益接口（端口 8081）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/brand/agreements` | 建立协议（draft，可标记续期来源） |
| POST | `/brand/agreement-versions` | 为协议新增草稿版本 |
| POST | `/brand/conflicts/preview` | 签署前预检时间窗与排他范围冲突 |
| POST | `/brand/agreements/sign` | 签署版本；存在冲突返回 `409 plan_conflict` 及明细 |
| POST | `/brand/agreements/terminate` | 终止协议 |
| POST | `/brand/agreements/renew` | 以旧协议为底稿续期，只复制权益文案，场次/物料/媒体/费用不延续 |
| POST | `/brand/rights` | 在草稿版本上登记露出、排他、供货、活动窗口等权益 |
| POST | `/brand/sessions`、`/brand/sessions/status` | 安排场次、推进场次状态 |
| GET | `/brand/calendar` | 按区域 / 场地查看场次日历 |
| POST | `/brand/media-assets`、`/brand/media-assets/status` | 媒体资产登记与交付状态 |
| POST | `/brand/deliverables`、`/brand/deliverables/delivery` | 物料约定与累计交付（不超量） |
| POST | `/brand/fees` | 费用承诺（fixed / per_session / per_acceptance） |
| POST | `/brand/evidence` | 上传现场凭证；同一内容哈希全局去重 |
| POST | `/brand/acceptance` | 验收：accepted / partial（带比例）/ returned / disputed |
| POST | `/brand/disputes/resolve` | 争议裁决，按比例解冻并计付 |
| POST | `/brand/payments`、`/brand/payments/complete` | 按权益+凭证分配付款并完成 |
| GET | `/brand/payments/{id}` | 解释每笔付款对应哪些有效权益和证据 |
| GET | `/brand/agreements`、`/brand/agreements/{id}`、`/brand/evidence` | 按角色裁剪的明细查询 |

所有写接口都接受 `request_id` 实现请求幂等；同一 `request_id` 配合不同载荷会被拒绝，重复提交返回首次回执。
