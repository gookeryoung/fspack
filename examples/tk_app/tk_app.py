"""tkinter GUI 示例：验证 embed python 下 tkinter 内置库打包可用。

验证 iter-29 实现的 TkinterBundler：AST 检出 ``import tkinter`` 后，从
python-build-standalone Windows 构建提取 tkinter 组件（纯 Python 包 +
``_tkinter.pyd`` + Tcl/Tk 运行时脚本）补充到 embed python runtime。

wrapper 通过 ``TCL_LIBRARY``/``TK_LIBRARY`` 环境变量指定 Tcl/Tk 脚本路径，
使 ``_tkinter.pyd`` 能找到 ``tcl8.6/`` 与 ``tk8.6/`` 运行时库。
"""


def main() -> None:
    """创建 Tk 窗口显示标签，验证 tkinter 打包可用."""
    import tkinter as tk

    root = tk.Tk()
    label = tk.Label(root, text="hello from tkinter", font=("Arial", 16))
    label.pack(padx=50, pady=20)
    root.title("tkinter 示例")
    tk_version = root.tk.call("info", "patchlevel")
    print("hello from tkinter")
    print(f"Tk patchlevel: {tk_version}")

    # 无显示环境（CI/wine）定时自动退出避免挂起
    root.after(1000, root.destroy)

    root.mainloop()


if __name__ == "__main__":
    main()
