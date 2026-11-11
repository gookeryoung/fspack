/* fspack 生成的 C loader —— 加载 python-build-standalone (macOS) 并运行用户入口脚本
   入口脚本路径从 <exe_basename>.entry 文件读取，回退 .entry（单入口兼容）
   与 Linux 版差异：用 _NSGetExecutablePath 取可执行路径（macOS 无 procfs），
   libpython 后缀为 .dylib，PATH_MAX 取自 sys/syslimits.h */
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/syslimits.h>
#include <mach-o/dyld.h>

#define LIBPYTHON "{libpython}"
#define PYTHONHOME "runtime/python"

/* loader 退出码规范（与 Windows/Linux 版一致，用户/CI 可据此精确诊断）：
   100=入口读取失败 101=Python 运行时加载失败 102=符号缺失 103=内存不足 */
#define ERR_ENTRY 100
#define ERR_DLL 101
#define ERR_SYM 102
#define ERR_MEM 103

typedef int (*Py_BytesMain_t)(int argc, char **argv);

static void write_log(const char *dir, const char *msg) {
    /* best-effort 追加写 <dir>/logs/loader.log：无终端（Finder 启动的 GUI
       会话）场景 stderr 不可见，日志文件是持久诊断通道；任何失败静默忽略 */
    char log_dir[PATH_MAX], log_path[PATH_MAX];
    snprintf(log_dir, sizeof(log_dir), "%s/logs", dir);
    mkdir(log_dir, 0755); /* 已存在时 EEXIST，忽略 */
    snprintf(log_path, sizeof(log_path), "%s/loader.log", log_dir);
    FILE *f = fopen(log_path, "a");
    if (!f) return;
    time_t t = time(NULL);
    struct tm tm_buf;
    if (localtime_r(&t, &tm_buf)) {
        char ts[32];
        strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tm_buf);
        fprintf(f, "[%s] %s\n", ts, msg);
    }
    fclose(f);
}

static void report_error(const char *dir, const char *msg) {
    /* 双通道输出：stderr（有终端时可见）+ 日志文件（持久兜底）。
       文案统一格式：[fspack loader] <阶段>: <原因>\n建议: <修复动作> */
    fprintf(stderr, "%s\n", msg);
    write_log(dir, msg);
}

static int verbose_enabled(void) {
    /* FSPACK_LOADER_VERBOSE=1 时输出各阶段耗时到 stderr，诊断启动性能；
       默认关闭，正常启动零开销（仅一次 getenv） */
    const char *v = getenv("FSPACK_LOADER_VERBOSE");
    return v != NULL && v[0] == '1' && v[1] == '\0';
}

static double now_ms(void) {
    /* 单调时钟毫秒：打点用，不受系统时间调整影响 */
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1000000.0;
}

static void set_dont_write_bytecode(void) {
    /* 发行目录常被杀软实时监控或挂载为只读，运行时 pyc 回写既慢又可能失败。
       禁用后每次 import 省去 __pycache__ 写尝试；用户已显式设置时不覆盖。 */
    if (getenv("PYTHONDONTWRITEBYTECODE") == NULL) {
        setenv("PYTHONDONTWRITEBYTECODE", "1", 0);
    }
}

static void split_exe(const char *exe_path, char *dir, size_t dir_cap, char *base, size_t base_cap) {
    char tmp[PATH_MAX];
    strncpy(tmp, exe_path, sizeof(tmp) - 1);
    tmp[sizeof(tmp) - 1] = '\0';
    char *slash = strrchr(tmp, '/');
    if (slash) {
        strncpy(base, slash + 1, base_cap - 1);
        base[base_cap - 1] = '\0';
        *slash = '\0';
        strncpy(dir, tmp, dir_cap - 1);
        dir[dir_cap - 1] = '\0';
    } else {
        dir[0] = '\0';
        strncpy(base, tmp, base_cap - 1);
        base[base_cap - 1] = '\0';
    }
}

