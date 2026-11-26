# 工具代理：语义感知路由、转换预算与引用恢复

`unknown`（暂不能确定）与 `absent`（没有找到）是两个独立业务区分；旧式适配端可能把二者都投影成 `empty`。本项目提供一个仅依赖 Python 3.11 标准库的工具代理，按申请用途选择“工具端点 + 多级适配转换”路径，并保留申请、实际尝试、预算、许可、原表示、交付表示和引用的独立事实。

## 快速验证

```bash
# 读取并清点演练资料（读取不可信 JSONL 时使用隔离模式）
python3 -I scenario_reader.py examples

# 全部单元测试与本地合成工具端到端验证
python3 -Im unittest discover -v

# 本地合成工具演示
python3 proxy_cli.py demo

# 运行已有 JSONL 演练，输出其中 inspect 结果
python3 proxy_cli.py scenario examples/compatible-summary-not-citation.jsonl
python3 proxy_cli.py scenario examples/retired-edge-with-live-tool.jsonl

# 直接按 JSONL 中的申请或引用查询
python3 proxy_cli.py query --events examples/citation-immutable-late-reply.jsonl --application cite-drill
python3 proxy_cli.py query --events examples/citation-immutable-late-reply.jsonl --citation cit-1
```

也可以把事件写入持久化 JSONL 日志后恢复查询：

```bash
python3 proxy_cli.py scenario examples/retired-edge-with-live-tool.jsonl --store /tmp/tool-proxy-state
python3 proxy_cli.py query --store /tmp/tool-proxy-state --application q
```

## 核心业务规则

### 1. 用途决定语义要求

- `purpose: "summary"` / `"browse"` / `"overview"`：仅浏览摘要，可丢失未要求保留的区分。
- `purpose: "formal"` / `"proofread"` / `"citation"` / `"正式校对"` / `"引用"`：正式校对/引用，自动要求保留 `absent` 与 `unknown`。
- 未显式给 `purpose` 时，若 `need` 同时包含 `absent` 和 `unknown`，按正式用途处理；否则按申请中明确的 `need` 判断。

路径规划会检查**每一段** adapter 的 `keeps`，不是只看最终表示。若状态值对应的区分未被某段保留，投影结果会变成 `status: "empty"`，并在 `semantic_report.lost_distinctions` 中记录丢失项。

### 2. 找不到路径时区分三类原因

`query_application(id)` 的 `blockers[].kind` 为：

- `semantic`：存在端点/图路径，但某段不能保留必需区分；明细含 `missing` 和 `first_blocking_edges`。
- `budget`：存在语义全合格路径，但最低成本（或重规划时“已耗用 + 新路径成本”）超过预算；明细含 `cheapest_cost`、`over_by`。
- `endpoint`：没有接受该请求表示的工具、数据范围不支持、不在可用窗口，或没有到目标表示的转换端点路径。

多级路径会组合工具提供方和多个 adapter 服务，转换成本求和；工具调用成本默认不计入 README 所定义的转换预算。

### 3. 预算、贵路径储备与便宜相容替代

申请中的 `budget` 是本次请求的转换成本上限。受理 `accepted` 后记录：

```json
{
  "limit": 4,
  "planned_cost": 4,
  "hold_cost": 4,
  "actual_cost": 0,
  "released_unused_hold": 0,
  "settled": false
}
```

若先只有较贵路径并已预留，之后注册更便宜且输入/输出、语义保持均相容的路径，代理会在内部重规划到便宜路径，但：

- 不改变 `attempt` 身份；
- 不要求客户重新发申请；
- 终态未确认前不提前释放预留；
- 拿到执行端终态并完成处理/确认无路径后，才把未使用预留记为 `released_unused_hold`；
- 已完成转换的 `actual_cost` 始终保留；重规划新路径时必须满足 `actual_cost + new_cost <= budget`。

adapter 注册/升级只会使仍在等待、未交付或未完成的相关申请内部重规划；已交付申请和其他客户的事实不变。

### 4. 执行端 unknown 不是业务 unknown

需要区分两种“未知”：

- **业务未知**：`reply.state = "done"` 且 `body.status = "unknown"`。这是一份有效的原工具答复，会生成不可变原表示；`business_answer.owner_follow_up_recommended = true`，可据此再问资料负责人。
- **执行端终态缺失**：`reply.state = "unknown"` 且没有终态 body。此时代理不创建结果表示、不投影、不交付，并保持 `budget.settled = false` 和原 `attempt_id`。重启后继续等待真实终态回执。

`reply.state = "never"` 被视为执行端给出的“未执行/无结果”终态，不创建表示，并可结算未使用预算。

### 5. 原表示与客户端表示是独立使用事实

- `original_representations[]`：工具终态 body 的规范化哈希、业务状态、支持区分、实际 attempt 和所有回执。
- `deliveries[]`：经过选定 adapter 路径投影后的客户端表示、路径版本、成本、语义完整性和合法性状态。
- `usage_facts[]`：分别记录交付事实和向下游提交引用的事实。
- `citation_ids[]`：申请关联的引用；引用自身保存提交时的不可变 `snapshot`。

引用查询入口：

```python
proxy.query_citation("cit-1")
proxy.query_entry({"citation": "downstream-ref"})
```

返回：

- `historical_snapshot_unchanged`：已提交快照是否仍保持原样；
- `late_receipts_do_not_rewrite_snapshot`：固定为该代理的事实原则；
- `new_citation_allowed` / `new_formal_proofread_citation_allowed`：当前是否还能用于新引用；
- `eligibility_checks.reasons`：如 `conflicting_terminal_receipts_unresolved`、`legality...`、`outside_application_data_scope`。

