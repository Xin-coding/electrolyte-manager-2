# Electrolyte Manager 3.2.0 — 图表范围与倒计时输入修复

基于已发布的 3.1.0。

## 图表操作

- 实时监控、历史对比及加液图恢复 2.1 风格的图内右键菜单，删除额外的底部范围工具栏和独立范围弹窗。
- 在图内右键，进入 X / Y 轴菜单，可切换自动或手动并输入上下限；鼠标拖动、缩放和查看全图继续可用。
- 点击范围输入框后冻结该轴；输入中的文字、光标及选择不会被实时刷新改写。回车或离开输入框后应用有效范围。
- 非数值、无穷值及下限不小于上限会标红提示，保留当前有效视图。恢复自动或更换数据集会清除未完成的范围输入。
- 保留 3.0 的手动时间区间查询、过期异步结果隔离、实时 X 轴联动、独立 Y 轴及当前视图导出行为。

## 倒计时输入

- 使用独立且有明确点击区域的上下按钮，支持鼠标和键盘方向键增减。
- 只接受 0–9 数字组成的整数。空白、符号、负数、小数、科学计数、文字以及超限值均保留原输入并标红，不静默改正。
- 小时为 0–999、分钟为 0–59；某一栏可以为 0，总时长至少 1 分钟。预约期间锁定输入。
- 非法输入无法创建预约。原有提前确认、到期检查、取消、急停及等待新读数后决策的逻辑不变。

## 连接提示

串口拒绝访问时显示明确的中英文排查提示，并在悬浮提示中保留系统原始错误。不同程序不能同时独占同一个串口；需要先在占用程序中释放对应设备。驱动要求重新插拔或重启时，软件不能靠更改仪器协议解决。

本版不改变泵通信、加液规则、平滑、温度补偿、电荷计算及 SQLite 格式。更新时退出未在记录的程序，备份并替换 app 文件夹，保留原 data、exports 和快捷方式。

## 验证

自动测试、界面检查及 CQ 现场验证的具体结果分别记录在 VALIDATION.md 和交付目录验收报告中。未完成的硬件项目不会按软件测试通过来计入。

## English

Restores the compact 2.1-style plot context menu and protects manual range drafts from live updates. Countdown inputs now have reliable step buttons and visible strict digit validation. Invalid input remains visible and cannot schedule feedback. Serial access-denied errors have actionable bilingual messages. Existing acquisition, delayed-start service, pump execution and data formats remain compatible.
