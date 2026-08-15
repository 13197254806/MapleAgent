# MapleBot V1

这是一个面向单客户端、单角色、单张固定地图的 CV 控制闭环原型。它只读取窗口画面并通过 Windows 标准键盘输入执行短时动作，不读取游戏内存、不注入进程、不修改网络封包，也不提供反作弊绕过。

> 使用前请确认目标游戏和服务器规则允许自动化。建议先在可控测试环境中完成模板、ROI、按键和地图标定；首次联调将 `control.mode` 设为 `mapping`。

## 已实现的 V1 链路

- Windows 指定窗口客户区捕获、固定分辨率缩放和 JPEG 编码（10～15 FPS 可配）。
- 单 WebSocket 二进制帧协议；消息均带 `session_id`、单调序号和毫秒时间戳。
- FastAPI 单会话服务端，模板匹配和通用 YOLO 风格 ONNX 适配器。
- 小地图玩家色点、HP/MP 色条、角色/怪物/死亡/阻塞 UI 检测。
- 连续帧中值平滑、识别丢失计数、位置不变检测和最近地图节点定位。
- `INIT / MAPPING / PATROL / COMBAT / RECOVER / STOPPED` 状态机。
- 图结构固定地图、相邻边导航、固定巡逻路线和基础距离战斗。
- 100～800 ms 短时动作计划、白名单输入、ACK 和单未决计划约束。
- 客户端 watchdog、服务端 heartbeat timeout、过期计划拒绝及 F12 紧急停止。
- 完整会话 Recorder、Mapping Mode 候选图和离线 Replay 差异报告。

## 目录

```text
src/maplebot/
  client/        Windows 捕获、通信、输入执行与 watchdog
  server/        FastAPI、SessionManager 和逐帧闭环
  perception.py  传统 CV、模板和 ONNX 检测
  world.py       连续帧状态融合
  map_service.py 地图图模型、导航查询和 Mapping Trace
  decision.py    FSM、战斗、导航和 ActionPlanner
  recorder.py    会话记录
  replay.py      离线重放与差异统计
  discovery.py   UDP 局域网服务发现
config.yaml      统一配置
environment.yml  Conda 环境 maple_agent
maps/            单地图 JSON
assets/templates 固定分辨率模板
```

## 安装

项目统一使用名为 `maple_agent` 的 Conda 环境。服务端和 Windows 客户端都在各自机器的项目目录执行：

```bash
conda env create -f environment.yml
conda activate maple_agent
```

更新已有环境：

```bash
conda env update -n maple_agent -f environment.yml --prune
```

`environment.yml` 使用 Python 3.11，并通过 pip 以 editable 模式安装项目、Windows 捕获依赖和开发测试依赖。模板模式不需要 `onnxruntime`；需要 ONNX 时额外执行：

```bash
python -m pip install -e ".[onnx]"
```

客户端依赖 `mss`，窗口必须处于可见状态；V1 不尝试捕获被完全遮挡或最小化的硬件加速画面。

## 首次配置

复制 `config.yaml` 为不入库的 `config.local.yaml`，至少修改：

1. 保持 `client.server_url: null`，让客户端自动发现局域网服务端。
2. `client.window_title`：目标窗口标题中稳定的一段文本。
3. `input.bindings`：逻辑动作到实际按键的映射。服务端无法下发此表之外的键。
4. `perception.rois`：按缩放后画面的归一化坐标 `[x, y, width, height]` 标定小地图、HP、MP。
5. HSV 范围和模板阈值。OpenCV HSV 范围为 H `0～180`、S/V `0～255`。
6. `control.mode: mapping`，先禁止自动输入并采集地图轨迹。

模板使用固定分辨率截图裁剪，文件名必须以类别开头，例如：

```text
assets/templates/player_default.png
assets/templates/monster_slime.png
assets/templates/death_dialog_default.png
assets/templates/blocking_dialog_default.png
```

如改用 ONNX，将 `backend` 设为 `onnx` 并填写 `model_path`。适配器支持常见的 `Nx6`（xyxy、置信度、类别）以及 YOLOv8 风格输出；实际导出模型应先用 Replay 校验坐标和类别顺序。

## 启动

先复制配置并启动服务端：

```bash
cp config.yaml config.local.yaml
conda activate maple_agent
python -m maplebot.server --config config.local.yaml
```

Windows PowerShell 使用：

```powershell
Copy-Item config.yaml config.local.yaml
conda activate maple_agent
python -m maplebot.client --config config.local.yaml
```

