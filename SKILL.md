# SKILL.md — Nemotron Scratch Inspector

## 运行状态（最近一次探活）

| 项 | 值 |
| --- | --- |
| `server.js` 进程 | PID 50 ✅ |
| `run_claude.js` 进程 | PID 97 ✅ |
| 端口 8082 `/health` | `{"ok":true,"port":8082,"service":"nemotron-scratch-inspector"}` |
| Python venv | `/home/agent/.claude/workspace/project/.venv/bin/python3` |
| Git HEAD | `e37c1f2`（main 分支） |
| `logs/run.log` | 已写入启动记录 + 监听日志 |
| `logs/start.log` | 空（容器入口本次未触发 `user_start.sh`） |
| `logs/agent_tui.log` | 含本次会话 1 条输入 |

## 触发场景

- 用户要求"用 Nemotron 检测图片划痕"、"做图片缺陷检测"、"做划痕识别 API"
- 用户指定 `Nemotron-3-Embed-1B-BF16` + `SentenceTransformers` + `PyTorch CPU`
- 用户要求"包装成 /nemtron 接口，GET/POST，q 是提问文字，img 是图片"
- 用户要求输出"检测后的图片 + JSON"

## 工作流程

1. 确认服务在 8082 端口已部署（`curl http://localhost:8082/health`）
2. 若用户提供了测试图片 → 转 base64 → 调 `POST /nemtron`
3. 检查响应 JSON 是否 `ok:true`，`intent` 是否符合用户期望
4. 把 `png_base64` 字段解码落盘，或前端直接展示
5. 任何修改后必须：
   - 更新 `user_start.sh` / `server.js` / `nemtron_worker.py` / `public/*`
   - `git add . && git commit -m "..."` 并 append 到 `commit.txt`
   - 发邮件给 `1119623207@qq.com` 并附截图

## 关键文件

| 文件 | 作用 |
| --- | --- |
| `server.js` | Node HTTP 入口；`/nemtron` 路由 |
| `nemtron_worker.py` | Python 推理 worker（Nemotron 文本嵌入 + CV 检测） |
| `public/index.html` | 聊天 UI |
| `public/app.css` | 仿 dimond.top 浅色蓝色样式 |
| `public/app.js` | 前端逻辑：图片上传、附件预览、调 `/nemtron`、渲染标注图 |
| `user_start.sh` | 容器启动入口 |
| `logs/run.log` | 服务端日志 |

## 接口规范

- `POST /nemtron` body: `{ "q": "text prompt", "img": "base64 or dataURL or URL" }`
- 响应: JSON，详见 `README.md` §JSON 响应示例
- 也支持 `GET /nemtron?q=...&img=...`，默认返回 `multipart/mixed`（JSON + PNG）

## 故障排查

### Nemotron 模型加载相关

| 现象 | 原因 | 修复 |
| --- | --- | --- |
| 模型首次加载超时 | HF 镜像不可达 / 网络 | 检查 `HF_ENDPOINT`（默认 `https://hf-mirror.com`） |
| `TypeError: __init__() got an unexpected keyword argument 'model_kwargs'` | sentence-transformers 版本太旧 | 升级到 `>=5.4.1` |
| `ValueError: Tokenizer class not found` | transformers 版本太低 | 升级到 `>=5.2.0` |
| `Unrecognized keys in 'rope_parameters' for 'rope_type'='yarn': {'apply_yarn_scaling'}` | 已知无害警告 | 忽略即可 |
| `attn_implementation 'flash_attention_2' 不可用` | CPU 环境无 flash_attn | 代码已自动 fallback 到 `eager` |
| `ministral3` 架构不识别 | transformers 版本太低 | 升级到 `>=5.2.0`（Nemotron backbone 是 Ministral-3） |

### Nemotron 正确用法要点（修改代码时必须遵守）

参考 [huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16](https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16)：

1. **query / passage 前缀**：用 `encode_query()` / `encode_document()`，会自动加 `query: ` / `passage: ` 前缀
2. **dtype**：必须 `torch.bfloat16`
3. **attn_implementation**：GPU 用 `flash_attention_2`；CPU 自动回退 `eager`
4. **max_seq_length**：`32768`
5. **trust_remote_code**：`True`（Ministral-3 自定义代码）
6. **相似度**：双方都 L2-normalize 后 dot product = cosine；或用 `model.similarity(q, p)`
7. **Matryoshka**：可截断到 1024 / 512（用 `NEMOTRON_DIM` 配置）

### 其他

- 模型首次加载超时：检查 `HF_ENDPOINT` 镜像可达性 + 网络
- 推理慢：1B BF16 模型 CPU 推理 ~3-15s/请求；可换 `NEMOTRON_MODEL=sentence-transformers/all-MiniLM-L6-v2` 提速；或降低 `NEMOTRON_DIM=512`
- 接口 500：看 `logs/run.log`（Node）+ `logs/agent_tui.log`（Python 输出）

