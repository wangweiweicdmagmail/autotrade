# 图表股票代码水印

源码：[chart-symbol-watermark.pine](../indicators/chart-symbol-watermark.pine)。这是独立的 Pine v6 显示工具，不是策略模型，不登记策略模型注册表、不输出交易接口，也不接入执行模型。

## 显示规则

- 每个图表宫格正中央固定显示当前标的代码，例如 `QQQ`、`NVDA`、`NAS100`。
- 使用 `position.middle_center` 的单格 table，位置相对于图表视口；拖动、缩放或产生新 K 线不会把文字推走。
- 代码来自当前图表的 `syminfo.ticker`。切换标的时 TradingView 重新计算脚本，文字随之更新，不锁定首次加载的股票。
- 不按日期或交易时段重置，因此跨交易日、盘前盘后都保留；在图表有 K 线且已加载本指标时显示。
- 默认字号 72、透明度 80（约 20% 不透明度）、粗体，文字颜色自动跟随图表主题。底色、边框透明。可单独调整字号、透明度、颜色以及是否显示交易所前缀。
- 不请求其他周期或外部行情，也不读取或操作券商订单。

## 使用

1. 在 TradingView 的 Pine 编辑器中新建一个指标，粘贴完整源码，保存并添加到图表。
2. 分别添加到需要显示水印的宫格；保存对应 TV 布局，以便下次打开继续使用。脚本本身不会自动安装到其他宫格。
3. 若八宫格中的代码太长，调小字号；不同字号不会自动适配宫格宽度。

## 验证与边界

核对股票切换、跨交易日及视口固定逻辑；新增文件检查空白与链接。尚未在 TradingView 编译和图表验证，不声称已修改当前浏览器里的指标或布局。

TradingView 官方参考：[Tables](https://www.tradingview.com/pine-script-docs/visuals/tables/)（视口锚点、字号）、[Text and shapes](https://www.tradingview.com/pine-script-docs/visuals/text-and-shapes/)（粗体）、[Visuals overview](https://www.tradingview.com/pine-script-docs/visuals/overview/)（`behind_chart` 与显示层级）。
