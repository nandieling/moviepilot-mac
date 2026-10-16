#!/bin/bash
# 应用生命周期检查：界面可用 -> 宿主退出后不留孤儿。
# 用法：packaging/lifecycle_check.sh [dev仓库路径]
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEV_ROOT="${1:-$HOME/MoviePilot}"
BACKEND_PORT="${MOVPILOT_BACKEND_PORT:-3201}"
FRONTEND_PORT="${MOVPILOT_FRONTEND_PORT:-3200}"
APP="$ROOT/dist/MoviePilot.app/Contents/MacOS/MoviePilotHost"
CONFIG=$(mktemp -d)
FAILED=0

probe() { # 连不上就报 0，忽略 curl 自己打的 000
  local code
  code=$(curl -sS -m 2 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null)
  case "$code" in ""|000) echo 0 ;; *) echo "$code" ;; esac
}

wait_closed() { # URL 上限秒数
  local limit=$2
  local deadline=$((SECONDS + limit))
  while [ $SECONDS -lt $deadline ]; do
    [ "$(probe "$1")" = "0" ] && { echo "  $1 已关闭（$((SECONDS - START))s）"; return 0; }
    sleep 1
  done
  echo "  $1 仍在监听，判定失败"; FAILED=1; return 1
}

# MoviePilot 会改写进程标题，按进程名找不到残留，一律按端口归属处理。
listening_on() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | tr '\n' ' '; }

pkill -9 -f "MacOS/MoviePilotHost" 2>/dev/null
pkill -9 -f "mpweb" 2>/dev/null
for port in $BACKEND_PORT $FRONTEND_PORT; do
  for pid in $(listening_on "$port"); do kill -9 "$pid" 2>/dev/null; done
done
sleep 1
for port in $BACKEND_PORT $FRONTEND_PORT; do
  busy=$(listening_on "$port")
  [ -n "$busy" ] && { echo "端口 $port 已被 $busy 占用，先释放再测"; exit 1; }
done
python3 "$ROOT/packaging/build_app.py" --dev-root "$DEV_ROOT" >/tmp/moviepilot-build.log 2>&1 || { tail -5 /tmp/moviepilot-build.log; exit 1; }

for signal in TERM KILL; do
  echo "=== 宿主收到 $signal ==="
  MOVPILOT_DEV_ROOT="$DEV_ROOT" CONFIG_DIR="$CONFIG" \
  MOVPILOT_BACKEND_PORT=$BACKEND_PORT MOVPILOT_FRONTEND_PORT=$FRONTEND_PORT \
    "$APP" --headless >/tmp/moviepilot-app.log 2>&1 &
  HOST=$!
  python3 "$ROOT/packaging/smoke_test.py" --url "http://127.0.0.1:$FRONTEND_PORT" --wait 150 | tail -1
  kill -"$signal" $HOST
  START=$SECONDS
  wait_closed "http://127.0.0.1:$FRONTEND_PORT/" 45
  wait_closed "http://127.0.0.1:$BACKEND_PORT/health/ready" 60
  sleep 2
  LEFT="$(listening_on $BACKEND_PORT) $(listening_on $FRONTEND_PORT)"
  LEFT=$(echo "$LEFT" | wc -w | tr -d ' ')
  echo "  仍在监听测试端口的进程数：$LEFT"
  [ "$LEFT" = "0" ] || FAILED=1
  sleep 2
done

rm -rf "$CONFIG"
[ $FAILED -eq 0 ] && echo "生命周期检查通过" || echo "生命周期检查失败"
exit $FAILED