客户端启动时会自动查找服务端，不再需要配置 IP。也可以直接运行脚本；脚本通过 `conda run -n maple_agent` 使用指定环境：

```bash
./scripts/run-server.sh
```

```powershell
.\scripts\run-server.ps1
.\scripts\run-client.ps1
```

健康检查仍为 `GET http://服务端地址:8765/health`。

## 局域网服务发现

默认配置：

```yaml
client:
  server_url: null

discovery:
  enabled: true
  service_name: maple-agent-v1
  bind_host: 0.0.0.0
  port: 8764
  broadcast_addresses: [255.255.255.255]
  timeout_ms: 500
  attempts: 3
```

服务端监听 UDP `8764`。客户端发送带随机 nonce 的广播查询，服务端向查询来源单播返回 WebSocket 端口；客户端使用响应包的来源 IP 连接 TCP `8765`。断线重连时会重新发现，因此服务端 DHCP 地址变化也不需要更新客户端配置。

需要在服务端防火墙允许：

- UDP `8764`：服务发现；
- TCP `8765`：HTTP 健康检查和 WebSocket。

广播通常只在同一二层局域网内有效，不会跨路由器、访客网络或隔离 VLAN。如果路由器不转发全局广播，可加入网段的定向广播地址，例如：

```yaml
discovery:
  broadcast_addresses:
    - 255.255.255.255
    - 192.168.1.255
```

发现失败时可临时使用手动覆盖：

```yaml
client:
  server_url: ws://192.168.1.20:8765/ws
```

V1 只接受第一个名称匹配的服务端，服务发现本身不提供身份认证，因此应只在可信局域网中运行，不要把 UDP/TCP 端口暴露到公网。

本地紧急停止键默认为 `F12`。触发后客户端立即释放所有由本程序按下的键，并结束当前控制连接。连接断开、服务端超过 1 秒没有响应或执行异常时也会释放按键。

默认 `input.require_game_foreground: true`。游戏窗口失去前台焦点时，客户端会拒绝或中断计划并释放按键，避免输入落到其他应用。

## Mapping Mode

将 `control.mode` 设为 `mapping` 后，服务端仍记录画面和小地图轨迹，但不发送任何自动动作。操作者手动遍历地图，结束连接后，会话目录会生成 `map_candidate.json`。

候选图只根据轨迹距离聚类，并按相邻轨迹的左右关系创建候选边。复制到 `maps/` 后必须人工完成这些内容：

- 合并或拆分平台关键节点；
- 补充 `jump`、`drop` 以及平台连接边；
- 修正边方向和 `bidirectional`；
- 设置 `patrol_route`；
- 用 Replay 检查最近节点定位。

确认地图后，把 `map.path` 指向新文件，再把 `control.mode` 改为 `patrol`。

## Recorder 与 Replay

每次连接创建 `recordings/<时间>-<session_id>/`：

```text
session.json
frames/*.jpg
frames.jsonl
perception.jsonl
world_state.jsonl
decisions.jsonl
action_plans.jsonl
events.jsonl
```

离线重放不连接游戏客户端，也不会执行按键：

```bash
python -m maplebot.replay recordings/<session-directory>
```

新输出位于该会话的 `replays/<时间>/`，包含重新生成的四条 JSONL 和 `summary.json`。摘要会比较玩家/怪物检测数、地图节点、FSM 状态和意图。为了让逐帧状态机可直接比较，回归录制建议保持 `save_every_nth_frame: 1`。

`events.jsonl` 中的 `frame_processed` 同时记录服务端处理耗时和校正时钟后的画面年龄，可用于检查 150 ms 延迟目标。

## 协议与安全行为

画面以一个 WebSocket 二进制消息传输：`4 字节网络序 JSON 头长度 + FrameHeader JSON + JPEG`。控制与 ACK 使用严格 Pydantic JSON 模型。客户端仅接受当前 `session_id`、递增 `plan_id`、未超过 `created_at_ms + ttl_ms` 且按键全部在本地白名单内的计划。

`STOPPED` 是锁存状态：检测到死亡或阻塞窗口后不会自动恢复，需排除问题并重新连接。连续识别不到角色会进入 `RECOVER` 并发送释放全部按键；检测到位置长时间不变时只尝试一次短反向移动加跳跃。

## 测试

```bash
ruff check src tests
pytest
```

单元测试覆盖帧封包、防过期计划、颜色型 UI 感知、状态融合、地图路径、FSM、安全停止、Recorder 与 Replay。真实 Windows 环境还应执行 30 分钟 soak test，并在任务管理器、断网、服务端重启和 F12 场景下逐项确认无按键残留。
