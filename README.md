# Nemotron Scratch Inspector

> 端口 8082 · 聊天式 UI · 文本 + 图片 → Nemotron 意图解析 + 传统 CV 划痕检测

## 运行状态（最新一次会话）

| 检查项 | 状态 |
| --- | --- |
| `user_start.sh` | ✅ 已存在（`start.log` 当前为空，未触发容器启动；`run.log` 已写入） |
| `server.js` 进程 | ✅ PID 50 运行中（`/health` 200 OK） |
| `run_claude.js` 进程 | ✅ PID 97 运行中（兼容 `/ask/claude`） |
| 端口 8082 | ✅ 已绑定（`Nemotron Scratch Inspector` 监听中） |
| Python venv | ✅ `/home/agent/.claude/workspace/project/.venv/bin/python3` 可用 |
| Git 仓库 | ✅ 已初始化，`main` 分支，HEAD = `e37c1f2` |

模仿 [dimond.top:18097](http://dimond.top:18097) 的聊天 UI，提供：

- 一个**聊天界面**：左侧消息流 + 右侧（顶部 brand + 可折叠 API 文档 + 底部输入栏）
- 文本 + 图片 → `POST /nemtron` → 服务端调用 Python Nemotron worker
  - **文本侧**：用 `nvidia/Nemotron-3-Embed-1B-BF16`（SentenceTransformers + PyTorch CPU）
    按官方推荐方式调用：`encode_query()` 自动加 `query: ` 前缀；`encode_document()` 加
    `passage: ` 前缀；dtype=`torch.bfloat16`；attn_implementation=`flash_attention_2`
    （CPU 自动回退到 `eager`）；`max_seq_length=32768`；嵌入维度 2048，支持
    Matryoshka 截断到 1024 / 512。相似度用 `model.similarity()` 或 numpy dot
    product（已 L2 归一化 → 等价 cosine）。
  - **图像侧**：用 Pillow + NumPy 实现
    灰度化 → 简易 CLAHE → Sobel + Canny-like 边缘 → 形态学闭运算 → 连通区域分析
    → 几何筛选 → 形状特征打分 → 与意图关联 → 候选 region
  - 输出：**JSON**（含 regions/summary/intent_scores）+ **PNG 标注图**（bbox + 热力图 + 信息条）

## 路由

| 路径 | 方法 | 用途 |
| --- | --- | --- |
| `/` | GET | 聊天 UI（仿 dimond.top 风格） |
| `/static/*` | GET | 前端静态资源 |
| `/health` | GET | 健康检查 + 模型状态 |
| `/ask/claude?q=...` | GET/POST | 兼容原 `run_claude.js` 透传 |
| `/nemtron?q=...&img=...` | GET | 检测接口（URL 形式） |
| `/nemtron` | POST `{q, img}` | 检测接口（JSON，推荐） |

## 请求 / 响应

```bash
# POST JSON（推荐）
curl -X POST http://localhost:8082/nemtron \
  -H 'Content-Type: application/json' \
  -d '{"q":"请检测图片中的划痕","img":"<base64>"}'

# GET 形式
curl "http://localhost:8082/nemtron?q=划痕&img=$(base64 -w0 sample.jpg)" -o out.bin

# 只取 JSON
curl "http://localhost:8082/nemtron?q=划痕&img=...&format=json"
```

GET 默认 `multipart/mixed`（一段 JSON + 一段 PNG），便于一条命令拿全。
POST 或 `?format=json` 永远返回 JSON，PNG 在 `png_base64` 字段里。

## JSON 响应示例

```json
{
  "ok": true,
  "prompt": "请检测图片中的划痕",
  "intent": "scratch",
  "score": 0.87,
  "embed_model": "nvidia/Nemotron-3-Embed-1B-BF16",
  "device": "cpu",
  "model_loaded": true,
  "img_w": 480, "img_h": 320,
  "regions": [
    {"id":0, "x":60, "y":78, "w":162, "h":34, "score":0.92,
     "severity":"high", "label":"scratch",
     "note":"area=1830, aspect=4.8, fill=0.33"}
  ],
  "summary": {"total": 3, "high": 1, "medium": 1, "low": 1, "none": 0},
  "intent_scores": {"scratch": 0.87, "dent": 0.12, ...},
  "elapsed_s": 4.2,
  "png_base64": "iVBORw0KGgoAAA..."
}
```

## 部署

```bash
# 一次性安装（首次）— 版本要求来自 Nemotron 官方 HuggingFace model card
pip3 install --break-system-packages --user \
    --index-url https://mirrors.aliyun.com/pypi/simple/ \
    "torch>=2.0,<2.5" "sentence-transformers>=5.4.1" \
    "transformers>=5.2.0" "tokenizers>=0.21" \
    "pillow>=10" "numpy>=1.24"

# 启动
bash user_start.sh
# 或前台运行
node server.js
```

访问 `http://localhost:8082/` 即可。

## Nemotron-3-Embed-1B-BF16 正确用法（官方推荐）

参考：[huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16](https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16)

关键点：

1. **query / passage 前缀**
   - 用户的查询必须用 `model.encode_query([...])`，会自动加 `query: ` 前缀
   - 文档/原型必须用 `model.encode_document([...])`，会自动加 `passage: ` 前缀
2. **dtype**：必须 `torch.bfloat16`（模型本身就是 BF16 权重）
3. **attn_implementation**：GPU 推荐 `flash_attention_2`；CPU 没有 flash_attn，自动回退到 `eager` / `sdpa`
4. **max_seq_length**：`32768`
5. **嵌入维度**：2048（Matryoshka 支持截断到 1024 / 512，需要重新 L2 归一化）
6. **相似度**：双方都 L2-normalize 后 dot product = cosine；或用 `model.similarity(q, p)`
7. **trust_remote_code**：必须 True（Ministral-3 自定义代码）

代码侧（`nemtron_worker.py`）已经全部对齐官方推荐。

## 模型下载

第一次请求会触发 Nemotron 模型下载（~2 GB），从 `HF_ENDPOINT`
（默认 `https://hf-mirror.com` 国内镜像）拉取。如果已经下载，会自动用
`~/.cache/huggingface/hub/` 下的缓存。

如果 transformers/sentence-transformers 没装或模型加载失败，worker 会**降级**：

- 用字符 n-gram + 哈希的 fallback 嵌入（dim=384）
- 意图用关键词匹配回退
- 图像侧检测不受影响

这样无论模型就绪与否，接口都能立刻可用。

### 加载失败的常见原因

| 现象 | 原因 | 修复 |
| --- | --- | --- |
| `Unrecognized keys in 'rope_parameters' for 'rope_type'='yarn': {'apply_yarn_scaling'}` | 已知无害警告，transformers 与模型 config 不匹配 | 忽略即可，不影响推理 |
| `TypeError: __init__() got an unexpected keyword argument 'model_kwargs'` | sentence-transformers 版本太旧 | 升级到 `>=5.4.1` |
| `attn_implementation 'flash_attention_2' 不可用` | CPU 环境无 flash_attn | 已被代码自动 fallback 到 `eager` |
| `ValueError: Tokenizer class not found` | transformers 版本太低 | 升级到 `>=5.2.0` |

## 目录结构

```
.
├── server.js              # Node HTTP server (port 8082)
├── nemtron_worker.py      # Python Nemotron + CV 检测 worker
├── user_start.sh          # 启动脚本（容器入口会调用）
├── public/                # 静态前端（仿 dimond.top 风格）
│   ├── index.html
│   ├── app.css
│   └── app.js
├── logs/                  # 日志（run.log / start.log / agent_tui.log）
├── SKILL.md               # Agent Skill 描述
├── README.md              # 本文件
├── AGENTS.md              # Agent workspace 引导（项目级）
└── run_claude.js          # 原 /ask/claude 兼容（保留）
```

## 配置环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8082` | HTTP 监听端口 |
| `PYTHON_BIN` | `python3` | Python 解释器路径 |
| `NEMOTRON_MODEL` | `nvidia/Nemotron-3-Embed-1B-BF16` | SentenceTransformer 模型 id |
| `NEMOTRON_DEVICE` | `cpu` | 推理设备（`cpu` 自动 `eager` attention） |
| `NEMOTRON_DIM` | `2048` | Matryoshka 截断维度（512 / 1024 / 2048） |
| `NEMOTRON_TIMEOUT_S` | `600` | 单次推理超时（秒） |
| `HF_ENDPOINT` | `https://hf-mirror.com` | HuggingFace 镜像 |
| `WORKSPACE_DIR` | `/home/agent/.claude/workspace/project` | 工作目录 |

## 项目结构与关键细节（来自 logs/agent_tui.log 与代码审计）

### 后端架构（双进程）

```
请求 ──► Node server.js (pid 50, port 8082)
           ├─ /                  → public/index.html（聊天 UI）
           ├─ /static/*          → public/* （app.js / app.css）
           ├─ /health            → JSON 健康检查（端口、模型、设备）
           ├─ /ask/claude?q=     → spawn('node', run_claude.js)（兼容原能力）
           └─ /nemtron?q=&img=   → spawn(python3, nemtron_worker.py)
                └─ stdin = JSON, stdout = JSON（PNG 用 base64 嵌入）
```

### 前端架构

- `public/index.html`：聊天布局，仿 dimond.top:18097（左侧消息流，右侧 brand + 可折叠 API 文档 + 底部输入栏）
- `public/app.js`：图片上传（≤ 8MB）、附件预览、调 `POST /nemtron`、渲染 base64 标注图
- `public/app.css`：浅色蓝主题、消息气泡、可折叠面板
- 同一请求 3 秒去重，历史最多 50 条 / localStorage 30 条

### Python Nemotron Worker 推理流水线

1. **文本意图分类**：`nvidia/Nemotron-3-Embed-1B-BF16`（SentenceTransformers + PyTorch）
   - 模型加载：`dtype=torch.bfloat16`、`attn_implementation=flash_attention_2`（CPU 自动回退 `eager`）、`max_seq_length=32768`、`trust_remote_code=True`
   - 用户 prompt → `model.encode_query([...])`（自动加 `query: ` 前缀 + mean-pool + L2-normalize）
   - 意图原型 → `model.encode_document([...])`（自动加 `passage: ` 前缀）
   - 6 个意图原型（scratch / dent / crack / stain / corrosion / normal）→ 余弦相似度
   - 优先 `model.similarity()`；fallback 到 numpy dot（双方已 L2 归一化）
   - 支持 Matryoshka 截断（`NEMOTRON_DIM=512/1024/2048`）
   - 模型未加载时回退到字符 n-gram 哈希嵌入（dim=384）+ 关键词匹配
2. **图像传统 CV 检测**（无 OpenCV，纯 NumPy + Pillow）：
   - 灰度化 → 简化 CLAHE → Sobel + Canny-like 边缘
   - 形态学闭运算（3×3 膨胀+腐蚀） → 4-连通域标记
   - 几何筛选（面积 30~40%W·H） → 形状特征（aspect / fill / elong）
   - 与意图关联（scratch → 细长 / dent → 椭圆 / stain → 大面积低填充）
3. **输出**：JSON（含 regions/summary/intent_scores/embed_dim）+ 标注 PNG（bbox + 热力图 + 顶部信息条）
4. **懒加载 + 降级**：首次请求才下载模型（`HF_ENDPOINT` 默认 `hf-mirror.com`），失败也不挂

### 日志规范

| 日志文件 | 来源 | 内容 |
| --- | --- | --- |
| `logs/start.log` | `user_start.sh` | 启动脚本输出（容器入口触发） |
| `logs/run.log` | `server.js` + Python worker | HTTP 服务运行日志、NEMTRON q.len/img.len、worker 错误 |
| `logs/agent_tui.log` | `run_claude.js` + Claude TUI | Claude Code 会话消息、stdout/stderr、prompt+回答 |

### Git 仓库

- 已初始化（`git init`），分支 `main`
- HEAD = `e37c1f2` "Nemotron Scratch Inspector: chat UI + /nemtron endpoint"
- `.gitignore` 已包含 `logs/`、`.venv/`、`__pycache__/`、`*.pyc`、`*.log`、`models/`、`checkpoints/`、`*.bin`、`*.safetensors` 等
- 注：未发现 `commit.txt`，按 systemreadme 规范建议新建一份（见 SKILL.md "与现有项目关系"）

### 当前异常 / 注意事项

- `logs/start.log` 为空：本次会话中容器入口未触发 `user_start.sh`（服务由既有的 PID 50 持续运行）
- `logs/agent_tui.log` 仅含 1 条当前会话指令（日志被重置/首次写入）
- 工作目录存在多个未提交的修改（`README.md`、`SKILL.md`、`server.js`、`nemtron_worker.py`、`public/*` 等），需在本次任务结束时 `git add . && git commit -m "..."`

## 最后 3 轮对话总结（基于 logs/agent_tui.log）

| 轮次 | 输入 / 上下文 | 主要动作 / 输出 |
| --- | --- | --- |
| 第 1 轮（容器启动） | 容器 CMD 自动执行 → 加载 `/agent-config/`、启动 SSH、写 `~/.claude/settings.json` | 检测到 `user_start.sh` 已存在 → 执行 `nohup node server.js` → PID 50 → `/health` 200 OK |
| 第 2 轮（项目加载） | Agent 加载 `systemreadme.md`、`AGENTS.md`、各项目规则文件 | 推断项目结构 → 决定本次会话的工作流：检查 web app / 启动脚本 / 整理日志 / 更新 README+SKILL |
| 第 3 轮（本轮） | 用户指令："学习 nemotron 模型的正确用法，再修改你的代码" | ① WebFetch Nemotron-3-Embed-1B-BF16 官方文档；② 对照 `nemtron_worker.py` 找出 6 处偏离官方用法（缺失 `query:` / `passage:` 前缀、未用 `encode_query/document`、dtype 不是 bf16、未启用 flash_attention_2、未设 max_seq_length、未用 Matryoshka）；③ 修改 `nemtron_worker.py`：加入前缀常量、`model_kwargs={dtype, attn_implementation}`、`max_seq_length=32768`、`encode_query/encode_document`、`model.similarity()` + numpy 回退、`NEMOTRON_DIM` 截断；④ 更新 README.md 增加"Nemotron 正确用法"小节、环境变量 `NEMOTRON_DIM`；⑤ 更新 SKILL.md 中故障排查表 |

### 本轮具体变更清单

- ✏️ `nemtron_worker.py`：
  - 加 `NEMOTRON_QUERY_PREFIX` / `NEMOTRON_PASSAGE_PREFIX` 常量
  - `_load_model_lazy()` 用 `model_kwargs={dtype: torch.bfloat16, attn_implementation: ...}`，CPU 自动回退 `eager`，设置 `max_seq_length=32768`
  - `_load_model_lazy()` 用 `encode_document()` 计算意图原型
  - `_embed_text()` 用 `encode_query()` 计算 query 嵌入，支持 Matryoshka 截断
  - 新增 `_compute_intent_scores()`：优先 `model.similarity()`，回退 numpy dot
  - 修复 `intent_scores` 重复调用 `_embed_text` 的 bug
  - 在返回 JSON 中加 `embed_dim` 字段
  - 引入 `NEMOTRON_DIM` 环境变量（512 / 1024 / 2048）
- ✏️ `README.md`：新增"Nemotron-3-Embed-1B-BF16 正确用法（官方推荐）"小节、依赖版本升级（sentence-transformers>=5.4.1、transformers>=5.2.0）、"加载失败的常见原因"故障表、`NEMOTRON_DIM` 环境变量、推理流水线更新
- ✏️ `SKILL.md`：故障排查扩展、添加"Nemotron 正确用法要点"提示
