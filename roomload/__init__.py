"""活动室试运行诊断与排期工具包。

模块：
- loading: 读取并校验合成源记录，处理 supersedes 替代链。
- events: 把转态事件解析成每个 block 的事实区间（含时钟不确定下的多种解释）。
- metrics: 按点位组计算覆盖、超线分钟比例与同日基线增量。
- licensing: 汇总房间组合 × 门状态 × 设备状态的确认/拒绝/未知结论。
- scheduling: 处理预约、值守、借道通行与换房回执。
"""

__all__ = ["loading", "events", "metrics", "licensing", "scheduling"]
