#!/bin/bash
# user_start.sh
# 启动 Nemotron Scratch Inspector (port 8082)
# 用法：直接执行，或被容器 entrypoint 自动调用
set -e

WORKSPACE="/home/agent/.claude/workspace/project"
cd "$WORKSPACE"

# 日志
mkdir -p logs
LOG="$WORKSPACE/logs/run.log"
touch "$LOG"

# Python 解释器：优先用 venv，没有则用系统 python3
if [ -x "$WORKSPACE/.venv/bin/python3" ]; then
    PYTHON_BIN="$WORKSPACE/.venv/bin/python3"
elif [ -x "$WORKSPACE/.venv/bin/python" ]; then
    PYTHON_BIN="$WORKSPACE/.venv/bin/python"
else
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi
export PYTHON_BIN

# HuggingFace 镜像（国内环境拉模型用）
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export NEMOTRON_MODEL="${NEMOTRON_MODEL:-nvidia/Nemotron-3-Embed-1B-BF16}"
export NEMOTRON_DEVICE="${NEMOTRON_DEVICE:-cpu}"
export NEMOTRON_TIMEOUT_S="${NEMOTRON_TIMEOUT_S:-600}"
# Matryoshka 截断维度（512 / 1024 / 2048）
export NEMOTRON_DIM="${NEMOTRON_DIM:-2048}"

echo "[$(date -Iseconds)] user_start.sh: PYTHON_BIN=$PYTHON_BIN" >> "$LOG"
echo "[$(date -Iseconds)] user_start.sh: HF_ENDPOINT=$HF_ENDPOINT MODEL=$NEMOTRON_MODEL DIM=$NEMOTRON_DIM" >> "$LOG"

# Python 依赖健康检查（不阻塞启动，只是记录）
if "$PYTHON_BIN" -c "import numpy, PIL" 2>/dev/null; then
    echo "[$(date -Iseconds)] user_start.sh: numpy+PIL OK" >> "$LOG"
else
    echo "[$(date -Iseconds)] user_start.sh: WARN numpy or PIL missing — worker will fallback" >> "$LOG"
fi
if "$PYTHON_BIN" -c "import torch, sentence_transformers" 2>/dev/null; then
    echo "[$(date -Iseconds)] user_start.sh: torch+sentence-transformers OK" >> "$LOG"
else
    echo "[$(date -Iseconds)] user_start.sh: WARN torch or sentence-transformers missing — Nemotron model disabled (fallback embedding)" >> "$LOG"
fi

# 启动 server.js（后台）
pkill -f "node $WORKSPACE/server.js" 2>/dev/null || true
sleep 0.5

nohup node "$WORKSPACE/server.js" >> "$LOG" 2>&1 &
SERVER_PID=$!
echo "[$(date -Iseconds)] user_start.sh: server.js pid=$SERVER_PID" >> "$LOG"
echo $SERVER_PID > "$WORKSPACE/.server.pid"

# 健康检查（最多等 10s）
for i in $(seq 1 20); do
    if curl -sf --max-time 1 "http://localhost:8082/health" >/dev/null 2>&1; then
        echo "[$(date -Iseconds)] user_start.sh: server.js up after ${i}*0.5s" >> "$LOG"
        exit 0
    fi
    sleep 0.5
done

echo "[$(date -Iseconds)] user_start.sh: WARN server not responding on 8082 (will keep running)" >> "$LOG"
exit 0
