#!/bin/bash
# ============================================================
# YuE2 云端 worker 全家桶一键部署（环境+模型+代码都在包内）
# 用法（新租 Vast.ai 实例，镜像 nvidia/cuda:12.4.1-base-ubuntu22.04，盘≥100G）：
#   curl -fsSL https://www.cbann.com/deploy/one.sh | bash
# 或：curl -fsSL https://raw.githubusercontent.com/kurni-12/yue2-worker-package/main/one.sh | bash
# 完成后 worker 自动上线接单（目标站点 https://www.cbann.com）
# ============================================================
set -e
cd /root
export DEBIAN_FRONTEND=noninteractive
START=$(date +%s)

echo "=== [1/4] 基础工具（ffmpeg，与下载并行）==="
( apt-get update -qq && apt-get install -y -qq ffmpeg >/dev/null 2>&1 ) &
APT_PID=$!

echo "=== [2/4] 下载全家桶（GitHub CDN，环境+模型+代码 约 18G）==="
GH="https://github.com/kurni-12/yue2-worker-package/releases/latest/download"
mkdir -p /root/yue2-home && cd /root/yue2-home
N=$(curl -fsSL "$GH/parts.txt" 2>/dev/null | head -1 || echo 10)
echo "分卷数: $N"
PIDS=""
for i in $(seq 0 $((N-1))); do
  P=$(printf "%02d" $i)
  ( curl -fSL -o "part$P.tar.gz" "$GH/yue2-env.part$P.tar.gz" && echo "part$P OK" ) &
  PIDS="$PIDS $!"
done
wait $PIDS

echo "=== [3/4] 解压 ==="
cat part*.tar.gz > env.tar.gz && rm -f part*.tar.gz
tar xzf env.tar.gz && rm -f env.tar.gz
ln -sfn env-main .venv && ln -sfn ../pyroot/bin/python3.10 env-main/bin/python
ln -sfn env-sheetsage2 .venv-sheetsage2 && ln -sfn ../pyroot/bin/python3.10 env-sheetsage2/bin/python
# .pth 指向 src 布局的 yue2 包
echo "/root/yue2-home/YuE/src" > env-main/lib/python3.10/site-packages/yue2.pth
echo "/root/yue2-home/YuE/src" > env-sheetsage2/lib/python3.10/site-packages/yue2.pth
wait $APT_PID || echo "apt 部分失败，继续"
.venv/bin/python -c "import torch; print('torch', torch.__version__)"
ls models | head -5

echo "=== [3.5/4] 拉取最新代码（覆盖包内旧版，几 KB 秒更新）==="
RAW="https://raw.githubusercontent.com/kurni-12/yue2-worker-package/main"
for f in worker_ai.py song_executor.py song_request.py web_archive.py audio_exports.py run_generate.py transcribe_bridge.py; do
  curl -fsSL -o "$f" "$RAW/$f" && echo "$f 已更新" || echo "$f 拉取失败，用包内版本"
done
mkdir -p _deploy
mv -f run_generate.py transcribe_bridge.py _deploy/ 2>/dev/null || true

echo "=== [4/4] 启动 worker ==="
curl -fsSL -o worker_secret.txt "https://www.cbann.com/deploy/worker_secret.txt" || echo "⚠ 未取到 worker_secret"
rm -f worker.pid
export YUE2_KIT=/root/yue2-home
( nohup .venv/bin/python -u worker_ai.py >> worker.log 2>&1 & )
sleep 12
ps aux | grep -c '[w]orker_ai.py' | xargs echo "worker 进程数:"
tail -3 worker.log
echo "=== 部署完成，用时 $(( $(date +%s) - START )) 秒 ==="
