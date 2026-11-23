# 第三方插件审计

| 目录 | 上游 | 许可证 | UPSTREAM | 风险 |
|---|---|---|---|---|
| `VideoZoomer_master` | https://github.com/zsgvivo/VideoZoomer | 见目录 LICENSE / Notice.txt | ✅ | 使用 eval、外联地址、调用 os.system、调用 subprocess |
| `bittorrents` | https://github.com/NightDust981989/nonebot-plugin-BitTorrents | 见上游（待核对） | ✅ | 外联地址 |
| `girls_2d` | https://github.com/zhuhiki/nonebot-plugin-2dgirls | 见目录 LICENSE | ✅ | 外联地址 |
| `gspanel` | 待人工确认 | ⚠️ 未声明 | ✅ | 外联地址 |
| `jinriyunshi` | 待人工确认 | ⚠️ 未声明 | ✅ | 外联地址 |
| `nonebot_plugin_rollpig_plus` | https://github.com/Felis2026/nonebot-plugin-rollpig-plus | 见上游（待核对） | ✅ | 外联地址 |
| `petpet` | https://github.com/MeetWq/nonebot-plugin-petpet | MIT | ✅ | 外联地址 |
| `picsearch` | 本项目自建（薄封装，调用 vendor/PicImageSearch） | 与本项目一致 | ✅ | 无 |
| `pixivrank_search` | https://github.com/HibiKier/nonebot_plugin_pixivrank_search | 见上游（待核对） | ✅ | 外联地址 |
| `steam_judger` | https://github.com/Duogeyu/nonebot_steam_judger | 见上游（待核对） | ✅ | 外联地址、调用 subprocess |

## vendor/（services 直接依赖的第三方库）

| 目录 | 上游 | 许可证 | LICENSE 文件 |
|---|---|---|---|
| `PicImageSearch` | https://github.com/kitUIN/PicImageSearch | MIT（见目录 LICENSE） | ✅ |

## 我们对上游改了什么

上面的表只回答「来源 / 许可证 / 风险」。每个插件的**行为适配点**（修了什么 bug、加了什么能力）
统一记在 [third-party-integration.md](third-party-integration.md#4-我们对上游插件改了什么)。

摘要：

- `picsearch`：本项目自建（聚合以图搜源），通过 `plugin.toml` 暴露 function 工具；
- `girls_2d` / `petpet` / `pixivrank_search` / `steam_judger` / `gspanel` / `rollpig_plus` /
  `bittorrents` / `jinriyunshi`：都做了路径、异步、异常、图片发送、命令统一等适配；
- `VideoZoomer_master`：原样内置，只做路径/依赖接入。
