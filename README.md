# Remo-GPU

Remo-GPU 是一个面向多主机的 GPU 监控工具：自动解析 `~/.ssh/config`，通过 SSH 并发执行 `nvidia-smi`（或自定义命令），并以 Textual 卡片、Rich 表格、纯文本等方式展示指标。

---

## 🚀 Quick Start

```bash
# 临时运行（自动隔离环境）
uvx remo-gpu

# 或安装后使用
pip install remo-gpu
remo-gpu --interval 3
```

> 默认 UI 为 Textual；**确保本地 `ssh` 可用、远程主机安装 `nvidia-smi`**

---

## ✨ Features

- 解析 `~/.ssh/config` 及其 `Include` 指令，自动收集 Host 别名
- 并发 SSH 查询（默认最多 8 台），实时展示 GPU 利用率/显存/温度
- Textual 卡片式界面（默认）支持滚动、刷新 (`r`)、退出 (`q`)
- Rich 彩色表格与纯文本模式可任选
- 启动时探测不可连接主机，单独列出并跳过后续刷新
- 自定义刷新间隔、SSH 参数、远程命令 (`--remote-command`)

---

## 🧭 Requirements

1. Python ≥ 3.9
2. 本机 `ssh` 可用，远程机器可免密登录
3. 远程安装 `nvidia-smi`（或自定义命令）

---

## ⚙️ Common Options

```bash
python -m remo_gpu \
  --interval 3 \
  --timeout 8 \
  --hosts gpu-a gpu-b \
  --identity-file ~/.ssh/id_rsa \
  --ssh-option "-o StrictHostKeyChecking=no" \
  --ssh-option "-o UserKnownHostsFile=/dev/null"
```

- `--ui {textual,rich,plain}`：切换 UI；默认 textual
- `--interval` / `--timeout`：刷新间隔 / SSH 超时
- `--interval-once`：只输出一次
- `--no-clear`：纯文本模式禁用清屏
- `--remote-command`：替换 `nvidia-smi`

---

## 🖥 UI Modes

```bash
# Textual（默认，可滚动卡片）
remo-gpu --interval 2

# Plain 纯文本
remo-gpu --ui plain --interval 2 --no-clear
```

---

## 🔧 Bash Version

若只需最小依赖，可直接运行 `remo_gpu.sh`：

```bash
bash remo_gpu.sh \
  --interval 3 \
  --hosts gpu-a,gpu-b \
  --ssh-option StrictHostKeyChecking=no \
  --ssh-option UserKnownHostsFile=/dev/null
```

- `--ssh-option` 会被转换为 `ssh -o key=value`
- `--once` 仅输出一次结果
- 兼容 macOS 自带 bash 3.2（如需关联数组请安装新版 bash）


## 💡 Tips

- `.ssh/config` 中的 `Host *` 会被忽略，请为实际机器使用具体别名
- 首次连接提示指纹时，可临时加 `--ssh-option "-o StrictHostKeyChecking=no"`
- 调高刷新频率（更小 `--interval`）或增大 `--concurrency` 时注意远程负载

---

## 🤝 Contributing

欢迎提交 PR/Issue：可以扩展输出格式、增加 Prometheus 上报、或结合 `watch` / `tmux` / `nvitop` 等工具打造自定义面板。如果该项目对你有帮助，欢迎 Star ⭐️ 支持。

