/*
 * crash-agent.c - 轻量级Crash进程代理
 * 
 * 纯C实现，支持两种模式：
 * 1. TCP服务器模式: crash-agent --server --port 7890
 * 2. 命令行模式（供SSH调用）: crash-agent --init vmlinux vmcore
 * 
 * 编译方式：
 *   gcc -o crash-agent crash-agent.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <time.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <sys/wait.h>
#include <netinet/in.h>
#include <arpa/inet.h>

/* ============ 常量定义 ============ */
#define MAX_SESSIONS       64
#define MAX_LINE_SIZE      65536
#define MAX_COMMAND_SIZE   4096
#define CRASH_TIMEOUT      60
#define EXEC_TIMEOUT       300
#define SESSION_TIMEOUT    600
#define DEFAULT_PORT       7890
#define MAX_CLIENTS        10

/* ============ 错误码 ============ */
#define ERR_OK             0
#define ERR_PARSE          -1
#define ERR_SESSION_NOT_FOUND -2
#define ERR_INIT_FAILED    -3
#define ERR_TIMEOUT        -6
#define ERR_NO_MEMORY      -7

/* ============ 会话结构 ============ */
typedef struct session {
    char id[64];
    char vmlinux[512];
    char vmcore[512];
    int stdin_fd, stdout_fd;
    pid_t pid;
    time_t created, last_active;
    int active;
    struct session *next;
} session_t;

/* ============ 全局变量 ============ */
static session_t *sessions = NULL;
static int server_sock = -1;
static int running = 1;

/* ============ 函数声明 ============ */
static void signal_handler(int sig);
static int server_init(int port);
static void server_loop(void);
static int handle_client(int client_fd);
static session_t* session_create(const char *vmlinux, const char *vmcore);
static int session_exec(session_t *s, const char *cmd, char *output, size_t output_size);
static int session_close(session_t *s);
static session_t* session_find(const char *id);
static void session_cleanup(void);
static int write_all(int fd, const char *buf, size_t len);
static int read_line(int fd, char *buf, size_t max_len);
static void generate_uuid(char *buf);

/* ============ 信号处理 ============ */
static void signal_handler(int sig) {
    (void)sig;
    running = 0;
    if (server_sock >= 0) close(server_sock);
}

/* ============ UUID生成 ============ */
static void generate_uuid(char *buf) {
    struct timespec ts;
    static unsigned int counter = 0;
    clock_gettime(CLOCK_REALTIME, &ts);
    snprintf(buf, 64, "%08x%04x%04x%04x%04x%08x",
             (unsigned int)ts.tv_sec,
             (unsigned int)(ts.tv_nsec >> 16) & 0xFFFF,
             (unsigned int)getpid() & 0xFFFF,
             (unsigned int)(counter++) & 0xFFFF,
             (unsigned int)ts.tv_nsec & 0xFFFF,
             (unsigned int)(ts.tv_nsec >> 16) ^ (unsigned int)getpid());
}

/* ============ I/O工具 ============ */
static int write_all(int fd, const char *buf, size_t len) {
    size_t written = 0;
    while (written < len) {
        ssize_t n = write(fd, buf + written, len - written);
        if (n < 0) { if (errno == EINTR) continue; return -1; }
        written += n;
    }
    return 0;
}

static int read_line(int fd, char *buf, size_t max_len) {
    size_t pos = 0;
    while (pos < max_len - 1) {
        ssize_t n = read(fd, buf + pos, 1);
        if (n <= 0) { if (n < 0 && errno == EINTR) continue; buf[pos] = '\0'; return pos; }
        if (buf[pos] == '\n') { buf[pos] = '\0'; return pos; }
        pos++;
    }
    buf[max_len - 1] = '\0';
    return pos;
}

