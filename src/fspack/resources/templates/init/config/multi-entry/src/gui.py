"""$project_name GUI 入口：tkinter 主窗口."""

import tkinter as tk
from tkinter import ttk


def main() -> None:
    """GUI 入口：tkinter 主窗口显示问候."""
    root = tk.Tk()
    root.title("$project_name")
    root.geometry("300x150")

    label = ttk.Label(root, text="hello, $project_name!", font=("Segoe UI", 14))
    label.pack(pady=50)

    root.mainloop()


if __name__ == "__main__":
    main()