迟到的不同 `done` 回执会被保留为新的表示版本和 `receipt_conflicts`，但不会覆盖首份/已引用快照；冲突未解除前，相关表示不能用于新的正式引用。相同 body 的重复回执按幂等处理，不重复生成交付事实。

### 6. 许可撤销与已完成未下发转换

`withdraw` 会版本化撤销 adapter：

- **尚未开始的段**：标记 `revoked` 并重新规划；若已有合法完成前缀，则优先从前缀输出继续；前缀成为死路时，也可从原表示重选，但已耗用成本仍占预算。
- **已完成且未下发的段**：
  - 若转换回执中的授权、scope、哈希和 adapter 版本证明有效，可继续下发；
  - 若无法证明合法，段进入 `quarantined`，交付进入 `held_for_review`，不下发该转换表示；
  - 原始工具结果保留在 `retained_original_result` / `delivery.fallback_body`，并给出 `completed_transform_legality_unproven` 复核原因。
- **已下发或已引用的表示**：历史使用事实不被重写；但若撤销后无法补证，该交付表示会被阻止用于新引用。原工具表示的引用资格独立于 adapter 许可撤销。

可证明合法性的 adapter 声明可包含 `lawful_basis`、`authorization: {"grant_id": "..."}` 或显式 `provable: true`。代理会为本地合成转换生成带哈希的 `proof`。没有任何授权/合法依据声明时，系统采取保守策略：撤销后视为无法证明合法。

### 7. 重启恢复

所有输入消息均为可重放事件。`JsonlEventStore` 以 append-only JSONL 持久化；重启时使用：

```python
recovered = ToolProxy.recover_from_store(JsonlEventStore("/tmp/tool-proxy-state"))
```

恢复内容包括：

- 申请时的 `data_scope`；
- 客户/租户 `customer`；
- 实际 `attempt_id`、全部回执和终态状态；
- 原表示、交付、引用和使用事实；
- 实际成本、预留和结算状态；
- adapter/tool 定义版本与撤销历史。

JSONL 中遇到 `restart` 事件时，场景运行器会从已保留事件重放至该点。

## 查询入口

### Python API

```python
from tool_proxy import ToolProxy

proxy = ToolProxy()
proxy.handle({"at": 0, "kind": "tool", "record": {...}})
proxy.handle({"at": 1, "kind": "query", "record": {...}})
# ...accepted / reply / cite / withdraw...

app = proxy.query_application("my-request")
citation = proxy.query_citation("my-citation")
by_downstream_ref = proxy.query_entry({"ref": "wiki:T-1"})
```

常用输出位置：

- `state`：`planned`、`accepted`、`endpoint_unknown`、`blocked`、`delivered`、`held_for_review`、`receipt_conflict` 等；
- `blockers`：语义/预算/端点诊断；
- `active_plan.hops`：实际选择的工具与 adapter 版本；
- `formal_proofread.can_use_delivered_representation`；
- `formal_proofread.can_use_original_for_formal_proofreading`；
- `business_answer.owner_follow_up_recommended`；
- `recovery`：申请 scope、客户、实际 attempt 和预算恢复状态。

### CLI

```bash
python3 proxy_cli.py scenario <file.jsonl> [--store DIR] [--compact]
python3 proxy_cli.py query --events <file.jsonl> --application ID
python3 proxy_cli.py query --store DIR --citation ID
python3 proxy_cli.py demo
```

## 扩展的消息类型

基础类型保持兼容：`adapter`、`tool`、`query`、`accepted`、`reply`、`withdraw`、`restart`、`inspect`。

为引用与转换回执新增并登记了：

- `cite`：登记向下游系统提交的引用快照；
- `deliver`：手动下发一个已完成的客户端表示；
- `transform`：外部/手动转换回执扩展点。

`tool` 声明额外支持 `request`/`output`（同时兼容 `source`/`result`）、`data_scope`/`scopes`、`window`/`available_window`、`usage_window`。`query` 额外支持 `purpose`、`customer`、`data_scope`、`auto_transform`、`auto_deliver`。

## 本地合成验证

`tool_proxy/synthetic.py` 提供内存中的 `SyntheticTool` 和 `SyntheticToolGateway`，测试不访问网络。`test_tool_proxy.py` 覆盖：

- 两个自带示例的关键业务结论；
- 多级路径与超预算直连替代；
- 端点 scope/window 诊断；
- 贵路径到便宜相容路径切换且 attempt 不变；
- adapter 升级只影响等待中的客户；
- 未开始段撤销重规划、已完成前缀成本；
- 可证明/不可证明转换的撤销后果；
- 引用快照不可变、冲突迟到回执阻止新引用；
- 持久化重启恢复 scope、客户、attempt 和预算 hold；
- 本地合成工具 gateway 端到端回执。

## 目录结构

```text
scenario_reader.py          # 原有 JSONL 格式读取/校验
proxy_cli.py                # 根目录便捷 CLI（兼容 python -I）
tool_proxy/
  semantics.py              # absent/unknown/empty 投影与用途规则
  registry.py               # 版本化工具与 adapter 声明、scope/window/撤销
  planner.py                # 语义感知路径搜索与 semantic/budget/endpoint 诊断
  service.py                # 申请、attempt、回执、预算、许可、引用、恢复
  store.py                  # append-only JSONL 持久化
  scenario.py               # 演练文件运行器
  synthetic.py              # 本地合成工具
  demo.py / cli.py          # 演示与命令行入口
examples/                   # 基础演练及新增引用/许可撤销演练
test_tool_proxy.py          # 领域规则与端到端测试
```
