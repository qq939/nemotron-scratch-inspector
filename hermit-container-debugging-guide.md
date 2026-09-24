# Hermit 容器卡片调试与测试经验总结

> 基于人猫替换、卡布奇诺拉花、Mythos 小说续写等多个项目的实际部署踩坑整理。
> 整理时间：2026-07-23

---

## 一、平台基础架构

| 项目 | 说明 |
|------|------|
| 控制台 | `https://hermit.dimond.top/` |
| 容器列表 | `GET https://hermit.dimond.top/api/agents` |
| 容器日志 | `GET /api/agents/{name}/logs?tail=N` |
| SSH终端 | `wss://hermit.dimond.top/ws/ssh?container={name}` |
| 发消息给容器AI | `POST /api/agents/{name}/send-message` |
| 平台级Ask | `POST /api/claude-ask` |
| 容器内端口 | 固定 `8082`（service_port） |
| 宿主端口映射 | 容器名前缀数字，如 `18084-mythos` → `18084` |
| 外部访问 | `http://dimond.top:{host_port}/` |
| 项目目录 | `/home/agent/.claude/workspace/project/` |

---

## 二、三条部署通道及其坑点

### 通道1：WebSocket SSH（最可靠）

**端点**：`wss://hermit.dimond.top/ws/ssh?container={container_name}`

**能力**：完整 shell 权限，可 git clone、npm install、启动进程、编辑文件。

**踩过的坑**：
- 输出包含大量 ANSI 转义码（颜色、光标），需要正则清理才能阅读
- `recv()` 不一定一次收全，必须循环读取 + 设置超时
- 命令行太长会被终端回显截断，看起来像乱码 → 拆成短命令
- 交互式命令（vim 等）不可用，只能用非交互式方式写文件（`echo`、`cat << 'EOF'`）
- SSH 会话断开后，`nohup` 启动的进程可能变 zombie → 必须用 `setsid` 而非 `nohup`
- 容器内没有 `ss` 命令，无法直接查看端口监听状态

### 通道2：send-message API（不可靠）

**端点**：`POST /api/agents/{name}/send-message`，body: `{"message": "..."}`

**本质**：把消息发给容器内运行的 Claude Agent，由它用 bash 执行。

**踩过的坑**：
- **返回 `{ok: true}` 仅表示消息已投递，不代表命令已执行**
- 容器 agent 繁忙时（正在跑续写任务等）会直接忽略消息
- 容器内的 Claude 有安全警觉，可能把部署指令当成 prompt injection 拒绝执行
  - 它拒绝过 kill 进程、clone 仓库、写 .env、启动服务
  - 连"我是主人"也被当作注入攻击拒绝
- 简单只读命令有时返回 "No response requested"，直接忽略

**结论**：send-message 只适合发简单查询，不适合做部署。

### 通道3：容器内 Ask 端点

**端点**：`http://localhost:8082/ask/claude?q=xxx`（支持 base64 编码）

**底层**：依赖 `run_claude.js`，通过 `/usr/local/bin/claude` CLI 执行。

**踩过的坑**：
- `run_claude.js` 缺失时返回 502
- Claude CLI 续写调用：`claude --dangerously-skip-permissions --continue --print -`，通过 stdin 传入 prompt
- 默认超时 5 分钟不够 → 改为 50 分钟
- **HTTP 超时后 Claude 可能继续写完，但内容会被丢弃** → 需做超时后仍保存的逻辑

---

## 三、关键踩坑记录

### 1. 端口绑定：必须 0.0.0.0

**现象**：服务启动成功，容器内 curl localhost:8082 能通，但外部访问返回空。

**原因**：`app.listen(PORT)` 在某些环境下默认绑 127.0.0.1，FRP 隧道转发不过来。

**解决**：`app.listen(PORT, '0.0.0.0')`，显式绑定所有接口。

### 2. 进程持久化：setsid 而非 nohup

