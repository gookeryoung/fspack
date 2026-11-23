"""$project_name 入口：tkinter 标准库 GUI 示例."""

import tkinter as tk
from tkinter import ttk


class MainWindow(tk.Tk):
    """主窗口：点击按钮计数."""

    def __init__(self) -> None:
        super().__init__()
        self.title("$project_name")
        self.geometry("400x200")
        self._count = 0

        self.label = ttk.Label(self, text="点击按钮 0 次", font=("Segoe UI", 12))
        self.label.pack(pady=30)

        self.button = ttk.Button(self, text="点我", command=self._on_click)
        self.button.pack()

    def _on_click(self) -> None:
        self._count += 1
        self.label.config(text=f"点击按钮 {self._count} 次")


def main() -> None:
    """启动 tkinter 应用."""
    app = MainWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
