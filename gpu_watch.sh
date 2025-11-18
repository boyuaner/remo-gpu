#!/usr/bin/env bash

# gpu_watch.sh
# -------------
# 使用 bash 解析 ~/.ssh/config 中的主机并周期性查询各主机 GPU 状态。

set -o pipefail
shopt -s nullglob

CONFIG_PATH="${HOME}/.ssh/config"
INTERVAL=5
CONNECT_TIMEOUT=10
CONCURRENCY=8
RUN_ONCE=false
REMOTE_CMD="nvidia-smi --query-gpu=index,name,temperature.gpu,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits"
IDENTITY_FILE=""
HOST_FILTER=()
SSH_OPTIONS=()
HOST_LIST=()
VISITED_FILES_LIST=()
RESULT_STATUS=()
RESULT_PAYLOAD=()
ACTIVE_HOSTS=()
SSH_ARGS=()

usage() {
  cat <<'EOF'
用法: gpu_watch.sh [选项]

选项:
  -c, --config PATH          指定 SSH 配置文件 (默认 ~/.ssh/config)
  -i, --interval SECONDS     刷新间隔，默认 5
  -t, --timeout SECONDS      ssh ConnectTimeout，默认 10
  -p, --concurrency N        并发 ssh 数，默认 8
  -H, --hosts h1,h2          仅监控指定 Host（逗号分隔）
  -r, --remote-command CMD   自定义远程命令
  -I, --identity-file PATH   指定私钥，相当于 ssh -i
  -o, --ssh-option OPT       额外 ssh 选项（例如 StrictHostKeyChecking=no，可多次给出）
  -n, --once                 只运行一次后退出
  -h, --help                 显示本帮助
EOF
}

