// trace_shim.c — LD_PRELOAD shim that captures every file open/write
// + every TCP connect + every getaddrinfo call made by the dlopen'd
// libbambu_networking.so (under qemu-x86_64).
//
// Build: zig cc -target x86_64-linux-gnu -O1 -shared -fPIC trace_shim.c -ldl -o trace_shim.so
//
// Use:   BAMBU_TRACE_LOG=/path/to/trace.log
//        LD_PRELOAD=$PWD/trace_shim.so qemu-x86_64 ... ./dump_driver ...
//
// What we want to see:
//   1. [open]    /tmp/bambu_certs/<x2d_serial>.crt       ← our prize
//   2. [open]    /tmp/bambu_certs/<x2d_serial>.key       ← also prize
//   3. [write]   the bytes of cert + key as they're created
//   4. [connect] api.bambulab.com:443 (cert-install endpoint)
//   5. [getaddr] api.bambulab.com → <ip>
//
// We log to BAMBU_TRACE_LOG (file path) — defaults to stderr — so the
// host's stdout stays clean.

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <dlfcn.h>
#include <pthread.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <sys/uio.h>
#include <errno.h>

static FILE* g_trace = NULL;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;

__attribute__((constructor))
static void trace_init(void) {
    const char* p = getenv("BAMBU_TRACE_LOG");
    if (p) {
        g_trace = fopen(p, "a");
        if (!g_trace) g_trace = stderr;
    } else {
        g_trace = stderr;
    }
    setvbuf(g_trace, NULL, _IOLBF, 0);
    fprintf(g_trace, "[trace] shim loaded (pid=%d)\n", getpid());
}

static void log_fmt(const char* fmt, ...) {
    if (!g_trace) return;
    pthread_mutex_lock(&g_lock);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_trace, fmt, ap);
    va_end(ap);
    pthread_mutex_unlock(&g_lock);
}

static void hex_dump(const char* prefix, const void* data, size_t n) {
    if (!g_trace) return;
    pthread_mutex_lock(&g_lock);
    const unsigned char* p = (const unsigned char*)data;
    size_t cap = n > 256 ? 256 : n;
    fprintf(g_trace, "%s (%zu bytes, showing %zu):\n", prefix, n, cap);
    for (size_t i = 0; i < cap; ++i) {
        fprintf(g_trace, "%02x", p[i]);
        if ((i & 0x1f) == 0x1f) fputc('\n', g_trace);
        else if ((i & 7) == 7)  fputc(' ', g_trace);
    }
    fputc('\n', g_trace);
    // Print printable preview
    fprintf(g_trace, "  ascii: ");
    for (size_t i = 0; i < cap; ++i) {
        char c = p[i];
        fputc((c >= 32 && c < 127) ? c : '.', g_trace);
    }
    fputc('\n', g_trace);
    pthread_mutex_unlock(&g_lock);
}

// ---------- file I/O ----------

int open(const char* path, int flags, ...) {
    static int (*real)(const char*, int, ...) = NULL;
    if (!real) real = (int(*)(const char*, int, ...))dlsym(RTLD_NEXT, "open");
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    int fd = real(path, flags, mode);
    if (path) log_fmt("[open ] %s -> fd=%d (flags=0x%x)\n", path, fd, flags);
    return fd;
}

int openat(int dirfd, const char* path, int flags, ...) {
    static int (*real)(int, const char*, int, ...) = NULL;
    if (!real) real = (int(*)(int, const char*, int, ...))dlsym(RTLD_NEXT, "openat");
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    int fd = real(dirfd, path, flags, mode);
    if (path) log_fmt("[openat] dirfd=%d %s -> fd=%d (flags=0x%x)\n",
                       dirfd, path, fd, flags);
    return fd;
}

int creat(const char* path, mode_t mode) {
    static int (*real)(const char*, mode_t) = NULL;
    if (!real) real = (int(*)(const char*, mode_t))dlsym(RTLD_NEXT, "creat");
    int fd = real(path, mode);
    if (path) log_fmt("[creat] %s -> fd=%d\n", path, fd);
    return fd;
}

// ---------- anti-debug spoofing ----------
//
// Bambu's plugin polls /proc/self/status and /proc/<PPID>/cmdline
// looking for "tracer" indicators. Under qemu-x86_64 the parent
// cmdline contains "qemu-x86_64" + "LD_PRELOAD" + the dylib path,
// which trips the anti-debug → connect_server returns -2 early.
// Spoof both files with sanitized content via fopen interposition.

static FILE* sanitized_status(void) {
    // /proc/self/status with TracerPid:0 and a benign Name
    const char* body =
        "Name:\tbambu-studio\n"
        "State:\tR (running)\n"
        "Tgid:\t1234\n"
        "Pid:\t1234\n"
        "PPid:\t1\n"
        "TracerPid:\t0\n"
        "Uid:\t1000\t1000\t1000\t1000\n"
        "Gid:\t1000\t1000\t1000\t1000\n"
        "FDSize:\t256\n"
        "VmSize:\t  120000 kB\n"
        "VmRSS:\t   80000 kB\n";
    FILE* f = fmemopen((void*)body, strlen(body), "r");
    return f;
}

