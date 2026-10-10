# 抖音小号爆款发现

**把方向说清楚，找到值得复看的小号内容。**

网站主入口：[小号爆款 · 创作手记](https://breakout.zmtgzt.com)。受邀请的用户注册后即可输入主题、查看后台进度、阅读报告和历史记录，无需安装软件或填写服务 Key。网站使用独立账号与私有报告，普通用户只能访问自己的研究。

[网站部署与维护](docs/web-deployment.md) · [网站范围与验收](docs/web-release-plan.md)

以下为可选的本地 Skill 入口：把 GitHub 仓库链接交给自己的 Agent，获取代码并启动本地向导。网站和 Skill 共用搜索、资格核验与报告标准；两者的账户和密钥不会混用。

> v0.2.0 公测版。统计由程序计算，报告由本机智能体的自动执行器或宿主阅读现有证据后撰写，不需要新增分析 API Key。真实 TikHub 查询需要你自己的 Key 和已确认预算；免费示例全是合成数据。当前不会承诺每个模型和客户端都已实测。

## 发给智能体，这一句就够了

> 请获取 https://github.com/guasi18587278913/douyin-breakout-finder 的源码，读取仓库里的 SKILL.md，检查本机运行条件，然后按你所在的客户端启动“小号爆款查找”。已有目录先检查本地改动，不要直接覆盖。API Key 由我在本机网页填写。

之后可以说：**“使用小号爆款发现，帮我研究【你的内容方向】。”**

## 只做三件事

1. **准备 Key。** 打开 [TikHub 后台](https://user.tikhub.io/)，注册或登录账号，在 API Key 管理中创建并复制自己的 Key。后台名称可能随版本变化，找不到时查 [官方文档](https://docs.tikhub.io/)。需要正式查询时，在 TikHub 自行选择充值金额。**不需要 GitHub Key。**
2. **说出想找什么。** 用一句话描述内容方向，选一个想参考的类型，点击“生成报告”。Key 在小弹窗里填写，价格和研究参数由程序处理。
3. **打开报告。** 后台自动调用本机智能体进行复核与写作，进度和报告都在当前网页显示。分析中断可在网页继续，复用已有数据。新版默认交付有依据的选题解读；数据或分析中断时显示阶段稿。只有标题与简介时不编造视频口播和镜头。勾选“记住 Key”后，下次无需再填；也可以在新研究页面点击“忘记 Key”。

需要先体验时，可让 Agent 运行离线合成示例。它不代表你的主题已有真实调研结果。

## 给负责获取代码与启动的 Agent

要求：Python **3.9+**、系统 curl、能执行本地程序，用户能打开该机器的浏览器。核心流程不需要第三方 Python 依赖。不要求用户另装一个安装包。

通过 Git 获取完整仓库，或使用所在客户端支持的 GitHub Skill 获取方式。保留仓库的 scripts、assets、references 目录，并先读取 SKILL.md。已有目录不能被静默覆盖。在仓库目录启动：

```sh
python3 scripts/start.py --assistant codex
# 按当前客户端选择 codex / claude / workbuddy；无法自动分析时使用 host 兼容模式
```

自动模式通过本机已安装并登录的 CLI 按需启动分析，不依赖原聊天仍在运行。启动助手应指定自己的客户端；`auto` 优先识别启动环境与安装来源，无法确定时提示指定入口，不静默换用另一个账户。分析会使用所选客户端的账户额度，和 TikHub 的 US$0.50 数据预留上限分开。Codex 自动模式使用 ChatGPT 登录；Claude Code 使用订阅登录；不接受环境里的分析 API Key。

只有宿主能运行 Skill、没有独立 CLI 时，使用 `--assistant host`，由启动助手按 [宿主研究协议](references/agent-workflow.md) 持续接单直到完成。该兼容模式仍要求宿主保持运行，不能承诺聊天结束后网页也能独立分析。

Windows 用 `python` 代替 `python3`。如果没有自动弹窗，把输出的 `url` 交给用户打开。页面只有当前电脑可访问；不要放到公网，也不要公开会话链接。

| 当前客户端 | 启动参数 | 本版边界 |
| --- | --- | --- |
| Codex | `--assistant codex` | 调用本机已登录的 Codex CLI |
| Claude Code | `--assistant claude` | 调用本机已登录的 Claude Code CLI |
| WorkBuddy | `--assistant workbuddy` | 需要可调用且已登录的 CodeBuddy CLI |
| DSH 或其他可执行本地程序的 Agent | `--assistant host` | 由当前 Agent 按宿主协议持续处理分析 |
| 纯网页聊天、无法执行本地程序 | 不支持 | 不能仅凭仓库链接运行本地服务 |

## 报告交付与生成流程

- [交付标准](references/report-standard.md)：口吻、精选案例、图卡字段、证据与完成判定。
- [标准样稿](references/report-example.json)：方法讲述、案例解释、跨场景例子与行动建议。
- [生成流程](docs/report-flow.md)：从用户输入到校验与图文报告的各环节。
- [交付验收](docs/delivery-verification.md)：核对实际安装与运行入口，防止只更新源码副本。

后台会实际读取标准和样稿，输出 `selection-report-v1`，再由程序统一计算与排版。标题、简介及已核验数据可支持选题分析；涉及口播和镜头的陈述必须有正文证据。其余合格作品放在更多小号爆款；待核验线索和内部诊断只保留在本地记录，不对用户展示。

旧版研究报告仍可读取，不会被自动改成新版完成状态。`install.py` 暂留作旧流程兼容工具，不是当前推荐入口。

## 更新代码

让 Agent 先查看当前 Git 提交、本地改动和上游差异；获得用户的更新指令后再更新。报告与密钥不放进仓库，不因代码更新重置已有运行记录。

## 费用和 Key

TikHub 是数据接口服务，GitHub 是代码发布平台。程序只向固定的 `https://api.tikhub.io` 数据接口发送 Key，拒绝重定向转发 Key。程序自动匿名读取官方接口报价，并校验接口身份与 USD/次计费契约；无需用户手填单价。每轮固定最多 50 次数据请求和 US$0.50 本地预留额度；你在页面主动点击“生成报告”后才开始本轮付费查询。请求失败也保守计入预留金额；这个金额不是 TikHub 实际账单。价格变化可能影响实际账单，应以后台为准。

页面只监听 `127.0.0.1`。勾选“在这台电脑记住 Key”后，密钥保存在独立数据目录的 private 子目录，默认是 `~/.local/share/douyin-breakout-finder/private/`，不在 Skill 源码或研究报告中。macOS/Linux 使用仅当前账户可读写的文件权限；Windows 使用当前用户的 DPAPI 加密。点击“忘记 Key”会移除保存文件。未勾选时仅在本轮内存中使用。

每轮结束会清理内存副本，已记住的 Key 保留供下一次使用。关闭网页后，会话闲置 30 分钟自动结束。不要将 Key 发到聊天、仓库、截图或问题反馈里。

## 运行证据与说明书

- [远程验收流程](.github/workflows/acceptance.yml)：全新 GitHub Actions 机器下载公开代码，测试安装、计算、预算、Key 边界和浏览器向导。
- [测试与兼容边界](docs/testing.md)：如实区分合成数据测试、客户端实测与真实付费查询。
- [使用与启动说明](SKILL.md)：由接收方的 Agent 获取完整仓库后按当前客户端启动。
- [版本发布与更新](https://github.com/guasi18587278913/douyin-breakout-finder/releases)。

## 开发验证

```sh
python3 -m unittest discover -s tests -v
node --check assets/wizard.js
npx --yes --package typescript@5.7.3 tsc -p tsconfig.json
```

浏览器验收由 Playwright 执行；只在测试环境安装它，使用者运行 Skill 不需要安装。完整研究方法见 [method.md](references/method.md)。

自动分析协议与浏览器回归（合成执行器，零真实模型/数据请求）：

```sh
python3 -m unittest discover -s tests -p test_analyst.py -v
python3 tests/browser_auto.py --out artifacts/auto-acceptance
```

自动执行器当前只分析已登记材料，不自动下载或转录视频。标题、简介与充分核验的数据可以生成选题解读；视频口播和镜头分析仍需真实正文证据，可通过宿主协议补充。CLI 适配的离线测试通过不代表已验证每种操作系统、订阅账户和真实模型，详见测试记录。
