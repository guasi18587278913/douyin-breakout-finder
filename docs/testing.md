# 测试范围与本轮结果

日期：2026-10-09。证据对应当前本地工作区；尚未提交、推送、发版或运行远程 CI。这里区分真实自动写作、离线协议模拟与完整新调研，不能互相替代。

## 模块检查

初次报告改版受影响模块合计 **112 项通过、0 项失败**。后续搜索覆盖与写作对照的结果见文末。命令使用 `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p <文件> -q`，最终各模块均退出 0。

| 模块 | 通过 | 范围 |
| --- | ---: | --- |
| test_skill.py | 32 | 原算法、分页、预算、缓存、失败与重试边界 |
| test_research.py | 16 | 语义复核、登记证据、报告状态与旧格式兼容 |
| test_analyst.py | 15 | 三种分析协议、握手、取消、超时和离线接续 |
| test_product.py | 20 | 输入、报价、密钥、本地服务及原安装兼容 |
| test_parallel.py | 9 | 并发上限、限速、原子预算、请求去重、串行合并、失败停止 |
| test_selection_report.py | 20 | 新格式、引用与资格、数字来源、缺证据边界、保存后完成状态 |

最后加入耗时记录和完成状态调整后，使用 `PYTHONPATH=scripts:tests python3 -m unittest test_selection_report test_research test_analyst test_product -q` 重跑对应 **71 项，退出 0**；未变更的另两个模块沿用本轮结果。

`node --check assets/wizard.js`、TypeScript `tsc -p tsconfig.json`、`git diff --check` 均退出 0。本项目向导是 JavaScript，类型检查覆盖 assets/wizard.js。未额外执行全量生产构建。

## 浏览器与成品

- `python3 tests/browser_auto.py --out artifacts/report-acceptance/browser-auto`：**3 项通过、0 失败，退出 0**。覆盖 Codex、Claude Code、WorkBuddy/CodeBuddy 的合成子进程协议、页面开始、分析中断与离线续跑。
- `python3 tests/browser_e2e.py --bundle . --out artifacts/report-acceptance/browser-e2e`：**11 项通过、0 失败，退出 0**。覆盖主题引导、Key 弹窗、合成示例、新报告、CSV、会话保护、390px 布局、刷新与密钥不泄露。
- 最终真实缓存报告额外完成 **10 项显示检查，退出 0**：完成入口、2 条正文案例和 1 条更多作品、3 张原封面、原视频/作者链接、禁用术语、桌面和手机无横向溢出、可展开播放依据、打印渲染与页面运行错误。截图、打印预览和 acceptance.json 在 `artifacts/report-acceptance/final-display/`。这是本轮成品检查，不是新增付费调研。

## 真实自动写作

`artifacts/report-acceptance/skill-generated-final/acceptance.json` 记录本机 Codex CLI 对既有 Skill 调研缓存的实际自动写作：

- 一次模型输出通过校验；正文精选 2 条作品、归纳 2 个方法；其余 1 条充分核验的合格作品放入“更多小号爆款”。
- 3 张原封面取得并内嵌；数值、日期、原视频和主页链接均由程序从既有记录生成。
- 写作约 64.08 秒，连同封面处理与渲染约 64.37 秒。
- TikHub 请求账本前后摘要一致，新增 TikHub 付费查询为 0；分析使用当前 Codex 额度。

这个耗时不包含重新搜索、相关性判断和播放核验，不能对外宣传为一轮新调研只需 64 秒。后续新调研会在 timings.json 分阶段记录准备、搜索、复核、核验、补搜、写作和成品生成耗时。

## 独立审查

跨模块代码已独立审查。作者旧页失败误伤已有充分历史、保存失败后继续发送队列、正文结尾/迁移例子夹带虚构统计三项阻塞已修复；独立纯内存定向复核 **4 项通过、0 失败**。没有把格式和已知禁写语句校验当作通用事实核查，自动报告仍需阅读质量验收。

## 尚未验证

- 未用新主题重新发起 TikHub 付费搜索，尚无本版完整新调研的速度实测。
- Claude Code、WorkBuddy/CodeBuddy 的本轮证据是本机协议模拟，未验证各自真实账户下的完整会话。
- Windows 启动包装与参数解析是离线构造测试，不等于 Windows 实机验收。
- 尚未在其他用户电脑上验收 GitHub 获取和启动全过程；不宣称所有客户端、模型或系统均已实测。

