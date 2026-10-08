# TikHub 直连适配

文档核对日期：2026-10-08。正式运行仍需确认接口可用性和具体单价。代码仅向 `https://api.tikhub.io` 的以下四条路径发送 API Key，不跟随重定向。

| 操作 | 方法与路径 | 参数要点 |
|---|---|---|
| search | POST `/api/v1/douyin/search/fetch_video_search_v2` | JSON 请求体；keyword、cursor、search_id、backtrace；sort_type="1"按点赞；content_type="1"视频 |
| posts | GET `/api/v1/douyin/app/v3/fetch_user_post_videos` | sec_user_id、max_cursor、count=20、sort_type=0、channel=normal/lite |
| stats | GET `/api/v1/douyin/app/v3/fetch_video_statistics` | aweme_ids 逗号分隔，最多2个 |
| comments | GET `/api/v1/douyin/app/v3/fetch_video_comments` | aweme_id、cursor=0、count=20保持默认 |

官方参考：

- 搜索：https://docs.tikhub.io/370212780e0
- 作者作品：https://docs.tikhub.io/186826223e0
- 播放统计：https://docs.tikhub.io/186826221e0
- 完整文档：https://api.tikhub.io/
- 定价与具体接口入口：https://tikhub.io/pricing
- 官方插件示例：https://github.com/TikHub/tikhub-plugin/blob/main/skills/douyin/SKILL.md

## 费用

TikHub 直连使用用户自己的 TikHub 余额，单位为 USD。接口单价可能不同，也可能有折扣。不要把曾经通过 AgentKey 观察到的 0.2 credits 写成 TikHub 的美元单价。

具体操作：在官方接口价目或用户后台确认四类接口的当前单价，然后在 init 前填写任务 JSON 的 `pricing`；可选评论暂不调用也先记录单价。按基础价/不含优惠的已确认价做保守预留即可。查不到时说明缺少单价，保持离线计划，不编造价格或核对时间。初始化后配置冻结，不能在已开始的运行中补改价格。

用户授权本轮额度后，调用命令可带 `--allow-paid`。相同任务范围、额度和报价未变时无需逐次询问。预算消耗包括搜索分页、作者分页、统计、评论和明确重试。

## 数据读取

标准成功外层是 code=200、data 为对象。还要检查 data.status_code；外层成功但内层 status_code 非零不能当作有效数据。搜索结果在 data.business_data[].data.aweme_info，翻页信息常在 data.business_config.next_page 与 backtrace。作者列表在 data.aweme_list。统计在 data.statistics_list。不要用搜索和主页里面常见的 play_count=0 代替专门统计。

只保存研究所需字段、游标、统计与评论文本。不会保存请求 Authorization，也不需要读取用户的浏览器 Cookie、其他工具配置或通讯录。接口结构变化时停止并修适配器，不猜测字段。

normal 渠道偶尔不能完整返回公开作品，可在核对文档后用 verify 的 `--channel lite`。认证/权限失败先解决凭证或访问权限。脚本遇到 HTTP 401/403 会停止，不尝试其他方式绕过。
