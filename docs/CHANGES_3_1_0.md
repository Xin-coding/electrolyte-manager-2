# Electrolyte Manager 3.1.0 — 反馈启动倒计时

基于 3.0.0，新增先记录、延迟启用反馈。M80 / PC8500 选择、图表范围、泵协议、原有反馈目标与加液计算、平滑与温度处理、电荷计算和 SQLite 格式保持兼容。

## 使用

1. 连接控制传感器和泵，确认注射器及装液额度，设置反馈参数，开始记录。
2. 在“实时监控”输入小时与分钟，范围为 1 分钟至 999 小时 59 分钟。点击“启用倒计时”。默认不启用。
3. 在原有泵/注射器确认窗口核对设置及延迟时间。点确定后开始倒计时；取消窗口不创建预约。
4. 页面显示剩余 HH:MM:SS、预计本地启用时间和状态。切换页面不影响后台计时。
5. 到时无需再次确认。检查全部条件后启用原有反馈，等待新的有效读数再判断是否加液；启用本身不直接加液。

倒计时期间持续采集、记录及计算电荷，不进行自动加液。自动目标保留本次记录已建立的目标。

## 取消和到期检查

- 修改反馈或采样设置、重新装液、修改额度、改变泵/注射器身份或注射器说明，会取消预约。普通备注和电流修改不影响预约。
- 取消倒计时、暂停、急停、结束实验及手动提前启用均撤销预约。先取消才能更改延迟。预约期间泵测试被拒绝。
- 到时检查仍在记录、参数有效、确认信息一致、泵已停止且无故障、额度有效和存储正常。泵状态还必须是最近 5 秒内取得的状态。
- 控制读数须是有效实时读数，年龄不超过两个采样周期加 5 秒：M80 为 7 秒，PC8500 为 125 秒，pH 根据设置周期计算。缓存帧、无效读数或断开连接均不能通过。
- 条件不满足时预约结束，持续显示“反馈未启动”和原因；恢复后不自动补启，需重新预约或手动启用。
- 采用单调时钟，调整系统时间不会改变剩余时长；预计本地时间随系统时钟更新。服务错过到期时间超过 5 秒时不补启。
- 预约只保存在当前运行内存，重启后不恢复。创建、取消、成功和失败写入现有实验事件表。

## 验证范围

倒计时采用模拟时钟、泵和传感器验证，并检查打包后的 Windows 程序。3.1 没有执行真实泵加液或新的设备硬件验收。3.0 的 M80 只读实机验收记录仍见对应版本发布说明。

## English

Monitor now supports a one-shot delayed feedback start (1 minute to 999 h 59 min). Confirm the pump, syringe and delay when scheduling. Recording continues with feedback off. At the deadline, the service checks confirmed settings, pump and load identity, storage, stopped pump status, and fresh live control readings. No second dialog is shown. Existing feedback starts only after these checks, and dosing decisions wait for a new post-activation sample. Cancellation and emergency stop invalidate queued starts. Failed or missed starts never retry, and reservations are not restored after restart. Both Chinese and English interfaces are available.
