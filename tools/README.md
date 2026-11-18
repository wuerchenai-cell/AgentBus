# AgentBus 工具链目录（tools/）登记与使用说明

本目录汇总 AgentBus 项目在开发、验证、体检、审计与构建过程中使用的所有辅助脚本与工具。
根据 §10.11 AUDIT-360-TOOLS-QUALITY-GATE 质量门禁要求，所有存量及新增工具均须在此登记并在单元测试中被覆盖。

## 工具脚本清单

| 工具脚本 | 用途与定位 | 门禁与关联规范 |
|---|---|---|
| `archived_binary_inventory.py` | 历史过时二进制文件扫描与清点 | §9.1 历史二进制物理封存防线 |
| `artifact_drift_attribution.py` | 交付产物漂移追溯与归因审计 | §3.5 产物防篡改与不可变性 |
| `audit_mailbox_backlog.py` | 信箱待办积压与消息水位审计 | §2.2 文件总线待办队列 |
| `benchmark_canvas_interactions.py` | 无限画布前端交互性能基准测试 | §4.3 可视化画布性能保障 |
| `bridge_surface_census.py` | WebBridge 接口暴露面普查与审计 | §6.1 外部桥接面安全 |
| `broad_except_locator.py` | 宽泛异常捕获（bare except）扫描与治理 | §7.12 代码质量防线 |
| `build_dependency_audit.py` | 显式源码/构建环境的静态依赖核对；默认无子进程和账号探测 | §8.2 独立发布构建规范 |
| `build_litter_inventory.py` | 构建遗留垃圾文件排查与清理 | 产出目录卫生与边界隔离 |
| `build_release.py` | 正式单文件便携版 EXE 构建脚本 | 唯一正式执行程序发布基准 |
| `check_doc_drift.py` | 项目文档与规范漂移检测 | 文档完整性与真源核对 |
| `check_formal_sync.py` | 正式契约与分支同步状态校验 | 工作流一致性门禁 |
| `check_offline.py` | 离线隔离与无外部模型依赖断言 | 离线闭环自测防线 |
| `check_tools_quality.py` | 工具链质量门禁检查器（文档与测试覆盖） | §10.11 AUDIT-360-TOOLS-QUALITY-GATE |
| `cli_surface_check.py` | 命令行参数与接口契约一致性体检 | CLI 命令面完备性 |
| `comprehensive_audit.py` | 全局深度细致排查与 CLI/Action/文档一致性体检 | §10.11 全局只读排查防线 |
| `contrast_audit.py` | 前端界面对比度与无障碍（A11y）合规审计 | UI/UX 规范 |
| `deep_consistency_check.py` | 核心契约与文档新旧指令一致性只读检查 | §3.5 文档真实性与历史清理 |
| `deploy_formal_release.py` | [已废弃/高危警示] 历史发布脚本（直接改写 workflow 且无前置测试，严禁用于正式出厂发布） | 废弃归档与安全阻断 |
| `delivery_self_proof.py` | 交付物自证与自包含回执验证 | 证据链自闭环证明 |
| `duplicate_impl_census.py` | 重复逻辑与重复实现代码普查 | 架构精简与防冗余 |
| `evidence_backfill_index.py` | 审计回执与证据回填索引构建 | §7.13 证据链追踪 |
| `export_source.py` | 纯源码导出与打包归档工具 | 源码分发防线 |
| `find_queued.py` | 就绪未认领排队任务快速定位工具 | 调度状态检索 |
| `gen_7_21_artifacts.py` | §7.21 分层测试产物与报告生成器 | 测试分层体检体系 |
| `gen_broad_exception_triage.py` | 宽泛异常分类处理清单生成器 | 异常治理辅助 |
| `generate_build_manifest.py` | 发布构建清单生成与哈希计算 | 出厂发布物签名与防伪 |
| `handover_screen.py` | 任务交接卡片与工位看板生成 | §3.8 动态移交与接管 |
| `host_file_registration_check.py` | 宿主文件与注册表登记状态自检 | 环境配置与注册验证 |
| `inject_jsdocs.py` | 前端脚本 JSDoc 类型注解注入与补齐 | 前端代码质量 |
| `inspect_forest.py` | 任务森林层级结构与依赖链只读自检 | 任务树拓扑审查 |
| `inspect_tree.py` | 任务树实时状态、叶子及生命周期检视 | 任务状态审查 |
| `launcher_truth_check.py` | 启动器哈希与正式执行程序真源比对 | §9.1 唯一正式执行程序防线 |
| `live_codex_check.py` | 实时 Codex 进程与命名管道通信探测 | 桌面端运行状态诊断 |
| `mcp_smoke.py` | MCP 服务连接与工具通信冒烟测试 | 外部 MCP 桥接防线 |
| `public_symbol_census.py` | 公共模块导出符号与 API 界面普查 | API 稳定性保障 |
| `shared_file_blast_radius.py` | 共享文件变更爆炸半径分析工具 | 共享文件保护防线 |
| `skill_manifest_audit.py` | AgentBus Skill 清单与配置合规审计 | Skill 工作流扩展检查 |
| `smoke_frozen.py` | 正式冻结二进制 EXE 独立冒烟测试 | 出厂交付物物理验证 |
| `test_layer_budget.py` | 分层测试耗时与性能预算校验 | §7.21 秒级与分钟级测试控制 |
| `tree_capability_audit.py` | 任务所需能力与当前成员工种匹配审计 | 避免死叶与无候选人节点 |
| `tree_dedup_check.py` | 任务树节点 ID 与章节编号防重审计 | 任务树拓扑唯一性保证 |
| `tree_delivery_sweep.py` | 全项目交付产物扫描与一致性对账 | 产物终态扫描与对账 |
| `tree_refresh_bench.py` | 任务树快照刷新与投影性能基准评测 | 调度引擎吞吐量评测 |
| `tree_structure_preflight.py` | 任务树结构写入前合法性与投影键预检 | 任务树持久化刚性门禁 |
| `workbench_contract_regen.py` | 工作台契约代码自动重新生成工具 | 契约代码维护与同步 |

## 构建依赖静态核对（2026-11-16）

`python -B -X utf8 tools/build_dependency_audit.py --source-root <源码目录> [--venv <构建环境>] [--exe <正式EXE>] --json`

必须明确源码目录；不再从祖先目录自动选旧 `.build-venv`，实际 `.web-build-venv` 可直接指定。默认只读AST与指定环境元数据，既不启动EXE，也不调用doctor、账号探针或模型。缺少venv/exe就显示未测量，不会搜索账户目录。字节数为文件逻辑长度、单包硬链接去重，不代表磁盘实际分配量。

静态图包含可选函数体内import，只表示可能可达；环境中安装的组件不等于冻结程序已链接它们。报告明确不是完整SBOM或商业许可放行。仅在明确需要执行已审源码帮助命令时加 `--run-smoke`，它只调用一次 `entry.py --help`、30秒超时，不调用doctor；普通审计不需要此选项。
