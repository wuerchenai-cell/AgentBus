# 发布流程与商业交付条件

来源：Codex 项目维护与官方分发资料；2026-10-03。

## 可重现的准备流程

新增总体计划或文件接口时，额外核验：指定评估回执完整性、不可变历史、指针 CAS、旧任务快照、原始目标续接、清单不匹配拒绝成功，以及直接发送/执行器收尾/最终汇总各路径。在实际成员会话测试分页读取、写入与回执；内部夹具通过不代替厂商执行。更新工作流后核对五个登记内容及项目程序摘要，文件更新不等于所有新聊天已原生加载。

1. 在源目录执行 `python -B -X utf8 tools/check_offline.py`，记录实际测试数、跳过原因和退出码。可选第三方凭据探测由测试入口替换；不要在CI放模型密钥。
2. 执行 `tools/export_source.py` 导出白名单源码。校验ZIP清单所有SHA256，解压到全新目录，重新跑同一个测试入口以及 `entry.py --help` / `member catalog`。固定ZIP时间戳允许同一输入按字节重现。
3. Windows隔离构建环境安装 `requirements-build.txt`，运行 `tools/build_release.py`。依赖下载是环境准备成本，不是模型请求。源码许可证或依赖许可资料不完整时不称商业放行。
4. 验证单文件EXE的CLI/MCP、分享清单、安装/检查/移除/重装、GUI和实际成员任务→回执。记录本机OS、Python、版本和程序摘要。
5. 在目标客户的干净电脑验安装、系统策略/签名和首次接入；长时协作另做持续运行、重启恢复、资源/费用测量。源测试通过不能替代这些验证。
6. 发布时提供摘要、变更说明、已知边界、回滚包及支持入口。公开源码与 Windows 二进制分别确定发行范围。

`.github/workflows/offline-tests.yml`只执行Windows无模型回归，actions按提交固定，仓库令牌仅contents:read；不签名、不创建Release、不上传用户数据。配置写好和本机同入口通过，不等于GitHub服务器已经运行成功。

## 公开源码与 Windows 版本更新

从[项目 Releases](https://github.com/wuerchenai-cell/AgentBus/releases)获取发布包。标有“源码预览”的发行用于学习、试用文件工作流与提交反馈。Windows 正式发行使用 `vX.Y.Z` 标签，并随附 `AgentBus-win-x64-green.zip` 与校验清单。

公开 GitHub 仓库采用 Apache-2.0 发布可授权的 AgentBus 自有源码。公开源码使用白名单导出，以同一份受测源码作为发布输入。不要把实际工程、构建环境或二进制提交到 Git。Windows 便携包在第三方分发权和发行范围确认后，作为 GitHub Release 附件提供。

首次准备公开构建时，在 `agentbus/release_updates.py` 的 `GITHUB_RELEASE_REPOSITORY` 填发布者控制的 `owner/repo`。同一次交付使用 `agentbus/__init__.py` 中的版本号，Git 标签写成 `v主版本.次版本.修订版本`（例如 `v1.2.0`）。每次重新发布要使用新版本号，不能用同一个标签更换包。已安装的旧程序若尚未包含更新检查，需要先手动安装一次支持该功能的版本。

1. 按上方流程核验源码与 Windows 便携包。单独执行项目主机门禁，验证正式 EXE 的实际启动和更新后登记。
2. 用与构建源码匹配的标签创建 GitHub Release 草稿，上传固定名称的 `AgentBus-win-x64-green.zip`，以及包含 ZIP 和 EXE SHA256 的校验清单。对照产品版本、源码提交、发行说明和包内的启动文件。
3. 正式发布时标记为 **Latest**。测试包标为 **Pre-release**，仅发源码且无 Windows 便携包的版本不应替代已有 Windows 最新版本。发布后用旧版客户端实查一次，确认会提示新版，并能打开正确的发布页。
4. 紧急修正时发布更高版本并保留前一个可信版本供手动回退。更新提醒只接受更高的正式版本，用户可在核对后手动恢复原包，并刷新工程和曾安装的用户级工作流登记。

用户可在 **帮助 → 检查更新…** 手动查询。启用自动检查后，程序打开时及长开期间可在后台检查，自动请求最多每天一次；关掉后仍可手动查。检查只请求发布仓库的公开元数据。离线、限流或服务错误时保留当前使用状态；仅有草稿、预发行版、同版、旧版或缺少 Windows 包时不提示安装。发现新版本时由用户打开发布页、核对包并手动安装。

更新检查匿名读取最近 20 条公开发行记录，按版本号选择最高的正式 Windows 发行。源码预览、测试版或缺少便携包的发行会被跳过。[GitHub Releases API](https://docs.github.com/en/rest/releases/releases#list-releases)。维护者可在 GitHub 发布页设定 Latest，或使用 [GitHub CLI 发布选项](https://cli.github.com/manual/gh_release_create)。

## 当前商业交付差距

|条件|当前状态|放行需要的证据|
|---|---|---|
|AgentBus源码许可/发行权|Owner已选Apache-2.0；第三方权利另核|自有代码授权范围与贡献来源记录，不能给第三方素材自动改许可|
|第三方SDK与内嵌二进制|已保留已收集许可证/NOTICE，传递依赖未完整审计|完整组件清单、版本、许可义务及原始文本覆盖|
|Windows发行者身份|现有包未取得正式可信签名|组织身份、受信任证书或签名服务、时间戳和客户策略验收|
|客户干净机器|目前同机换目录测试|另一台干净Windows机器全流程与文档可操作性|
|长时无人运行|本机短任务/有界恢复已有证据|选定成员数小时压力、故障、恢复及用量实测|
|多用户企业部署|当前同账户、可信本地成员协作|若要共享服务，需独立鉴权/租户隔离/权限和审计存储设计|
|支持与更新|公开版本元数据检查、用户确认后人工换包|在线回验真实发布仓库、支持责任及可信分发|

GitHub明确说明公开代码和授予开源许可是不同事情；没有许可证不会自动授予使用/修改/分发权。[GitHub许可证说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)

程序摘要用于内容一致性检查，不能证明发行者身份。Microsoft建议使用受信任签名提供者，并提供Artifact Signing/SignTool路径。本项目不通过降低Windows安全策略来替代正式分发工作。[Windows签名文档](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/code-signing-for-smart-app-control)

## 超期任务退役与明确的新工作续接（2026-11-16）

`recover` 仍按原任务预算恢复。已超期且确认执行者停止时，管理员可用
`retire --workspace <工程> --agent <原成员> --id <原任务ID> --claim-epoch <原代次> --owner-confirmed-inactive --reason <停止依据与断点>`
退役该认领。它保留原目标、期限、预算和检查点，仅撤销旧票据并将记录放入 blocked；不判定成功、不重排、不唤醒队列，也不替用户证明执行者确实停止。

只有用户明确开启新工作单元时，当前组长才使用
`tree continue --workspace <工程> --leaf <原章节或叶ID> --agent <组长> --id <原任务ID> --claim-epoch <原代次> --goal <明确的新目标> --request-id <固定请求ID>`。
它保留原叶与原审核，新建有关联的新作业及待认领叶；新执行者需要新票据与独立审核。同请求重试不重复建叶，改变目标须使用新的请求ID。普通“继续”不应自动绕过原任务期限调用此入口。

这两个命令的回归与冻结程序验证不代替桌面交互、厂商认证或长期无人协作验收。