static FILE* sanitized_cmdline(void) {
    // /proc/<PPID>/cmdline — looks like BambuStudio launched normally.
    // NUL-separated argv ending with NUL.
    static char body[] = "bambu-studio\0--no-bg\0\0";
    FILE* f = fmemopen((void*)body, sizeof body - 1, "r");
    return f;
}

FILE* fopen(const char* path, const char* mode) {
    static FILE* (*real)(const char*, const char*) = NULL;
    if (!real) real = (FILE*(*)(const char*, const char*))dlsym(RTLD_NEXT, "fopen");
    if (path && getenv("BAMBU_SPOOF_PROC")) {
        if (strcmp(path, "/proc/self/status") == 0) {
            log_fmt("[fopen-SPOOF] /proc/self/status\n");
            return sanitized_status();
        }
        // /proc/<n>/cmdline path — match any PID
        if (strncmp(path, "/proc/", 6) == 0 &&
            strlen(path) > 6 &&
            strstr(path, "/cmdline")) {
            log_fmt("[fopen-SPOOF] %s\n", path);
            return sanitized_cmdline();
        }
    }
    FILE* f = real(path, mode);
    if (path) log_fmt("[fopen] %s mode=%s -> %p\n", path, mode, (void*)f);
    return f;
}

ssize_t write(int fd, const void* buf, size_t count) {
    static ssize_t (*real)(int, const void*, size_t) = NULL;
    if (!real) real = (ssize_t(*)(int, const void*, size_t))dlsym(RTLD_NEXT, "write");
    if (fd >= 3 && count > 0 && count < 64*1024 && g_trace) {
        log_fmt("[write] fd=%d count=%zu\n", fd, count);
        // Only dump if it looks like text/PEM/JSON
        const char* p = (const char*)buf;
        if (count > 16 && (p[0] == '-' || p[0] == '{' || p[0] == 'M')) {
            hex_dump("[write data]", buf, count);
        }
    }
    return real(fd, buf, count);
}

// ---------- networking ----------

int connect(int sockfd, const struct sockaddr* addr, socklen_t addrlen) {
    static int (*real)(int, const struct sockaddr*, socklen_t) = NULL;
    if (!real) real = (int(*)(int, const struct sockaddr*, socklen_t))dlsym(RTLD_NEXT, "connect");
    if (addr) {
        if (addr->sa_family == AF_INET) {
            const struct sockaddr_in* s = (const struct sockaddr_in*)addr;
            char buf[INET_ADDRSTRLEN] = {0};
            inet_ntop(AF_INET, &s->sin_addr, buf, sizeof(buf));
            log_fmt("[connect] sock=%d %s:%d\n", sockfd, buf, ntohs(s->sin_port));
        } else if (addr->sa_family == AF_INET6) {
            const struct sockaddr_in6* s = (const struct sockaddr_in6*)addr;
            char buf[INET6_ADDRSTRLEN] = {0};
            inet_ntop(AF_INET6, &s->sin6_addr, buf, sizeof(buf));
            log_fmt("[connect6] sock=%d [%s]:%d\n", sockfd, buf, ntohs(s->sin6_port));
        }
    }
    return real(sockfd, addr, addrlen);
}

int getaddrinfo(const char* node, const char* service,
                const struct addrinfo* hints, struct addrinfo** res) {
    static int (*real)(const char*, const char*, const struct addrinfo*, struct addrinfo**) = NULL;
    if (!real) real = (int(*)(const char*, const char*, const struct addrinfo*, struct addrinfo**))dlsym(RTLD_NEXT, "getaddrinfo");
    log_fmt("[getaddrinfo] node=%s service=%s\n",
            node ? node : "(null)", service ? service : "(null)");
    return real(node, service, hints, res);
}

ssize_t send(int s, const void* buf, size_t len, int flags) {
    static ssize_t (*real)(int, const void*, size_t, int) = NULL;
    if (!real) real = (ssize_t(*)(int, const void*, size_t, int))dlsym(RTLD_NEXT, "send");
    if (len > 0 && len < 4096 && g_trace) {
        log_fmt("[send  ] sock=%d len=%zu flags=0x%x\n", s, len, flags);
        // First 200 bytes only
        size_t cap = len > 200 ? 200 : len;
        const char* p = (const char*)buf;
        if (p[0] == 'G' || p[0] == 'P' || p[0] == 'H') { // GET/POST/HEAD
            log_fmt("[send  ] body[:cap]=");
            for (size_t i = 0; i < cap; ++i)
                fputc((p[i] >= 32 && p[i] < 127) || p[i] == '\n' || p[i] == '\r' ? p[i] : '.', g_trace);
            fputc('\n', g_trace);
        }
    }
    return real(s, buf, len, flags);
}
