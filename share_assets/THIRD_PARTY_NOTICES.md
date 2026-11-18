# 随包第三方组件记录

来源：AgentBus 构建依赖与上游官方仓库；初始归档：2026-10-09；固定版本证据更新：2026-11-16。

这个分发包保留已收集的原始许可证文本。`licenses/provenance.json` 逐项记录组件、版本、来源及 SHA256，不记录构建电脑路径。

- Codex Python SDK 与随包 CLI 0.157.1：本机 wheel metadata 声明 Apache-2.0；wheel 未携带 LICENSE 正文。归档官方 OpenAI Codex rust-v0.157.1 对应固定提交的 LICENSE 与 NOTICE，原文与既有归档逐字节一致，保留其对 Ratatui 的声明。冻结程序所带codex.exe与0.157.1安装制品逐字节核对，并仅运行--version确认版本。
- Python 3.12.14：实际 WebView2 构建发行版的 LICENSE.txt（包含该发行版提供的声明）。
- Tcl/Tk 8.6：旧界面兼容模块使用的构建发行版 license.terms。
- pydantic、pydantic-core、annotated-types、typing-extensions、typing-inspection、packaging：原安装 wheel 提供的许可证。
- PyInstaller：原发行版 COPYING.txt，包含其 bootloader 分发例外。
- ripgrep 15.2.0：随 Codex runtime 打包的 rg.exe与安装制品逐字节一致，仅运行--version得到版本与提交前缀，再与官方15.2.0标签提交核对；MIT与UNLICENSE来源已固定到该提交。
- Ratatui 0.30.2：版本来自Codex 0.157.1固定提交的Cargo.lock；crates.io原始crate摘要与锁文件一致，LICENSE与crate记录的固定源码提交及既有归档逐字节一致。锁文件只证明发布源码的依赖选择，不证明全部crate都进入本二进制，也不构成完整传递清单。
- pywebview 6.2.1、cffi 2.1.1、bottle 0.13.4、pycparser 3.1：实际安装 wheel 的原始许可证。
- pythonnet 3.0.5、clr-loader 0.2.8：对应上游版本标签的 MIT LICENSE；wheel 未携带正文。
- proxy-tools 0.1.0：安装源码与官方PyPI 0.1.0 sdist逐字节一致；源码头部写BSD，而sdist的setup.py/PKG-INFO及安装元数据均写MIT。sdist没有LICENSE文件。现有BSD正文另从上游固定提交取得且与既有归档逐字节一致，不能冒称来自sdist；冲突仍未解决，不擅自择一或声明商业放行。
- Microsoft.Web.WebView2 SDK 1.0.3856.49：官方 NuGet 包的 LICENSE.txt。随 pywebview 打包的 Core/WinForms/Win-x64 Loader DLL 与该包逐字节 SHA256 一致；系统 Evergreen Runtime 由微软独立安装，不随本包分发。

## 冻结程序中观察到的构建与内嵌组件

2026-11-16按正式EXE的PYZ模块索引核对，altgraph、pefile、PyInstaller、pywin32-ctypes和setuptools均有同名模块出现，不能仅因它们也用于构建就当作未随包。现补入altgraph0.17.5、pefile2024.8.26、pywin32-ctypes0.2.3、setuptools84.0.0自身原始许可，并保留PyInstaller原有COPYING。

setuptools的公开wheel RECORD另列有内嵌组件许可：autocommand、backports.tarfile、importlib_metadata、jaraco.text/context/functools、more-itertools、packaging（含Apache/BSD双文本）、platformdirs、tomli、wheel与zipp。相关原文及config、validate-pyproject的两份NOTICE完整归档；后者包含MPL2.0文本，未删改。共补20份，当前清单45份。版本来自对应安装元数据；不能仅凭PYZ名称出现就把所有内嵌包/版本或完整链接链当成已证明。许可证文本归档也不替代各项再分发义务核对。

本记录仅描述归档到的材料，不声称已完成 Codex 二进制全部传递依赖的完整许可证审计。官方许可证来源链接见 provenance 清单。各软件账号、商业模型订阅与桌面应用本体不随本包提供。

## 公开源码图标

`agentbus/member_identity_icons.py` 为成员生成统一的中性机器人头像。项目配色和显示名称的缩写用于区分成员，各接入项仍使用原有显示名称。

`agentbus/assets/agentbus.ico` 的三点连线图形由 Codex 于 2026-11-18 为 AgentBus 公开源码候选单独绘制，使用几何形状和纯色，生成时仅使用 Python 标准库。应用图标和成员头像作为 AgentBus 项目素材由根目录 `LICENSE` 下的 Apache-2.0 涵盖。应用图标含 16、24、32、48、64、128、256 像素七种尺寸。

`agentbus/assets/agentbus.ico` 的 SHA-256 为 `94eeef68d57e6e8d19af38e3ead99fb762f701d63e0622cb803509d9466ca794`。
