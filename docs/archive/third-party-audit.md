> ⚠️ **已归档（2026-11-25）**：本文是历史设计，可能和现行冲突。
> 现行设计以 [DESIGN.md](../DESIGN.md) 为准；差异见 [设计审计](../design-audit-2026-11-25.md)。

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