trim() {
  local var="$1"
  var="${var#"${var%%[![:space:]]*}"}"
  var="${var%"${var##*[![:space:]]}"}"
  printf '%s' "$var"
}

safe_name() {
  printf '%s' "$1" | tr '/:@' '___'
}

contains_element() {
  local needle="$1"
  shift || true
  for element in "$@"; do
    [[ "$element" == "$needle" ]] && return 0
  done
  return 1
}

mark_visited() {
  local path="$1"
  VISITED_FILES_LIST+=("$path")
}

is_visited() {
  local path="$1"
  contains_element "$path" "${VISITED_FILES_LIST[@]}"
}

add_host() {
  local host="$1"
  [[ -z $host ]] && return
  [[ $host == *"*"* || $host == *"?"* ]] && return
  if ! contains_element "$host" "${HOST_LIST[@]}"; then
    HOST_LIST+=("$host")
  fi
}

parse_config_file() {
  local path="$1"
  [[ -z $path ]] && return
  case "$path" in
    "~"*) path="${path/#\~/$HOME}" ;;
  esac
  if [[ ! -f $path ]]; then
    return
  fi

  local dir base abs
  dir=$(cd "$(dirname "$path")" 2>/dev/null && pwd) || return
  base="$(basename "$path")"
  abs="${dir}/${base}"

  if is_visited "$abs"; then
    return
  fi
  mark_visited "$abs"

  while IFS= read -r raw || [[ -n $raw ]]; do
    raw="${raw%%#*}"
    raw="$(trim "$raw")"
    [[ -z $raw ]] && continue

    local key rest lower
    key="${raw%%[[:space:]]*}"
    rest="${raw#"$key"}"
    rest="$(trim "$rest")"
    lower=$(printf '%s' "$key" | tr 'A-Z' 'a-z')

    if [[ $lower == "include" && -n $rest ]]; then
      for pattern in $rest; do
        case "$pattern" in
          "~"*) pattern="${pattern/#\~/$HOME}" ;;
        esac
        local matches=($pattern)
        if [[ ${#matches[@]} -eq 0 ]]; then
          matches=("$pattern")
        fi
        for inc in "${matches[@]}"; do
          parse_config_file "$inc"
        done
      done
      continue
    fi

    if [[ $lower == "match" ]]; then
      continue
    fi

    if [[ $lower == "host" && -n $rest ]]; then
      for token in $rest; do
        add_host "$token"
      done
    fi
  done <"$abs"
}

parse_hosts() {
  parse_config_file "$CONFIG_PATH"
  if [[ ${#HOST_LIST[@]} -eq 0 ]]; then
    echo "未在 $CONFIG_PATH 中找到 Host 条目" >&2
    exit 1
  fi
  ACTIVE_HOSTS=()
  while IFS= read -r host; do
    [[ -z $host ]] && continue
    ACTIVE_HOSTS+=("$host")
  done < <(printf '%s\n' "${HOST_LIST[@]}" | LC_ALL=C sort -u)

  if [[ ${#HOST_FILTER[@]} -gt 0 ]]; then
    local -a filtered=()
    local -a missing=()
    local target
    for target in "${HOST_FILTER[@]}"; do
      local found=1
      for host in "${ACTIVE_HOSTS[@]}"; do
        if [[ $host == "$target" ]]; then
          filtered+=("$host")
          found=0
          break
        fi
      done
      if (( found )); then
        missing+=("$target")
      fi
    done
    ACTIVE_HOSTS=("${filtered[@]}")
    if [[ ${#missing[@]} -gt 0 ]]; then
      printf '警告: 未找到主机 %s\n' "${missing[*]}" >&2
    fi
  fi

  if [[ ${#ACTIVE_HOSTS[@]} -eq 0 ]]; then
    echo "没有可监控的主机，退出。" >&2
    exit 1
  fi
}

parse_host_filter() {
  local value="$1"
  IFS=',' read -r -a HOST_FILTER <<<"$value"
  local -a cleaned=()
  local item
  for item in "${HOST_FILTER[@]}"; do
    item="$(trim "$item")"
    [[ -z $item ]] && continue
    cleaned+=("$item")
  done
  HOST_FILTER=("${cleaned[@]}")
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -c|--config)
        CONFIG_PATH="$2"
        shift 2
        ;;
      -i|--interval)
        INTERVAL="$2"
        shift 2
        ;;
      -t|--timeout)
        CONNECT_TIMEOUT="$2"
        shift 2
        ;;
      -p|--concurrency)
        CONCURRENCY="$2"
        shift 2
        ;;
      -H|--hosts)
        parse_host_filter "$2"
        shift 2
        ;;
      -r|--remote-command)
        REMOTE_CMD="$2"
        shift 2
        ;;
      -I|--identity-file)
        IDENTITY_FILE="$2"
        shift 2
        ;;
      -o|--ssh-option)
        SSH_OPTIONS+=("$2")
        shift 2
        ;;
      -n|--once)
        RUN_ONCE=true
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "未知参数: $1" >&2
        usage
        exit 1
        ;;
    esac
  done

  [[ $INTERVAL =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "interval 必须为数字" >&2; exit 1; }
  [[ $CONNECT_TIMEOUT =~ ^[0-9]+$ ]] || { echo "timeout 必须为整数" >&2; exit 1; }
  [[ $CONCURRENCY =~ ^[0-9]+$ && $CONCURRENCY -gt 0 ]] || { echo "concurrency 必须为正整数" >&2; exit 1; }

  if [[ -n $IDENTITY_FILE ]]; then
    SSH_ARGS+=("-i" "$IDENTITY_FILE")
  fi
  for opt in "${SSH_OPTIONS[@]}"; do
    SSH_ARGS+=("-o" "$opt")
  done
}

fetch_host() {
  local host="$1"
  local out_file="$2"
  local err_file="$3"
  local status_file="$4"
  local cmd=(ssh -o BatchMode=yes -o ConnectTimeout="$CONNECT_TIMEOUT" "${SSH_ARGS[@]}" "$host" "$REMOTE_CMD")
  "${cmd[@]}" >"$out_file" 2>"$err_file"
  local rc=$?
  echo "$rc" >"$status_file"
}

run_batch() {
  local tmp_dir="$1"
  local host
  local -a pids=()
  local -a names=()
  local concurrency=${CONCURRENCY}
  if (( concurrency > ${#ACTIVE_HOSTS[@]} )); then
    concurrency=${#ACTIVE_HOSTS[@]}
  fi
  (( concurrency == 0 )) && concurrency=1

  for host in "${ACTIVE_HOSTS[@]}"; do
    local safe
    safe="$(safe_name "$host")"
    local out_file="${tmp_dir}/${safe}.out"
    local err_file="${tmp_dir}/${safe}.err"
    local status_file="${tmp_dir}/${safe}.status"
    fetch_host "$host" "$out_file" "$err_file" "$status_file" &
    pids+=($!)
    if (( ${#pids[@]} >= concurrency )); then
      wait "${pids[0]}" 2>/dev/null
      pids=("${pids[@]:1}")
    fi
  done
  for pid in "${pids[@]}"; do
    wait "$pid" 2>/dev/null
  done
}

collect_results() {
  local tmp_dir="$1"
  RESULT_STATUS=()
  RESULT_PAYLOAD=()
  local idx
  for idx in "${!ACTIVE_HOSTS[@]}"; do
    local host="${ACTIVE_HOSTS[$idx]}"
    local safe
    safe="$(safe_name "$host")"
    local status_file="${tmp_dir}/${safe}.status"
    local out_file="${tmp_dir}/${safe}.out"
    local err_file="${tmp_dir}/${safe}.err"
    local status="999"
    [[ -f $status_file ]] && read -r status <"$status_file"
    if [[ $status != "0" ]]; then
      local err=""
      [[ -s $err_file ]] && err=$(<"$err_file")
      err=${err:-"ssh exit $status"}
      err=$(printf '%s' "$err" | head -n 1)
      RESULT_STATUS[$idx]="ERR"
      RESULT_PAYLOAD[$idx]="$err"
    else
      local payload=""
      [[ -s $out_file ]] && payload=$(<"$out_file")
      RESULT_STATUS[$idx]="OK"
      RESULT_PAYLOAD[$idx]="$payload"
    fi
  done
}

render_table() {
  printf '%-20s %-4s %-22s %-6s %-18s %-8s %s\n' "Host" "GPU" "Name" "Util%" "Memory (MiB)" "Temp" "Status"
  printf '%-20s %-4s %-22s %-6s %-18s %-8s %s\n' "--------------------" "----" "----------------------" "------" "------------------" "--------" "------"
  local idx
  for idx in "${!ACTIVE_HOSTS[@]}"; do
    local host="${ACTIVE_HOSTS[$idx]}"
    local status="${RESULT_STATUS[$idx]}"
    local payload="${RESULT_PAYLOAD[$idx]}"
    [[ -z $status ]] && continue
    if [[ $status == "ERR" ]]; then
      printf '%-20s %-4s %-22s %-6s %-18s %-8s %s\n' "$host" "-" "-" "-" "-" "-" "$payload"
      continue
    fi
    if [[ -z $(trim "$payload") ]]; then
      printf '%-20s %-4s %-22s %-6s %-18s %-8s %s\n' "$host" "-" "-" "-" "-" "-" "No GPU"
      continue
    fi
    local first=true
    while IFS=',' read -r gpu_index gpu_name gpu_temp gpu_util mem_used mem_total _rest; do
      [[ -z $gpu_index ]] && continue
      gpu_index="$(trim "$gpu_index")"
      gpu_name="$(trim "$gpu_name")"
      gpu_temp="$(trim "$gpu_temp")"
      gpu_util="$(trim "$gpu_util")"
      mem_used="$(trim "$mem_used")"
      mem_total="$(trim "$mem_total")"
      [[ -z $gpu_util ]] && gpu_util="0"
      [[ -z $gpu_temp ]] && gpu_temp="0"
      local host_label=""
      if $first; then
        host_label="$host"
        first=false
      fi
      printf '%-20s %-4s %-22s %-6s %-18s %-8s %s\n' \
        "$host_label" "${gpu_index:-0}" "${gpu_name:-N/A}" "${gpu_util:-0}" \
        "${mem_used:-0}/${mem_total:-0}" "${gpu_temp:-0}°C" "OK"
    done <<<"$payload"
  done
}

clear_screen() {
  if [[ -t 1 ]]; then
    printf '\033[2J\033[H'
  fi
}

main_loop() {
  while true; do
    local tmp_dir
    tmp_dir=$(mktemp -d)
    run_batch "$tmp_dir"
    collect_results "$tmp_dir"
    rm -rf "$tmp_dir"

    clear_screen
    printf '[%s] 本地: %s  主机数: %d\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$(hostname)" "${#ACTIVE_HOSTS[@]}"
    render_table

    $RUN_ONCE && break
    sleep "$INTERVAL"
  done
}

main() {
  parse_args "$@"
  parse_hosts
  main_loop
}

main "$@"


