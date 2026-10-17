#!/bin/bash
# 备份 MoviePilot 数据目录：数据库用 VACUUM INTO 取一致快照，其余文件原样打包。
# 用法：packaging/backup_data.sh [输出目录]     应用可以开着不用停。
set -u -o pipefail
CONFIG="${CONFIG_DIR:-$HOME/Library/Application Support/MoviePilot}"
OUT="${1:-$HOME/Documents}"
[ -d "$CONFIG" ] || { echo "找不到数据目录：$CONFIG"; exit 1; }
mkdir -p "$OUT"

STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
SNAP="$STAGE/user.db"
# VACUUM INTO 读一个一致性快照，绕开正在写的 -wal，也不会像直接拷贝那样拷出半截库。
/usr/bin/sqlite3 "$CONFIG/user.db" "VACUUM INTO '$SNAP'" || { echo "数据库快照失败"; exit 1; }
CHECK=$(/usr/bin/sqlite3 "$SNAP" "PRAGMA integrity_check;")
[ "$CHECK" = "ok" ] || { echo "数据库自检未通过：$CHECK"; exit 1; }

# 只收用户状态：日志、缓存、临时文件都能重新生成，不进备份。
KEEP=(app.env category.yaml cookies plugins plugin_transactions plugin_packages)
ITEMS=()
for name in "${KEEP[@]}"; do
  if [ -e "$CONFIG/$name" ]; then ITEMS+=("$name"); else echo "跳过（不存在）：$name"; fi
done

STAMP=$(date +%Y%m%d-%H%M)
ARCHIVE="$OUT/moviepilot-数据备份-$STAMP.tar.gz"
tar -czf "$ARCHIVE" -C "$CONFIG" "${ITEMS[@]}" -C "$STAGE" user.db || exit 1
shasum -a 256 "$ARCHIVE" | tee "$ARCHIVE.sha256"

echo "备份：${ARCHIVE}（$(du -h "$ARCHIVE" | cut -f1 | tr -d ' ') ）"
echo "内容：$(tar -tzf "$ARCHIVE" | awk -F/ '{print $1}' | sort -u | tr '\n' ' ')"
