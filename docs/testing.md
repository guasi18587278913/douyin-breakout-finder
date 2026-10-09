# 测试范围

本版已完成本机验收：68 项自动检查、11 项浏览器检查和 JavaScript 类型检查通过。远程验收待 GitHub workflow 权限授权后执行；没有把本机测试描述为远程通过。

## 已有自动检查

- 核心算法、预算、分页、缓存、失败请求与重试边界。
- Key 不落盘、不返回页面状态；本地 Host、Origin 与会话验证。
- 弱结构输入（缺字段、错误类型、无效枚举）不会开始收费查询。
- 没有 USD 单位或不匹配接口的报价不能自动启用。
- 安装、更新、备份、本地修改保护、数据目录独立。
- 浏览器真实操作：自然语言输入 → 一次方向追问 → Key 弹窗；一键免费示例 → 报告及 CSV；桌面与手机布局。

## 没有宣称的测试

- 没有在 GitHub 上传真实 TikHub Key，没有执行付费研究。
- 没有在 Codex、Claude Code、WorkBuddy、DeepSeek Harness 的真实客户端登录运行整个会话。
- 没有调用 DeepSeek、MiniMax 等真实模型做语义质量比较。
- 已通过官方文档的 curl 方式匿名读取四个接口报价；解析器严格校验接口身份。网络或格式异常时停止，不要求用户手填单价。

“安装路径与协议兼容”不代表“每个客户端/模型都已通过实测”。确定性数值与报告格式不依赖宿主模型；语义理解、关键词覆盖与深度拆解仍受模型和证据影响。

## 研究报告修复

新增宿主接入、付费前握手、多轮语义复核、正文证据登记和完整研究报告校验。已验证迟到/重复/串单响应、无宿主不收费、正文伪报拒绝、并发证据保存、离线补写后网页自动刷新。合成宿主闭环产生有具体内容的完整报告；真实缓存修复保持原账本不变，缺播放数据继续标阶段稿。

## 网页自动分析接续

新增 Codex / Claude Code / CodeBuddy 三种 CLI 协议适配，WorkBuddy 的自动入口对应可调用的 CodeBuddy CLI，不假设桌面端登录必然等于 CLI 登录。自动执行器沿用自己的登录位置，不传入 TikHub Key，也不读取其他客户端的凭据。

本次验证包含：真实子进程协议的合成替身、无原宿主接单时完成分析、付费查询前握手失败保护、取消与超时、无工具配置、Windows npm 启动包装解析及命令长度、缺正文不伪报完成、核验中断和写作中断后复用缓存续跑且账本不变。浏览器通过真实按钮测试三个适配器的开始、阶段稿、续跑与报告更新。Windows 解析在本机离线构造测试，不等于 Windows 实机验收。

本机 CLI 帮助已核对：Codex 0.160.1、Claude Code 2.1.287；CodeBuddy 参数依据官方文档，未在本机安装。没有调用真实模型或 TikHub 付费 API，没有完成 Claude 订阅、WorkBuddy 桌面、CodeBuddy 登录态的实机端到端验收。

接口依据：[Codex 非交互模式](https://developers.openai.com/codex/noninteractive)、[Claude Code 程序化运行](https://code.claude.com/docs/en/headless)、[CodeBuddy 无头模式](https://www.workbuddy.cn/docs/cli/headless)、[CodeBuddy 参数](https://www.workbuddy.cn/docs/cli/cli-reference)。

本次本机结果（当前工作区）：`test_analyst.py` 15 项、`test_research.py` 16 项、`test_product.py` 20 项，合计 51 项通过、0 失败；`browser_auto.py` 3 项、`browser_e2e.py` 11 项，合计 14 项通过、0 失败。上述命令、`node --check assets/wizard.js`、TypeScript 5.7.3 `tsc -p tsconfig.json` 均退出 0。未重复运行未变更核心模块的全量测试，未发布或触发远程 CI。