接口依据：[Codex 非交互模式](https://developers.openai.com/codex/noninteractive)、[Claude Code 程序化运行](https://code.claude.com/docs/en/headless)、[CodeBuddy 无头模式](https://www.workbuddy.cn/docs/cli/headless)、[CodeBuddy 参数](https://www.workbuddy.cn/docs/cli/cli-reference)。

## 后续：移除完成后的中间页

完成状态在同一标签直接打开报告；在线报告顶部返回“新研究”。输入页刷新不会再次跳回旧报告，未完成状态仍显示进度和续跑。报告文件本身不带本地服务导航，保持离线可用。

本次定向验证：`test_product.py` 20 项，`browser_e2e.py` 13 项，`browser_auto.py` 3 项，均退出 0、0 失败；JavaScript 类型检查、语法检查与差异检查退出 0。浏览器证据保存于 `artifacts/navigation-acceptance/`，全部使用合成数据，无真实付费查询。此前业务计算与并发模块未变更，不重复验证。

原本地验收链接额外通过 3 项真实缓存检查（直接进入报告、新研究刷新保留输入页、请求账本不变），退出 0。导航增量独立只读审查未发现阻塞问题。

## 后续：写法对照与搜索覆盖

用户确认后，用同一份现有报告、同一模型 gpt-6.1-sol（CLI报告 reasoning effort 为 none）完成4次真实改写：ASD-STE100 清晰表达原则的中文适配2次，ra-人话 固定版本2次。每次生成均退出0、结构与引用检查通过；没有择优重跑或手工润色。原始提示、结果、模型信息、耗时及来源摘要在 `artifacts/writing-comparison/`，对照页为该目录下的 index.html。

ASD 官方原文直接请求返回403，依据官方检索摘录借用清晰表达原则；未完整检验英文词表、英文词数等要求。这次没有测试仅添加一句标准名称的简短提示。ra-人话 来源版本为 `83d1783b892bbaa29a137895ff8338264b6872fa`，只读取文字，没有安装或执行外部脚本。两组方法说明长度不同，比较的是这两种实际用法，不单独归因于某条规则。助手逐段比较建议优先 ra-人话，但两组仍有未解释的抽象说法；未做真实读者测试，正式写作规则没有替换。

搜索代码取消“3条合格即停止补搜”，默认候选核验上限从8提高到50；先核验已有小号，再按每页新增情况决定翻页或换同主题词，最多补搜两轮。补搜给核验留额度，作者历史按小批获取，并根据剩余额度缩小尾段批次。原向导50次请求/0.50美元预留上限及粉丝、倍数、样本标准均未提高或放宽。

新增 `test_coverage.py` 12项，覆盖第9/10条候选继续处理、3条合格后仍补搜、重复和大号页面停止翻页、费用头寸、过新作品不占核验位、身份/渠道/缓存统计变化使旧结果失效等。模拟30个不同作者的场景，在50次请求内得到7条合格；模拟已有对照统计的场景，10条候选均能核验。两项仅用于证明机制，不代表真实搜索命中率。

本轮命令与结果：

- `PYTHONPATH=scripts:tests python3 -m unittest test_coverage test_parallel test_skill test_research test_analyst test_product test_selection_report -q`：当时123项通过、0失败，退出0。
- 最终缓存与渠道边界修正、增加1项测试后，`PYTHONPATH=scripts:tests python3 -m unittest test_coverage test_parallel test_skill -q`：53项通过、0失败，退出0；其余未变更检查不重复。当前模块用例总数124。
- `browser_auto.py`：3项通过；`browser_e2e.py`：13项通过；对照页显示：9项通过。均退出0、0失败。
- 独立审查发现的目标引用复用问题已修复；身份、统计、低余额及50次边界定向复核4项通过、退出0，最终小增量只读复核无剩余阻塞。
- `git diff --check` 退出0。前端向导代码未变更，沿用本轮已通过的类型检查。对照页已做桌面、390px、切换和报告链接验证。

以上均无新增 TikHub 付费查询；真实新主题命中率与完整耗时仍待用户从页面开始一轮研究后测量。未提交、推送、发版或运行远程CI。

## 用户否定两组改写后的调整

用户判定 ASD 与 ra-人话 都不如原始样稿，本轮已撤回实验推荐，不接入两种改写方法。报告继续使用原始样稿，允许在相同案例、相同证据下沿用准确原句；不同案例仍须引用本轮证据。调整篇幅指引为通常2–4短段，允许一到两段引言，并保留生活场景的迁移例子。既有统计、引用、资格与正文证据校验没有放宽。

`python3 -m unittest discover -s tests -p test_selection_report.py -q`：20项通过、0失败，退出0；`git diff --check`退出0。实际自动生成使用现有Skill缓存，一次输出通过校验，约58秒，2条正文案例、3条合格作品；没有新增TikHub查询，原请求账本保持一致。产物和提示材料保存在 `artifacts/approved-sample-check/generated/`。用户随后确认“可以的”，本份报告口吻验收通过；其他新主题的实际效果仍需完整调研验证。

## 尚未完成的体验项

此前“自动记住 Key”尚未实现；用户已随后确认私密目录保存方案，目前已实现并进入假密钥与页面验收，结果见后续记录。

## 交付准备检查

当前工作区已复制到一个新的临时源码目录，副本不包含运行报告、缓存和私密文件。使用 `python3 -S` 禁用第三方 site-packages 后，免费示例可以生成当前选题报告，本地页面能独立启动且资源齐全；缺少分析助手时在收费查询前停止。`artifacts/delivery-acceptance/check_fresh_source.py` 4项通过、0失败，退出0，源文件摘要另存于同目录。该检查是本机全新源码副本，不能描述成另一台电脑实测。

本机分析入口就绪检查：Codex 可用；Claude Code 已安装但未通过订阅登录方式检查；CodeBuddy 未安装。因此本轮无法在本机声称后两种客户端的真实账户闭环已通过。

README 已移除不存在的 PDF 手册链接，并纠正“缺少视频正文只能生成阶段稿”的过时说明。本地文档链接检查与源码语法解析通过；未修改现有 CI 配置。

## 可选 Key 保存验收

用户已授权私密目录保存与“忘记 Key”，功能已实现。API只有显式 `remember=true` 才写入；页面默认勾选并显示保存说明。记住的文件位于独立数据目录的 private 子目录，源码目录内禁用持久保存。POSIX目录0700、文件0600，读取检查所有者、权限、链接和大小；Windows使用当前用户DPAPI加密。保存为原子替换，忘记仅移除本应用保存的 Key，不删除研究记录。结束会话清理内存副本，不删除已记住的文件。

- `python3 -m unittest discover -s tests -p test_credentials.py -v`：17项通过、0失败，Windows原生DPAPI 1项在macOS跳过，退出0。
- `PYTHONPATH=scripts:tests python3 -m unittest test_product test_analyst test_selection_report -q`：55项通过、0失败，退出0。
- `python3 tests/browser_credentials.py artifacts/credentials-acceptance/browser`：6项通过，覆盖保存、重启、忘记、仅本轮使用和手机布局。
- `browser_auto.py` 3项、`browser_e2e.py` 13项通过。浏览器检查均退出0、0失败，全部用假Key。
- 新版64个源文件的隔离副本检查4项通过，禁用第三方Python库后启动和免费报告仍正常。
- JavaScript类型检查、语法检查与差异检查退出0。
- 独立安全审查未发现密钥泄漏、同源绕过或存储阻塞；假Key定向复核14项通过、0失败，退出0。Windows部分只有源码审查，仍等待Windows原生检查。

保护机制依据：[Microsoft DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)。不使用允许机器上所有用户解密的 LOCAL_MACHINE 标志。

已打开一次“AI自媒体”的真实调研输入页，费用和次数已获用户授权。此处记录完成时尚未输入真实Key，未发起付费查询；真实结果将另行记录，不与上述模拟检查混用。

## 首次 GitHub CI 与安装入口排查

提交70683d1已发布。首次CI运行37943972620中，Ubuntu/Python3.9与3.12核心检查通过，公开下载及类型检查通过；Windows失败主要来自测试用默认系统编码读取UTF-8报告，并暴露了并发替换凭证文件的问题。Windows DPAPI读写本身已通过真实平台测试。浏览器验收失败于旧“Key永不落盘”用例默认勾选了新保存选项。

修复中统一测试的UTF-8读取，原会话内用例显式取消记住。凭证存储增加按路径线程锁和跨进程系统锁，持锁后初始化标记；“忘记”也清理本模块因崩溃留下的、确认属于本用户的普通临时密钥文件。新增空目录并发首次保存和写后崩溃清理场景。本地凭证测试20项通过、1项Windows专用跳过，退出0；独立安装副本上的浏览器验收13项通过、退出0。独立复核确认锁初始化和崩溃清理阻塞已关闭，额外5项通过、退出0。最终跨系统结论以修复提交的CI为准。

用户反馈旧报告仍出现后，核对“启动小号爆款查找技能”聊天和PID52722的启动命令，确认它从 `.agents/skills/douyin-breakout-finder` 启动。该副本安装记录31项中30项与历史提交db66f87e406e相同，缺少新版报告模块、标准样稿和AGENTS.md，仍使用research-v1、8条候选上限及旧中间页。首页journal资源虽已单独更新，但核心未同步。独立审查确认5项首页资源与新版完全一致，6项本地修改的意图已收进仓库，无需混用旧CSS/JS/HTML。

启动输出现增加source_dir与report_format，后续验收必须核对实际安装入口。最近旧版生成的Skill报告数据保留5条合格、1条有限样本，49次原查询；新版修复只复用其数据与已登记证据，不新增TikHub查询，不覆盖原稿。
