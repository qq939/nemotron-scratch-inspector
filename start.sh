#!/bin/bash
cd /home/agent/.claude/workspace/project
if [ -f user_start.sh ] && [ -s user_start.sh ]; then 
    chmod +x user_start.sh   # ← 赋权
    ./user_start.sh          # ← 执行
fi