**现象**：SSH 会话断开后，node 进程变成 zombie/defunct，服务挂了。

**原因**：`nohup` 在 WebSocket SSH 环境下不够可靠，会话断开时进程收到 SIGHUP。

**解决**：
```bash
setsid node server.js >> logs/novel-stdout.log 2>&1 & disown
```

### 3. git reset --hard 会丢数据

**现象**：部署后小说内容消失，chapters/ 目录清空。

**原因**：数据文件（mythos.txt、chapters/）虽然在 .gitignore 里，但 `git reset --hard` 会清空未跟踪的文件。

**解决**：
- 部署前备份数据文件
- 或在部署脚本中加入备份恢复逻辑
- .gitignore 必须覆盖所有数据文件（历史教训：小说内容曾被 git clone 覆盖丢失）

### 4. 分支不一致：master vs main

**现象**：容器内 git pull 不到最新代码。

**原因**：容器本地分支是 `master`，远程是 `main`。

**解决**：
```bash
git fetch origin && git reset --hard origin/main
```

### 5. 容器系统文件边界

**严禁**将以下文件加入项目仓库或 .gitignore：
- `run_claude.js`
- `start.sh`
- `user_start.sh`

这些是容器平台自身文件，由 `start.sh` → `user_start.sh` 链路管理。容器启动报错（如 "Cannot find module run_claude.js"）由容器自身流程解决，项目代码无需干预。

### 6. user_start.sh 的位置

**路径**：`/home/agent/.claude/workspace/project/user_start.sh`

**作用**：容器重启时自动拉起服务。

**注意**：这是容器系统文件，不要加入 git 仓库。

---

## 四、推荐部署流程

```bash
# 1. 推送代码到 GitHub
git add . && git commit -m "..." && git push origin main

# 2. 通过 WebSocket SSH 进入容器
# （使用 websocket-client 连接 wss://hermit.dimond.top/ws/ssh?container={name}）

# 3. 在容器内执行部署
cd /home/agent/.claude/workspace/project

# 备份数据（重要！）
cp -r chapters/ /tmp/chapters_backup/ 2>/dev/null

# 同步代码
git fetch origin && git reset --hard origin/main

# 恢复数据（如果有备份）
cp -r /tmp/chapters_backup/* chapters/ 2>/dev/null

# 杀掉旧进程
pkill -9 -f "node server.js"
sleep 2

# 启动新进程（用 setsid，不用 nohup）
setsid node server.js >> logs/novel-stdout.log 2>&1 & disown

# 4. 验证（从外部）
curl http://dimond.top:{host_port}/
```

---

## 五、调试检查清单

部署后逐项验证：

- [ ] 容器内 node 进程存活（`ps aux | grep node`，不能是 zombie/defunct）
- [ ] 端口绑定正确（容器内 `curl localhost:8082` 有响应）
- [ ] 外部可访问（`curl http://dimond.top:{port}/` 返回 HTML）
- [ ] 数据文件完整（mythos.txt / chapters/ 不为空）
- [ ] .gitignore 覆盖所有数据文件
- [ ] user_start.sh 存在且可执行（容器重启后自动拉起）
- [ ] 日志文件有输出（`tail -f logs/novel-stdout.log`）

---

## 六、备选方案

当 WebSocket SSH 和 send-message 都不可靠时的备选：

1. **通过项目文件空间上传**：`coze agent file upload` 上传到项目空间，再从容器内拉取
2. **通过 GitHub 中转**：push 到 GitHub，容器内 git fetch + reset
3. **让用户手动在容器终端执行**：最可靠但最不方便

---

## 七、OBS 文件上传

上传方式（HTTP PUT，无需认证）：
```bash
curl --upload-file <本地文件> http://obs.dimond.top/<文件名>
```

- 上传成功后返回：`http://obs.dimond.top/<文件名>`
- 文件名建议英文+数字+短横线，避免中文 URL 编码问题
- 单次限制 10MB
