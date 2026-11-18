# 接入依据与外部方案取舍

来源：Codex 本机审查与官方公开资料；核对日期：2026-10-03。以下是工程选择，不表示已在本机安装或验收所有列出的产品。

## 外部项目带来的改进

|来源|可借鉴的做法|本项目采用方式|
|---|---|---|
|[MCO](https://github.com/mco-org/mco)|独立 provider 接口、明确选择团队、保留完整答复与运行状态|保留成员原始意见；接入声明和本机验收分开；按项目保存团队|
|[ACP 规范](https://agentclientprotocol.com/protocol/v1/overview)|JSON-RPC 初始化、会话创建/可选恢复、权限请求、取消与进度通知|沿用已有 ACP 适配器；按能力和实际回执验收，GUI/CLI 独立核对|
|[ACP Registry](https://github.com/agentclientprotocol/registry)|结构化产品身份、分发平台、来源、许可证与协议检查|只读推荐目录提供来源链接；不直接执行注册表给出的下载或启动命令|
|[Clutch](https://github.com/fancy1108/Clutch)|桌面监督台、首次启动向导、安装/平台状态明确|继续把任务、失败原因、接手入口和维护动作显示在 EXE 中|
|[Microsoft Agent Framework](https://github.com/microsoft/agent-framework)|把工作流和多 Agent 编排作为可扩展模块提供|借鉴边界划分；此轮保留已经实测的本地文件状态，不引入整套框架迁移|

MCO明确指出路径范围检查不等于操作系统沙箱，provider仍管理各自认证与原生权限。Clutch文档还注明部分CLI默认跳过权限提示；本项目保留用户权限，不引入该默认行为。两者都是借鉴对象，各自公开说明也不等于本项目的Windows实机验收。

## 扩展更多产品

应用内 `member catalog` 与“添加 Agent”的推荐菜单列出有日期和来源的产品。覆盖既有 Codex、WorkBuddy、Trae、ZCode、OpenCode，以及 Gemini CLI、GitHub Copilot CLI、Cursor CLI、Cline CLI、Qwen Code、Kimi CLI、Mistral Vibe、Factory Droid、Auggie、goose、Claude ACP、pi ACP。名单以当前随包目录为准，自定义产品仍可登记。

注意身份区别：Claude ACP / pi ACP 是桥接程序；CodeBuddy Code 注册表条目不是本机 WorkBuddy 桌面实例的验收；GLM 模型桥接不等于 ZCode 桌面。Kimi旧CLI与后续产品迁移也需重新核对。Windows可用性、安装/登录、权限、任务及恢复均需在目标机器分别验收。

[注册表格式](https://github.com/agentclientprotocol/registry/blob/main/FORMAT.md)提供 binary、npx、uvx 分发描述和平台字段；SHA256为可选元数据，预览渠道未做同样的运行验证。声明不能取代本机任务闭环或发行者签名。

## 新适配器的验收契约

1. 只在用户选中的当前项目运行，使用绝对工作目录与固定参数数组，禁止拼接 shell 命令。
2. 明确产品、CLI/桥接发布者、版本与平台；不以发现命令入口标记“可用”。
3. 初始化/认证/会话阶段分别记录结果，能力不支持时给出可操作失败；不自动扩大权限。
4. 同一成员一次仅运行一个 prompt；等待用程序事件，预算和取消有界。
5. 真实短任务写出可核对的产物，回执拥有新ID、`reply_to`关联原任务、按显式`result_to`投递并被收取。
6. 拒绝、超时、STOP、丢连接与恢复分别测试。`processing`需尊重原执行者，旧时间戳不是死亡证据。
7. 桌面会话与独立接口互相不冒充；真实厂商测试和模拟协议测试分别记录。

此轮推荐目录完全离线，无账号读取、安装器、自动下载或自动启用。目录内每个条目均标明本机未由目录验收；真正接入依赖项目配置及证据。
