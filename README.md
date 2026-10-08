# 抖音小号爆款发现

**把方向说清楚，找到值得复看的小号内容。** 一个可以从 GitHub 安装的 Skill，附本地向导、简短方向引导和可复算报告。

> v0.2.0 公测版。统计由程序计算，完整报告由启动 Skill 的宿主智能体阅读证据后撰写，不需要新增分析 API Key。真实 TikHub 查询需要你自己的 Key 和已确认预算；免费示例全是合成数据。当前不会承诺每个模型和客户端都已实测。

## 发给智能体，这一句就够了

> 请从 https://github.com/guasi18587278913/douyin-breakout-finder 安装“抖音小号爆款发现”Skill，按 README 选择适合你自己的安装目录，然后打开本地研究向导。不要让我把 API Key 发在聊天里。

装好后说：**“使用小号爆款发现，帮我研究【你的内容方向】。”**

## 只做三件事

1. **准备 Key。** 打开 [TikHub 后台](https://user.tikhub.io/)，注册或登录账号，在 API Key 管理中创建并复制自己的 Key。后台名称可能随版本变化，找不到时查 [官方文档](https://docs.tikhub.io/)。需要正式查询时，在 TikHub 自行选择充值金额。**不需要 GitHub Key。**
2. **说出想找什么。** 用一句话描述内容方向，选一个想参考的类型，点击“生成报告”。Key 在小弹窗里填写，价格和研究参数由程序处理。
3. **打开报告。** 智能体继续复核、阅读内容与写作。报告包含主题发现、分类、逐条拆解、选题建议和对照证据。不要在只打开网页时结束智能体会话；未完成分析或证据不足时，只显示阶段稿。Key 在本轮结束后清除。

不想先付费？点击“先体验免费示例”，一分钟走完固定 FDE 合成数据流程。它不代表你的主题已有真实调研结果。

## 给负责安装的智能体

要求：Python **3.9+**、系统 curl、能执行本地程序、用户能打开该机器的浏览器。不需要第三方 Python 运行依赖。

下载此仓库后，在仓库目录运行：

```sh
python3 install.py --client codex
# 或 --client claude / dsh / workbuddy / agents
```

向导启动方式（切到安装器返回的 `installed` 目录）：

```sh
python3 scripts/start.py
```

启动后必须按 [宿主研究协议](references/agent-workflow.md) 使用返回的 `bridge_dir`，持续等待、分析与提交，直到 `finished`。只是打开网页然后结束回复，无法完成完整报告；程序会在收费查询前检查助手是否接入。

Windows 用 `python` 代替 `python3`。如果没有自动弹窗，把输出的 `url` 交给用户打开。页面只有当前电脑可访问；不要放到公网，也不要公开会话链接。

| 客户端 | 安装方式 | 本版边界 |
| --- | --- | --- |
| Codex | `--client codex` → `~/.agents/skills/` | 遵循官方技能目录；安装与脚本已有本机测试；远程验收尚待完成，真实客户端会话另见测试记录 |
| Claude Code | `--client claude` → `~/.claude/skills/` | 遵循官方技能目录；同上 |
| DeepSeek Harness（DSH） | `--client dsh` → `$DSH_HOME/skills` 或 `~/.dsh/skills/` | 遵循官方文件系统技能加载文档；未声称实际模型推理通过 |
| WorkBuddy | `--client workbuddy` → 通用 `~/.agents/skills/` | 需在客户端技能管理里添加/导入；自动发现取决于版本，尚未做真实客户端实测 |
| 其他本地 Agent | `--client agents` 或 `--target /技能目录/douyin-breakout-finder` | 需要支持 SKILL.md 和 Python 执行 |
| 纯网页聊天 | 无法运行本地版本 | 仅把链接发进不支持执行代码的聊天工具不会完成安装 |

官方依据：[Codex](https://developers.openai.com/codex/skills/) · [Claude Code](https://code.claude.com/docs/en/skills) · [DSH](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/skill/skill-filesystem/README.md) · [WorkBuddy](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)。

## 后续更新

直接对智能体说：“检查这个 Skill 是否有更新，有的话帮我更新。”安装器从 **GitHub 最新正式 Release** 下载，不要求 GitHub Key：

```sh
python3 install.py --client codex --check
python3 install.py --client codex --update
```

修改 `--client` 或提供原来的 `--target`。默认不会偷偷替换代码。有本地修改会停止；成功更新保留旧版备份。研究数据默认在 `~/.local/share/douyin-breakout-finder/`，不会随技能更新被覆盖。公开仓库不等于购买权限系统；本版未提供付费授权、防复制或客户管理。

## 报告怎样保证可信

- 默认：粉丝 ≤ 1 万，目标播放 ≥ 同作者邻近作品中位数的 10 倍，最多核验 8 条候选。
- 对照目标 10 条，少于 10 条但不少于 6 条单列“有限样本”；缺数据或历史覆盖不全不算“已核验”。
- 关键词只给初步提示，全部候选交由宿主逐条语义复核；样本不足时可在原预算内补搜两轮。未决条目在报告里明确列出。
- 中位数、倍数、费用与资格由程序计算；模型只提交分析文字和现有证据引用，不能覆盖统计。
- 正文分析需引用本地登记并校验的逐字稿或观看记录。没有正文时，不能填写真实口播开头和叙事结构，也不能宣称完成全文拆解。
- 默认不额外查询评论。高级分析可以复用当前宿主，**没有实测就不能承诺 DeepSeek、MiniMax 等所有模型语义表现一致**。

## 费用和 Key

TikHub 是数据接口服务，GitHub 是代码发布平台。程序只向固定的 `https://api.tikhub.io` 数据接口发送 Key，拒绝重定向转发 Key。程序自动匿名读取官方接口报价，并校验接口身份与 USD/次计费契约；无需用户手填单价。每轮固定最多 50 次数据请求和 US$0.50 本地预留额度；你在页面主动点击“生成报告”后才开始本轮付费查询。请求失败也保守计入预留金额；这个金额不是 TikHub 实际账单。价格变化可能影响实际账单，应以后台为准。

页面只监听 `127.0.0.1`；Key 保留在进程内存，任务结束或结束会话后清除。关闭网页后，会话闲置 30 分钟自动结束。不要将 Key 发到聊天、仓库、截图或问题反馈里。

## 运行证据与说明书

- [远程验收流程](.github/workflows/acceptance.yml)：全新 GitHub Actions 机器下载公开代码，测试安装、计算、预算、Key 边界和浏览器向导。
- [测试与兼容边界](docs/testing.md)：如实区分合成数据测试、客户端实测与真实付费查询。
- [PDF 简明手册](docs/quickstart.pdf)：使用远程测试截图制作；发布后可直接下载。
- [版本发布与更新](https://github.com/guasi18587278913/douyin-breakout-finder/releases)。

## 开发验证

```sh
python3 -m unittest discover -s tests -v
node --check assets/wizard.js
npx --yes --package typescript@5.7.3 tsc -p tsconfig.json
```

浏览器验收由 Playwright 执行；只在测试环境安装它，使用者运行 Skill 不需要安装。完整研究方法见 [method.md](references/method.md)。