static int read_entry(const char *exe_path, char *entry_out, size_t cap) {
    char dir[PATH_MAX], base[PATH_MAX], path[PATH_MAX], msg[PATH_MAX * 2];
    split_exe(exe_path, dir, sizeof(dir), base, sizeof(base));

    /* 多入口模式：<dir>/<base>.entry */
    snprintf(path, sizeof(path), "%s/%s.entry", dir, base);
    FILE *f = fopen(path, "r");
    if (!f) {
        /* 单入口模式回退：<dir>/.entry */
        snprintf(path, sizeof(path), "%s/.entry", dir);
        f = fopen(path, "r");
        if (!f) {
            snprintf(msg, sizeof(msg),
                     "[fspack loader] 读取入口失败: 找不到 %s/%s.entry 或 %s/.entry\n"
                     "建议: 发布包不完整，请重新安装或联系发布者", dir, base, dir);
            report_error(dir, msg);
            return ERR_ENTRY;
        }
    }
    if (!fgets(entry_out, (int)cap, f)) {
        fclose(f);
        report_error(dir, "[fspack loader] 读取入口失败: 入口文件内容为空\n建议: 发布包损坏，请重新安装");
        return ERR_ENTRY;
    }
    fclose(f);
    size_t n = strlen(entry_out);
    while (n > 0 && (entry_out[n-1] == '\n' || entry_out[n-1] == '\r')) {
        entry_out[--n] = '\0';
    }
    if (n == 0) {
        report_error(dir, "[fspack loader] 读取入口失败: 入口路径无效\n建议: 发布包损坏，请重新安装");
        return ERR_ENTRY;
    }
    return 0;
}

static int get_exe_path(char *buf, size_t cap) {
    /* macOS 用 _NSGetExecutablePath 获取当前可执行文件路径，返回绝对路径 */
    uint32_t size = (uint32_t)cap;
    if (_NSGetExecutablePath(buf, &size) != 0) {
        fprintf(stderr, "无法获取可执行文件路径（buffer 不足，需 %u 字节）\n", size);
        return 1;
    }
    return 0;
}

int main(int argc, char **argv) {
    char exe_path[PATH_MAX], dir[PATH_MAX];
    if (get_exe_path(exe_path, sizeof(exe_path)) != 0) {
        report_error(".", "[fspack loader] 定位可执行文件失败: 无法获取可执行文件路径\n建议: 请从正常路径启动");
        return ERR_ENTRY;
    }
    strncpy(dir, exe_path, sizeof(dir) - 1);
    dir[sizeof(dir) - 1] = '\0';
    char *slash = strrchr(dir, '/');
    if (slash) *slash = '\0';

    int verbose = verbose_enabled();
    double t_start = now_ms();

    char lib[PATH_MAX], entry[PATH_MAX], home[PATH_MAX], entry_full[PATH_MAX * 2], msg[PATH_MAX * 2];
    snprintf(lib, sizeof(lib), "%s/%s", dir, LIBPYTHON);
    snprintf(home, sizeof(home), "%s/%s", dir, PYTHONHOME);

    set_dont_write_bytecode();

    if (read_entry(exe_path, entry, sizeof(entry)) != 0) {
        return ERR_ENTRY;
    }
    if (verbose) {
        fprintf(stderr, "[fspack loader] read_entry 耗时 %.1fms\n", now_ms() - t_start);
    }
    snprintf(entry_full, sizeof(entry_full), "%s/%s", dir, entry);

    setenv("PYTHONHOME", home, 1);

    /* RTLD_LAZY：libpython 符号表庞大，惰性绑定让启动期跳过全量重定位
       （CPython 官方可执行文件同款加载方式），首次调用各符号时才解析 */
    double t_dll = now_ms();
    void *h = dlopen(lib, RTLD_LAZY | RTLD_GLOBAL);
    if (!h) {
        snprintf(msg, sizeof(msg),
                 "[fspack loader] 加载 Python 运行时失败: %s\n"
                 "原因: %s\n"
                 "建议: 检查 runtime 目录是否完整，或联系发布者", lib, dlerror());
        report_error(dir, msg);
        return ERR_DLL;
    }
    if (verbose) {
        fprintf(stderr, "[fspack loader] dlopen %s 耗时 %.1fms\n", LIBPYTHON, now_ms() - t_dll);
    }
    Py_BytesMain_t py_main = (Py_BytesMain_t)dlsym(h, "Py_BytesMain");
    if (!py_main) {
        report_error(dir, "[fspack loader] Python 运行时异常: 未找到 Py_BytesMain 符号\n建议: runtime 目录与发布工具版本不匹配，请重新安装");
        return ERR_SYM;
    }

    char **new_argv = (char **)malloc(sizeof(char *) * (argc + 2));
    if (!new_argv) {
        report_error(dir, "[fspack loader] 内存不足: 分配参数缓冲区失败\n建议: 关闭其他程序后重试");
        return ERR_MEM;
    }
    new_argv[0] = argv[0];
    new_argv[1] = entry_full;
    for (int i = 1; i < argc; i++) new_argv[1 + i] = argv[i];
    new_argv[argc + 1] = NULL;
    if (verbose) {
        fprintf(stderr, "[fspack loader] loader 总耗时 %.1fms（进入 Python）\n", now_ms() - t_start);
    }
    return py_main(argc + 1, new_argv);
}
