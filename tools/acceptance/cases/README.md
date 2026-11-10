# 验收 case 格式

每行一个 JSON，UTF-8，无 BOM。`runner.py` 只认这些字段：

- `case_id`：唯一，格式 `S0x-nn`；
- `suite`：`S01` / `S02` / `S03` / `S04` / `S05` / `S12` / `S13` / `S14`；`F` 系列由 `tools/selftest_proactive_pacing.py` 执行；
- `status`：`planned`（锁定阶段）、`active`（第一轮实现后）、`disabled`；
- `title`：人读的一句话；
- `fixture`：`memory` / `learning` / `memory_learning` / `learning_dispatch` / `budget`；
- `given`：构造输入，不得出现生产库路径；
- `expect`：返回值、数据库状态、允许/禁止动作；
- `forbidden`：明确禁止的副作用；
- `planned_reason`：为什么现在还是 planned。

锁定阶段只验证 schema、路径安全与 case_id 唯一；`--allow-planned` 只表示“允许未实现”，**不把 planned 计为 passed**。

第一轮把 S01/S02/S03/S04 改为 `active`、第二轮把 S05 改为 `active`、第三轮把 S13 改为 `active`、第四轮把 S14 改为 `active` 时，必须同时实现 runner 里的执行器；只改 status 不改执行器会直接报错。