/* ============ 会话管理 ============ */
static session_t* session_create(const char *vmlinux, const char *vmcore) {
    session_t *s = malloc(sizeof(session_t));
    if (!s) return NULL;
    memset(s, 0, sizeof(session_t));
    
    generate_uuid(s->id);
    strncpy(s->vmlinux, vmlinux, sizeof(s->vmlinux) - 1);
    strncpy(s->vmcore, vmcore, sizeof(s->vmcore) - 1);
    
    int stdin_pipe[2], stdout_pipe[2];
    if (pipe(stdin_pipe) < 0 || pipe(stdout_pipe) < 0) { free(s); return NULL; }
    
    pid_t pid = fork();
    if (pid < 0) { free(s); return NULL; }
    
    if (pid == 0) {
        close(stdin_pipe[1]); close(stdout_pipe[0]);
        dup2(stdin_pipe[0], STDIN_FILENO);
        dup2(stdout_pipe[1], STDOUT_FILENO);
        close(stdin_pipe[0]); close(stdout_pipe[1]);
        execlp("stdbuf", "stdbuf", "-oL", "crash", vmlinux, vmcore, (char*)NULL);
        _exit(1);
    }
    
    close(stdin_pipe[0]); close(stdout_pipe[1]);
    s->pid = pid;
    s->stdin_fd = stdin_pipe[1];
    s->stdout_fd = stdout_pipe[0];
    s->created = s->last_active = time(NULL);
    s->active = 1;

    // 检查子进程是否立即退出（vmlinux/vmcore 路径无效等）
    usleep(100000);
    int wstatus;
    if (waitpid(pid, &wstatus, WNOHANG) == pid) {
        session_close(s);
        free(s);
        return NULL;
    }

    write_all(s->stdin_fd, "set scroll off\n", 15);

    // 等待 crash 初始化完成（读到第一个 crash> 提示符）
    // 大 vmcore 可能加载数分钟，超时设为 600 秒
    fd_set rfds;
    struct timeval tv;
    char buf[4096];
    time_t init_start = time(NULL);
    int ready = 0;
    while (!ready && (time(NULL) - init_start) < 600) {
        FD_ZERO(&rfds); FD_SET(s->stdout_fd, &rfds);
        tv.tv_sec = 1; tv.tv_usec = 0;
        if (select(s->stdout_fd + 1, &rfds, NULL, NULL, &tv) <= 0) continue;
        ssize_t n = read(s->stdout_fd, buf, sizeof(buf) - 1);
        if (n <= 0) continue;
        buf[n] = '\0';
        if (strstr(buf, "crash>")) ready = 1;
    }
    if (!ready) {
        session_close(s);
        free(s);
        return NULL;
    }

    s->next = sessions;
    sessions = s;
    return s;
}

static int session_exec(session_t *s, const char *cmd, char *output, size_t output_size) {
    if (!s || !s->active) return ERR_SESSION_NOT_FOUND;
    
    // 清空缓冲区
    fd_set rfds;
    struct timeval tv;
    while (1) {
        FD_ZERO(&rfds); FD_SET(s->stdout_fd, &rfds);
        tv.tv_sec = 0; tv.tv_usec = 10000;
        if (select(s->stdout_fd + 1, &rfds, NULL, NULL, &tv) <= 0) break;
        char buf[1024];
        if (read(s->stdout_fd, buf, sizeof(buf)) <= 0) break;
    }
    
    // 发送命令
    size_t cmd_len = strlen(cmd);
    if (cmd_len >= MAX_COMMAND_SIZE) cmd_len = MAX_COMMAND_SIZE - 1;
    
    char cmd_buf[MAX_COMMAND_SIZE + 2];
    memcpy(cmd_buf, cmd, cmd_len);
    cmd_buf[cmd_len] = '\n';
    cmd_buf[cmd_len + 1] = '\0';
    
    if (write_all(s->stdin_fd, cmd_buf, cmd_len + 1) < 0) return -1;
    
    // 读取输出
    size_t total = 0;
    int prompt_found = 0;
    time_t start = time(NULL);
    
    while (!prompt_found && (time(NULL) - start) < EXEC_TIMEOUT) {
        FD_ZERO(&rfds); FD_SET(s->stdout_fd, &rfds);
        tv.tv_sec = 1; tv.tv_usec = 0;
        if (select(s->stdout_fd + 1, &rfds, NULL, NULL, &tv) > 0) {
            char buf[4096];
            ssize_t n = read(s->stdout_fd, buf, sizeof(buf) - 1);
            if (n > 0) {
                buf[n] = '\0';
                char *pos = strstr(buf, "crash>");
                if (pos) {
                    size_t len = pos - buf;
                    if (total + len < output_size) memcpy(output + total, buf, len);
                    total += len;
                    prompt_found = 1;
                } else {
                    if (total + n < output_size) memcpy(output + total, buf, n);
                    total += n;
                }
            } else if (n == 0) break;
        }
    }
    
    if (total >= output_size) total = output_size - 1;
    output[total] = '\0';
    s->last_active = time(NULL);
    return (int)total;
}

static int session_close(session_t *s) {
    if (!s) return -1;
    s->active = 0;
    if (s->stdin_fd >= 0) { write_all(s->stdin_fd, "quit\n", 5); usleep(100000); close(s->stdin_fd); }
    if (s->stdout_fd >= 0) close(s->stdout_fd);
    if (s->pid > 0) { kill(s->pid, SIGTERM); usleep(100000); kill(s->pid, SIGKILL); waitpid(s->pid, NULL, 0); }
    return 0;
}

