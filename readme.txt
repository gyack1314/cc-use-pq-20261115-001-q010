秋窗客流分析账本
运行环境：Python 3.11，已按 requirements.txt 预装依赖。
启动：python -m autumn_ledger serve --database autumn-ledger.sqlite3
接口说明：服务启动后访问 /docs。
合成业务演练：python -B -m autumn_ledger demo
测试：python -B -m unittest discover -s tests -v
构建：python -B -m autumn_ledger.check_source
数据完整性：python -B -m autumn_ledger check --database autumn-ledger.sqlite3
金额单位为人民币分；订单修订为全量快照，退款为累计值。
出游日期是供应商给出的目的地本地日期；事件时间必须含时区。
取消不计出游人数，未退收入仍计入净额。缺报不等于零需求。
本地演练使用虚构数据，分析仅描述所选来源样本。

版本化指标口径（autumn_ledger/metrics）：
- 指标以受限算术 DSL 登记（JSON 节点树，白名单算子 const/measure/metric/exposure/neg/add/sub/mul/div/ratio），
  系统只解释这些节点，不执行任何 Python 表达式或任意代码。
- 可引用已有基础度量（net_cents、travelers 等）、其他已定义指标的指定版本，以及明确选择的暴露分母
  （orders/travelers/城市日期 city_days/完整覆盖城市日期 complete_city_days）。
- 生命周期：PUT /metrics 登记草稿 -> POST /metric-trials 在同一数据版本与现行口径并行试算 ->
  POST /metrics/{key}/activate 审核启用。只有启用版本能进入报告与对外发布。
- 启用前校验引用版本存在、依赖均已启用、无循环依赖、无单位冲突，并在错误详情中给出具体依赖路径。
- 指标值带互斥状态：ok / zero_denominator（零分母）/ not_applicable（该分组无法定位分母）/
  missing（缺报，不能当零）。比例 ratio 的总计从汇总后的分子分母重算，绝不平均各组比例。
- 修改已启用指标=登记新版本草稿；停用前检查下游依赖，被引用时拒绝停用并返回 dependents。
- 报告创建时冻结整套指标依赖版本（result.metric_freeze，含 digest）；
  GET /editions/{id}/metric-evidence 导出可读定义证据，导出包含 metric-definitions.json 与 metric-values.csv。
  之后重命名或停用口径都不影响历史重放（verify 使用报告内冻结版本，不回读当前注册表）。
