# crash-mcp

通过SSH连接远端机器，运行轻量级crash-agent进行内核崩溃分析。本地只需Python 3.8+，无需Go。

## 配置方法

### 1. 远端部署 (龙芯等)

```bash
# 编译
gcc -o crash-agent crash-agent.c
```

### 2. 配置Claude Code

编辑 `~/.config/claude-desktop/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "crash": {
      "command": "python3",
      "args": [
        "/path/to/crash_mcp.py",
        "--ssh",
        "user@loongson"
      ]
    }
  }
}
```

或分开指定：
```json
{
  "mcpServers": {
    "crash": {
      "command": "python3",
      "args": [
        "/path/to/crash_mcp.py",
        "--host", "loongson",
        "--user", "yourname",
        "--port", "22"
      ]
    }
  }
}
```

重启Claude Code后，crash工具会自动加载。

## 使用方法

在Claude Code中，直接用自然语言调用：

```
帮我初始化一个crash会话，vmlinux是/boot/vmlinux-5.15.0，vmcore是/var/crash/vmcore
```

或者更简单地描述：

```
用crash分析这个vmcore文件：/var/crash/vmcore，vmlinux是/boot/vmlinux-5.15.0
```

Claude会自动调用MCP工具。

### 手动调用MCP工具

如果你想直接调用工具，可以说：

```
使用init_crash工具初始化会话，vmlinux路径是/boot/vmlinux-5.15.0，vmcore路径是/var/crash/vmcore
```

```
使用execute工具执行bt命令
```

## MCP工具

| 工具 | 说明 | 参数 |
|------|------|------|
| `init_crash` | 初始化crash会话 | vmlinux, vmcore |
| `execute` | 执行crash命令 | session_id, command |
| `close` | 关闭会话 | session_id |
| `list_sessions` | 列出所有会话 | 无 |

## SSH多跳支持

利用已有的SSH配置：

```
# ~/.ssh/config
Host loongson
    HostName 192.168.x.x
    ProxyJump bastion1, bastion2
```

配置中使用别名即可。