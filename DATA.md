# 资料约定

原始资料均为本地合成。CSV 使用 UTF-8，首行是字段名。

所有时间为合成月份起点后的当地小时，读数为 Wh 的累计值。连续两个 `readings` 的 hour 是差值所覆盖的端点；其中有 reset 时不跨表底相减，缺少中间点不产生逐小时已测值。`topology` 是计量点归属的半开时段，记录按 recorded 时点可见，后补回执用 replaces 指代旧关系。`operations` 有两间活动室是否实际使用及场馆的服务组合，temperature 是室外摄氏温度。总表的正差额并非自动属于房间。地面层 MN 和南侧 MS 有独立总表。
`kits` 和 `access_windows` 约束测量资源与可访问计量点的时间。`campaigns` 的 target 指向计量点，span 是要覆盖的小时段；already_started 表示已有现场记录，planned 表示可重新安排。`observations` 给实际接入点、对应量及接线是否经现场确认。`decisions` 是值班人过去开出的处置事实，不是新数据的分析目标值。`policy` 中 wh_delta 是相对可比范围的最低小时差，duration_h 是需连续成立的小时数，min_comparables 是同服务组合且温度差不超过 temp_band 的最少观测，uncertain_margin 是总分表匹配的误差带（Wh）。
