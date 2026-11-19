# AgentBus

**把分散在不同 AI 对话里的工作，变成一棵可以分工、验收和接手的项目任务树。**

AgentBus 是一个本地优先的多 Agent 协作工作台。任务、进度、交付文件和接手说明保存在工程目录里。更换 Agent 或重开聊天后，接手者可以从该任务的记录继续。

[下载与版本说明](https://github.com/wuerchenai-cell/AgentBus/releases) · [发布与更新](RELEASE.md) · [接入方式](ECOSYSTEM.md) · [安全反馈](SECURITY.md)

## 适合做什么

- **多人分工**：把开发、测试、文档拆成有依赖的任务，由组长统一分派，或在工作台指定成员。
- **跨对话接手**：原成员退出、换工具或用完额度时，新成员按任务编号读取当前进度、已有产物和下一步。
- **交付和验收**：从任务树查看谁在执行、哪些任务受阻，以及已提交的文件。启用独立监督后，由另一名成员验收交付。

## Windows 工作台怎么用

1. 打开 `AgentBus.exe`，选择工程目录、参与成员和组长。也可以把业务产出放在单独的目录。
2. 让组长编制任务树，或导入已有任务树。选择组长分派或手动分派。
3. 把工作台生成的接入指令发给成员。成员认领任务、提交产物和回执，工作台显示进度与接手信息。

文件协作是通用入口。成员在自己的聊天中接手。需要自动发起新回合时，须另行验证该成员支持的 CLI、ACP 或 SDK 接入。

**Windows 10/11 x64** 从 [Releases](https://github.com/wuerchenai-cell/AgentBus/releases/latest) 下载 `AgentBus-win-x64-green.zip`，解压后打开 `AgentBus.exe`。桌面端需要 Microsoft WebView2。使用 Codex SDK 接入时，请使用单独安装的官方 Codex CLI。支持更新检查的桌面版默认在打开时检查，发现新版后点击「前往下载」即可打开 GitHub 发布页。可在 **帮助 → 检查更新…** 手动检查或关闭自动检查。

## 从源码体验任务树

需要 Python 3.12 或 3.14。下面会创建一个独立的临时工程并预览模板，无需 Agent 账号。

```powershell
$project = Join-Path ([IO.Path]::GetTempPath()) ('AgentBus-demo-' + [Guid]::NewGuid().ToString('N'))
python -B -X utf8 entry.py init --workspace $project --members codex,workbuddy --coordinator codex
python -B -X utf8 entry.py tree import --workspace $project --template software-project --preview
```

在源码根目录执行 `python -B -X utf8 tools/check_offline.py --scope portable` 可跑离线回归。Windows 桌面依赖 WebView2，冻结构建使用 Python 3.12，详情见[发布与更新](RELEASE.md)。桌面、厂商和干净电脑验收单独记录。

## 开源许可

AgentBus 自有源码采用 Apache-2.0。第三方组件与品牌素材适用各自的许可。参见[许可范围](../../SOURCE_LICENSE_STATUS.md)。