static session_t* session_find(const char *id) {
    session_t *s = sessions;
    while (s) { if (s->active && strcmp(s->id, id) == 0) return s; s = s->next; }
    return NULL;
}

static void session_cleanup(void) {
    time_t now = time(NULL);
    session_t **pp = &sessions;
    while (*pp) {
        session_t *s = *pp;
        if (!s->active || (now - s->last_active) > SESSION_TIMEOUT) {
            session_close(s);
            *pp = s->next;
            free(s);
        } else {
            pp = &s->next;
        }
    }
}

/* ============ TCP服务器模式 ============ */
static int handle_client(int client_fd) {
    char line[MAX_LINE_SIZE], req_id[64], type[64], payload[MAX_LINE_SIZE];

    fprintf(stderr, "[LOG] handle_client: waiting for request...\n");

    if (read_line(client_fd, line, sizeof(line)) <= 0) {
        fprintf(stderr, "[LOG] handle_client: read_line returned <=0, exiting\n");
        return -1;
    }
    fprintf(stderr, "[LOG] handle_client: received line=[%s]\n", line);

    char *p = line;
    char *pipe1 = strchr(p, '|');
    if (!pipe1) return -1;

    size_t id_len = pipe1 - p;
    if (id_len < 64) { strncpy(req_id, p, id_len); req_id[id_len] = '\0'; }

    char *pipe2 = strchr(pipe1 + 1, '|');
    if (!pipe2) return -1;

    size_t type_len = pipe2 - pipe1 - 1;
    if (type_len < 64) { strncpy(type, pipe1 + 1, type_len); type[type_len] = '\0'; }

    strcpy(payload, pipe2 + 1);

    fprintf(stderr, "[LOG] handle_client: type=[%s], req_id=[%s]\n", type, req_id);

    session_t *session;
    char response[MAX_LINE_SIZE];
    int status = ERR_OK;
    const char *output = "";

    if (strcmp(type, "init") == 0) {
        fprintf(stderr, "[LOG] handle_client: processing init request\n");
        char *vmlinux = payload;
        char *vmcore = strchr(payload, '|');
        if (vmcore) { *vmcore = '\0'; vmcore++; }
        session = session_create(vmlinux, vmcore ? vmcore : "");
        if (!session) { status = ERR_INIT_FAILED; output = "Failed"; }
        else { snprintf(response, sizeof(response), "%s|%s|%s", session->vmlinux, session->vmcore, session->id); output = response; }
    }
    else if (strcmp(type, "exec") == 0) {
        char *session_id = payload;
        char *cmd = strchr(payload, '|');
        if (cmd) { *cmd = '\0'; cmd++; }
        session = session_find(session_id);
        if (!session) { status = ERR_SESSION_NOT_FOUND; output = "Not found"; }
        else { char out[MAX_LINE_SIZE] = {0}; int ret = session_exec(session, cmd ? cmd : "", out, sizeof(out) - 1); if (ret < 0) status = ret; snprintf(response, sizeof(response), "%s", out); output = response; }
    }
    else if (strcmp(type, "close") == 0) {
        session = session_find(payload);
        if (session) session_close(session);
        output = "Closed";
    }
    else if (strcmp(type, "heartbeat") == 0) {
        session = session_find(payload);
        if (session) session->last_active = time(NULL);
    }
    else if (strcmp(type, "list") == 0) {
        int count = 0;
        session_t *s = sessions;
        while (s) { if (s->active) count++; s = s->next; }
        snprintf(response, sizeof(response), "%d", count);
        s = sessions;
        while (s) {
            if (s->active) { char entry[MAX_LINE_SIZE]; snprintf(entry, sizeof(entry), "|%s|%s|%s", s->id, s->vmlinux, s->vmcore); strcat(response, entry); }
            s = s->next;
        }
        output = response;
    }
    else { status = ERR_PARSE; output = "Unknown"; }

    fprintf(stderr, "[LOG] handle_client: done processing, status=%d\n", status);
    char resp[MAX_LINE_SIZE];
    snprintf(resp, sizeof(resp), "%s|%d|%s\n", req_id, status, output);
    fprintf(stderr, "[LOG] handle_client: sending response [%.100s]\n", resp);
    write_all(client_fd, resp, strlen(resp));
    fprintf(stderr, "[LOG] handle_client: response sent, returning\n");
    return 0;
}

