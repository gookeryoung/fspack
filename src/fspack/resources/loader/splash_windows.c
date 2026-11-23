
/* ---- splash 启动画面（--splash 构建选项注入，默认构建不含此段）---- */
#define SPLASH_W 480
#define SPLASH_H 280
#define SPLASH_TIMEOUT_MS 30000
#define SPLASH_POLL_MS 150

static const wchar_t *g_splash_title = L"__FSPACK_SPLASH_TITLE__";
static HANDLE g_splash_event = NULL;

struct splash_enum_ctx { DWORD pid; HWND found; };

static BOOL CALLBACK splash_enum_cb(HWND hwnd, LPARAM lp) {
    /* 查找本进程内首个可见非工具窗口（GUI 应用主窗口首帧检测） */
    struct splash_enum_ctx *ctx = (struct splash_enum_ctx *)lp;
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    if (pid != ctx->pid) return TRUE;
    wchar_t cls[64];
    if (GetClassNameW(hwnd, cls, 64) == 0) return TRUE;
    if (wcscmp(cls, L"FspackSplash") == 0) return TRUE;
    if (!IsWindowVisible(hwnd)) return TRUE;
    if (GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW) return TRUE;
    ctx->found = hwnd;
    return FALSE;
}

static LRESULT CALLBACK splash_wndproc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC hdc = BeginPaint(hwnd, &ps);
        RECT rc;
        GetClientRect(hwnd, &rc);
        HBRUSH bg = CreateSolidBrush(RGB(24, 26, 32));
        FillRect(hdc, &rc, bg);
        DeleteObject(bg);
        SetTextColor(hdc, RGB(235, 237, 240));
        SetBkMode(hdc, TRANSPARENT);
        HFONT old_font = (HFONT)SelectObject(hdc, GetStockObject(DEFAULT_GUI_FONT));
        RECT rc_title = rc;
        DrawTextW(hdc, g_splash_title, -1, &rc_title,
                  DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS);
        RECT rc_hint = rc;
        rc_hint.top = rc.bottom - 52;
        DrawTextW(hdc, L"正在启动，请稍候...", -1, &rc_hint,
                  DT_CENTER | DT_SINGLELINE | DT_END_ELLIPSIS);
        SelectObject(hdc, old_font);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_ERASEBKGND:
        return 1; /* 背景在 WM_PAINT 统一绘制，避免闪烁 */
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

static DWORD WINAPI splash_thread(LPVOID param) {
    (void)param;
    HANDLE ev = g_splash_event;
    WNDCLASSW wc;
    ZeroMemory(&wc, sizeof(wc));
    wc.lpfnWndProc = splash_wndproc;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_APPSTARTING);
    wc.lpszClassName = L"FspackSplash";
    if (!RegisterClassW(&wc)) return 0;
    int sx = GetSystemMetrics(SM_CXSCREEN), sy = GetSystemMetrics(SM_CYSCREEN);
    HWND hwnd = CreateWindowExW(
        WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
        wc.lpszClassName, g_splash_title, WS_POPUP | WS_VISIBLE,
        (sx - SPLASH_W) / 2, (sy - SPLASH_H) / 2, SPLASH_W, SPLASH_H,
        NULL, NULL, wc.hInstance, NULL);
    if (!hwnd) return 0;
    UpdateWindow(hwnd);

    ULONGLONG t_start = GetTickCount64();
    for (;;) {
        DWORD wait = MsgWaitForMultipleObjects(1, &ev, FALSE, SPLASH_POLL_MS, QS_ALLINPUT);
        if (wait == WAIT_OBJECT_0) break; /* wrapper 通知关闭（WEB server 启动） */
        if (wait == WAIT_TIMEOUT) {
            /* 轮询进程内首个可见应用窗口（GUI 工具包通用首帧检测） */
            struct splash_enum_ctx ctx;
            ctx.pid = GetCurrentProcessId();
            ctx.found = NULL;
            EnumWindows(splash_enum_cb, (LPARAM)&ctx);
            if (ctx.found) break;
            if (GetTickCount64() - t_start >= SPLASH_TIMEOUT_MS) break; /* 超时兜底 */
            continue;
        }
        MSG msg;
        while (PeekMessageW(&msg, NULL, 0, 0, PM_REMOVE)) {
            if (msg.message == WM_QUIT) return 0;
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
    DestroyWindow(hwnd);
    return 0;
}

static void splash_show(void) {
    /* 命名事件 Local\\fspack_splash_<pid>：Python wrapper 经 ctypes
       OpenEventW+SetEvent 通知关闭（WEB 应用无自有窗口场景） */
    wchar_t name[64];
    _snwprintf(name, 64, L"Local\\fspack_splash_%lu", GetCurrentProcessId());
    g_splash_event = CreateEventW(NULL, TRUE, FALSE, name);
    if (!g_splash_event) return;
    HANDLE th = CreateThread(NULL, 0, splash_thread, NULL, 0, NULL);
    if (th) CloseHandle(th);
}

static void splash_close(void) {
    if (g_splash_event) SetEvent(g_splash_event);
}
