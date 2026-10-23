#!/bin/bash
# ============================================================
# YuE2 云端 worker 一键部署（5 分钟版）
# 用法：在新租的 Vast.ai 实例（nvidia/cuda:12.4.1-base-ubuntu22.04，盘≥120G）上执行：
#   curl -fsSL https://www.cbann.com/deploy/one.sh | bash
# 或：  curl -fsSL https://raw.githubusercontent.com/kurni-12/yue2-worker-package/main/one.sh | bash
# 完成后 worker 自动上线接单（目标站点 https://www.cbann.com）
# ============================================================
set -e
cd /root
export DEBIAN_FRONTEND=noninteractive
START=$(date +%s)

echo "=== [1/5] 基础工具（与下载并行）==="
( apt-get update -qq && apt-get install -y -qq python3-venv python3-pip git ffmpeg >/dev/null 2>&1 ) &
APT_PID=$!

echo "=== [2/5] 下载预装环境包（GitHub CDN，约 3.5G）==="
GH="https://github.com/kurni-12/yue2-worker-package/releases/latest/download"
mkdir -p /root/yue2-home && cd /root/yue2-home
curl -fsSL -o env.part1 "$GH/yue2-env.part1.tar.gz" &
C1=$!
curl -fsSL -o env.part2 "$GH/yue2-env.part2.tar.gz" &
C2=$!

echo "=== [3/5] 模型下载与脚本获取（并行）==="
curl -fsSL -o /root/dl_models_hf.py "https://www.cbann.com/deploy/dl_models_hf.py" || \
curl -fsSL -o /root/dl_models_hf.py "https://raw.githubusercontent.com/kurni-12/yue2-worker-package/main/dl_models_hf.py"
# 区域自适应模型源
if curl -s -m 8 -o /dev/null https://hf-mirror.com; then
  export HF_ENDPOINT=https://hf-mirror.com
else
  export HF_ENDPOINT=https://huggingface.co
fi
echo "HF_ENDPOINT=$HF_ENDPOINT"
( python3 -m pip install -q huggingface_hub 2>/dev/null || pip3 install -q huggingface_hub 2>/dev/null || true
  python3 /root/dl_models_hf.py > /root/dl_models.log 2>&1; echo "模型下载 exit=$?" > /root/dl_models.done ) &
DL_PID=$!

wait $C1 $C2
echo "环境包下载完成：$(ls -la env.part*.tar.gz | awk '{s+=$5} END {printf "%.1f GiB", s/1073741824}')"

wait $APT_PID || echo "apt 部分失败，继续"

echo "=== [4/5] 解压环境 ==="
cat env.part1.tar.gz env.part2.tar.gz > env.tar.gz && rm env.part*.tar.gz
tar xzf env.tar.gz && rm env.tar.gz
ls -la .venv/bin/python .venv-sheetsage2/bin/python worker_ai.py 2>&1 | head -4

echo "=== [5/5] 等模型下载完成 ==="
wait $DL_PID
cat /root/dl_models.done 2>/dev/null
du -sh models 2>/dev/null | awk '{print "models:", $1}'

echo "=== 启动 worker ==="
curl -fsSL -o /root/yue2-home/worker_secret.txt "https://www.cbann.com/deploy/worker_secret.txt" || echo "⚠ 未取到 worker_secret（需确认站点 /deploy/worker_secret.txt 存在）"
cd /root/yue2-home
rm -f worker.pid
export YUE2_KIT=/root/yue2-home
( nohup .venv/bin/python -u worker_ai.py >> worker.log 2>&1 & )
sleep 12
ps aux | grep -c '[w]orker_ai.py' | xargs echo "worker 进程数:"
tail -3 worker.log
echo "=== 部署完成，用时 $(( $(date +%s) - START )) 秒 ==="