static void server_loop(void) {
    printf("crash-agent listening on port %d\n", DEFAULT_PORT);
    fflush(stdout);
    
    while (running) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(server_sock, &rfds);
        
        struct timeval tv = {1, 0};
        int ready = select(server_sock + 1, &rfds, NULL, NULL, &tv);
        
        if (ready < 0) { if (errno == EINTR) continue; break; }
        if (ready == 0) { session_cleanup(); continue; }
        
        if (FD_ISSET(server_sock, &rfds)) {
            struct sockaddr_in client_addr;
            socklen_t addr_len = sizeof(client_addr);
            int client_fd = accept(server_sock, (struct sockaddr*)&client_addr, &addr_len);
            if (client_fd >= 0) {
                handle_client(client_fd);
                shutdown(client_fd, SHUT_RDWR);
                close(client_fd);
            }
        }
    }
}

static int server_init(int port) {
    int opt = 1;
    struct sockaddr_in addr;
    
    server_sock = socket(AF_INET, SOCK_STREAM, 0);
    if (server_sock < 0) { perror("socket"); return -1; }
    
    setsockopt(server_sock, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = INADDR_ANY;
    
    if (bind(server_sock, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("bind"); close(server_sock); return -1;
    }
    
    if (listen(server_sock, MAX_CLIENTS) < 0) {
        perror("listen"); close(server_sock); return -1;
    }
    
    return 0;
}

/* ============ 命令行模式（供SSH调用） ============ */
static int cmd_init(const char *vmlinux, const char *vmcore) {
    session_t *s = session_create(vmlinux, vmcore);
    if (!s) { printf("ERROR: Failed to create session\n"); return 1; }
    printf("%s|%s|%s\n", s->id, s->vmlinux, s->vmcore);
    return 0;
}

static int cmd_exec(const char *session_id, const char *cmd) {
    session_t *s = session_find(session_id);
    if (!s) { printf("ERROR: Session not found\n"); return 1; }
    
    char output[MAX_LINE_SIZE] = {0};
    int ret = session_exec(s, cmd, output, sizeof(output) - 1);
    if (ret < 0) { printf("ERROR: Exec failed\n"); return 1; }
    
    printf("%s\n", output);
    return 0;
}

static int cmd_close(const char *session_id) {
    session_t *s = session_find(session_id);
    if (!s) { printf("ERROR: Session not found\n"); return 1; }
    session_close(s);
    printf("OK\n");
    return 0;
}

static int cmd_list(void) {
    int count = 0;
    session_t *s = sessions;
    while (s) { if (s->active) count++; s = s->next; }
    
    printf("%d", count);
    s = sessions;
    while (s) {
        if (s->active) {
            printf("|%s|%s|%s", s->id, s->vmlinux, s->vmcore);
        }
        s = s->next;
    }
    printf("\n");
    return 0;
}

/* ============ 主函数 ============ */
int main(int argc, char *argv[]) {
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);
    signal(SIGPIPE, SIG_IGN);
    
    if (argc < 2) {
        fprintf(stderr, "Usage:\n");
        fprintf(stderr, "  TCP server mode:   %s --server [--port PORT]\n", argv[0]);
        fprintf(stderr, "  CLI mode (SSH):   %s --init VMLINUX VMCORE\n", argv[0]);
        fprintf(stderr, "                    %s --exec SESSION_ID COMMAND\n", argv[0]);
        fprintf(stderr, "                    %s --close SESSION_ID\n", argv[0]);
        fprintf(stderr, "                    %s --list\n", argv[0]);
        return 1;
    }
    
    if (strcmp(argv[1], "--server") == 0) {
        int port = DEFAULT_PORT;
        for (int i = 2; i < argc; i++) {
            if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
                port = atoi(argv[++i]);
            }
        }
        if (server_init(port) < 0) return 1;
        server_loop();
        return 0;
    }
    else if (strcmp(argv[1], "--init") == 0 && argc >= 4) {
        return cmd_init(argv[2], argv[3]);
    }
    else if (strcmp(argv[1], "--exec") == 0 && argc >= 4) {
        return cmd_exec(argv[2], argv[3]);
    }
    else if (strcmp(argv[1], "--close") == 0 && argc >= 3) {
        return cmd_close(argv[2]);
    }
    else if (strcmp(argv[1], "--list") == 0) {
        return cmd_list();
    }
    else if (strcmp(argv[1], "-h") == 0 || strcmp(argv[1], "--help") == 0) {
        printf("Usage:\n");
        printf("  TCP server:  %s --server [--port PORT]\n", argv[0]);
        printf("  CLI (SSH):   %s --init VMLINUX VMCORE\n", argv[0]);
        printf("               %s --exec SESSION_ID COMMAND\n", argv[0]);
        printf("               %s --close SESSION_ID\n", argv[0]);
        printf("               %s --list\n", argv[0]);
        return 0;
    }
    
    fprintf(stderr, "Unknown command\n");
    return 1;
}