## 安全 / 性能

- 限制请求体 ≤ 16MB（`server.js` 里 12MB 阈值是图片 base64）
- 前端限制图片 ≤ 8MB（`MAX_IMG_BYTES`）
- 同一请求 3s 内去重
- 历史最多 50 条 / 30 条（localStorage）
- Python worker 超时 600s 默认；可在 `NEMOTRON_TIMEOUT_S` 调整

## 与现有项目关系

- 保留原 `/ask/claude` 路由（透传到 `run_claude.js`）→ 不破坏已有能力
- 仿 dimond.top:18097 的 UI 风格（chat 布局、浅色蓝色调、消息气泡、附件预览）
- 把 `/nemtron` 包装成与 `/ask/claude` 一致的接口风格（GET/POST 双支持，参数化）

## 最近会话摘要（2026-07-27）

- **第 1 轮（容器启动）**：CMD 自动执行 → 检测到 `user_start.sh` → `nohup node server.js` → PID 50 → `/health` 200 OK
- **第 2 轮（项目加载）**：Agent 加载 `systemreadme.md`、`AGENTS.md` 等规则文件 → 决定本轮工作流
- **第 3 轮（看护运维）**：
  1. `ls` 项目根目录 + `logs/` + 读取 `systemreadme.md`、`user_start.sh`、`server.js`、`run_claude.js`、`nemtron_worker.py`、`README.md`、`SKILL.md`、`logs/*`
  2. 验证 `curl http://localhost:8082/health` 返回 200
  3. 验证 `ps -ef` 看到 PID 50（server.js）和 PID 97（run_claude.js）
  4. 读取 git log（`e37c1f2`）、`git status`（多个未提交修改）
  5. 整理"项目结构 + 关键细节 + 最后 3 轮对话总结"追加到 `README.md`
  6. 在 `SKILL.md` 顶部新增"运行状态"表格、"最近会话摘要"
- **第 4 轮（本轮 / 学习 Nemotron 正确用法）**：
  1. WebFetch 官方 model card：`https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16`
  2. 对照 `nemtron_worker.py` 找出 6 处偏离官方用法：
     - 缺失 `query: ` / `passage: ` 前缀
     - 用 `model.encode([s])` 而非 `encode_query([s])` / `encode_document([s])`
     - 模型加载未指定 `dtype=torch.bfloat16`
     - 未启用 `attn_implementation=flash_attention_2`（CPU 回退 `eager`）
     - 未设置 `max_seq_length=32768`
     - 未用 Matryoshka 截断
  3. 修改 `nemtron_worker.py`：
     - 加 `NEMOTRON_QUERY_PREFIX` / `NEMOTRON_PASSAGE_PREFIX` 常量
     - `_load_model_lazy()` 用 `model_kwargs={dtype, attn_implementation}`，CPU 自动回退 `eager`，设置 `max_seq_length=32768`
     - `_load_model_lazy()` 用 `encode_document()` 计算意图原型
     - `_embed_text()` 用 `encode_query()` 计算 query 嵌入，支持 Matryoshka 截断
     - 新增 `_compute_intent_scores()`：优先 `model.similarity()`，回退 numpy dot
     - 修复 `intent_scores` 重复调用 `_embed_text` 的 bug
     - 在返回 JSON 中加 `embed_dim` 字段
     - 引入 `NEMOTRON_DIM` 环境变量（512 / 1024 / 2048）
  4. 更新 `README.md`：新增"Nemotron-3-Embed-1B-BF16 正确用法（官方推荐）"小节、依赖版本升级（sentence-transformers>=5.4.1、transformers>=5.2.0）、"加载失败的常见原因"故障表、`NEMOTRON_DIM` 环境变量、推理流水线更新
  5. 更新 `SKILL.md`：故障排查表、Nemotron 正确用法要点

### 本轮待办（未执行）

- [ ] 修复 `logs/start.log` 为空的问题（容器入口未触发 → 检查 `user_start.sh` 是否被自动调用）
- [ ] 新建 `commit.txt`（按 systemreadme 第十一节规范追加 commit_id + 标题）
- [ ] 处理 9 个未提交的修改文件 → `git add . && git commit -m "fix(nemtron): 对齐官方推荐用法（prefix / encode_query / dtype bf16）"`
- [ ] 清理 `__pycache__/`（`.gitignore` 已忽略，但目录仍存在）
- [ ] 重启 `server.js` 让新代码生效（`bash user_start.sh` 或 `pkill -f "node server.js" && nohup node server.js &`）
