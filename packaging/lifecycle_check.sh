#!/bin/bash
# 应用生命周期检查：界面可用 -> 宿主退出后不留孤儿。
# 用法：packaging/lifecycle_check.sh [dev仓库路径]
set -u -o pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEV_ROOT="${1:-$HOME/MoviePilot}"
BACKEND_PORT="${MOVPILOT_BACKEND_PORT:-3201}"
FRONTEND_PORT="${MOVPILOT_FRONTEND_PORT:-3200}"
APP="$ROOT/dist/MoviePilot.app/Contents/MacOS/MoviePilotHost"
LOGDIR="${TMPDIR:-/tmp}/moviepilot-lifecycle"
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

dump_logs() { # 失败时把宿主与两段子进程的日志留在固定位置，免得随临时目录一起消失
  local tag=$1 config=$2
  mkdir -p "$LOGDIR/$tag" && cp "$config/logs/"*.log "$LOGDIR/$tag/" 2>/dev/null
  for name in webhost backend; do
    echo "  ---- $tag $name 日志（末 20 行）----"
    tail -20 "$LOGDIR/$tag/moviepilot.$name.log" 2>/dev/null | sed 's/^/  /'
  done
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
rm -rf "$LOGDIR"; mkdir -p "$LOGDIR"
python3 "$ROOT/packaging/build_app.py" --dev-root "$DEV_ROOT" >/tmp/moviepilot-build.log 2>&1 || { tail -5 /tmp/moviepilot-build.log; exit 1; }

for signal in TERM KILL; do
  echo "=== 宿主收到 $signal ==="
  # 每轮全新的数据目录：上一轮写下的配置/锁不能影响这一轮的启动耗时。
  CONFIG=$(mktemp -d)
  # MOVPILOT_PORT_PERSIST=0：禁掉端口记忆，保证应用真的用脚本指定的端口。
  MOVPILOT_DEV_ROOT="$DEV_ROOT" CONFIG_DIR="$CONFIG" MOVPILOT_PORT_PERSIST=0 \
  MOVPILOT_BACKEND_PORT=$BACKEND_PORT MOVPILOT_FRONTEND_PORT=$FRONTEND_PORT \
    "$APP" --headless >"$LOGDIR/$signal.host.log" 2>&1 &
  HOST=$!
  grep -m1 '^\[端口\]' "$LOGDIR/$signal.host.log" >/dev/null 2>&1 \
    || for _ in $(seq 30); do sleep 1; grep -qm1 '^\[端口\]' "$LOGDIR/$signal.host.log" && break; done
  grep '^\[端口\]' "$LOGDIR/$signal.host.log" | sed 's/^/  /'
  # 就绪耗时单独计时：首次运行要建库、建目录，超时上限给足，但耗时必须打印出来。
  READY_AT=$SECONDS
  while [ $((SECONDS - READY_AT)) -lt 240 ]; do
    [ "$(probe "http://127.0.0.1:$FRONTEND_PORT/")" = "200" ] && break
    kill -0 $HOST 2>/dev/null || break
    sleep 1
  done
  echo "  就绪耗时：$((SECONDS - READY_AT))s"
  if ! python3 "$ROOT/packaging/smoke_test.py" --url "http://127.0.0.1:$FRONTEND_PORT" --wait 30 | tail -1; then
    dump_logs "$signal" "$CONFIG"; FAILED=1
  fi
  kill -"$signal" $HOST 2>/dev/null
  START=$SECONDS
  wait_closed "http://127.0.0.1:$FRONTEND_PORT/" 45
  wait_closed "http://127.0.0.1:$BACKEND_PORT/health/ready" 60
  sleep 2
  LEFT="$(listening_on $BACKEND_PORT) $(listening_on $FRONTEND_PORT)"
  LEFT=$(echo "$LEFT" | wc -w | tr -d ' ')
  echo "  仍在监听测试端口的进程数：$LEFT"
  [ "$LEFT" = "0" ] || FAILED=1
  [ $FAILED -eq 0 ] && rm -rf "$CONFIG"
  sleep 2
done

echo "  日志：$LOGDIR"
[ $FAILED -eq 0 ] && echo "生命周期检查通过" || echo "生命周期检查失败"
exit $FAILED
