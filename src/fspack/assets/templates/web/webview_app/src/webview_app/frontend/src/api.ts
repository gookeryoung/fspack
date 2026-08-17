/**
 * PyWebView API - 用于与Python后端通信的接口
 */

declare global {
  interface Window {
    pywebview?: {
      api: {
        // 系统相关API
        get_system_info: () => Promise<Record<string, any>>

        // 应用控制API
        minimize_window: () => Promise<void>
        maximize_window: () => Promise<void>
        close_window: () => Promise<void>

        // 自定义业务API
        get_app_version: () => Promise<string>
      }
    }
  }
}

// API封装类
export class PyWebViewAPI {
  private static instance: PyWebViewAPI

  static getInstance(): PyWebViewAPI {
    if (!PyWebViewAPI.instance) {
      PyWebViewAPI.instance = new PyWebViewAPI()
    }
    return PyWebViewAPI.instance
  }

  private get api() {
    return window.pywebview?.api
  }

  // 检查API是否可用
  isAvailable(): boolean {
    return !!window.pywebview?.api
  }

  // 获取系统信息
  async getSystemInfo(): Promise<Record<string, any>> {
    if (this.isAvailable()) {
      return this.api!.get_system_info()
    }
    return { platform: 'web', userAgent: navigator.userAgent }
  }

  // 窗口控制
  async minimizeWindow(): Promise<void> {
    if (this.isAvailable()) {
      return this.api!.minimize_window()
    }
  }

  async maximizeWindow(): Promise<void> {
    if (this.isAvailable()) {
      return this.api!.maximize_window()
    }
  }

  async closeWindow(): Promise<void> {
    if (this.isAvailable()) {
      return this.api!.close_window()
    }
  }

  // 获取应用版本
  async getAppVersion(): Promise<string> {
    if (this.isAvailable()) {
      return this.api!.get_app_version()
    }
    return '1.0.0-web'
  }
}

export const api = PyWebViewAPI.getInstance()
