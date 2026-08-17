import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'
import type { UserConfig } from 'vite'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import ElementPlus from 'unplugin-element-plus/vite'
import UnoCSS from 'unocss/vite'

// https://vite.dev/config/
export default defineConfig(() => {
  const baseConfig: UserConfig = {
    plugins: [
      vue(),
      UnoCSS(),
      ElementPlus({}), // 添加 Element Plus 插件以处理样式
      AutoImport({
        resolvers: [ElementPlusResolver()]
      }),
      Components({
        resolvers: [ElementPlusResolver()]
      })
    ],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url))
      }
    },
    build: {
      // 设置输出目录为 deploy
      outDir: 'deploy',
      // 优化的 chunk 分割策略 - 使用正则表达式配置
      rollupOptions: {
        output: {
          // 声明式 chunk 分割配置
          manualChunks: (id: string) => {
            // 非第三方库不分割
            if (!id.includes('node_modules')) {
              return undefined
            }

            // 使用正则表达式配置 - 更精确且易维护
            const chunkRules = [
              // Vue 生态系统
              { name: 'vue-vendor', pattern: /(?:^|\/)node_modules\/(?:vue|vue-router)/ },

              // UI 组件库
              { name: 'ui-vendor', pattern: /(?:^|\/)node_modules\/element-plus/ }
            ]

            // 按优先级匹配 - 避免循环依赖问题
            for (const rule of chunkRules) {
              if (rule.pattern.test(id)) {
                return rule.name
              }
            }

            // 其他第三方库
            return 'vendor'
          },
          // 优化 chunk 命名
          chunkFileNames: (chunkInfo: { facadeModuleId?: string | null }) => {
            const facadeModuleId = chunkInfo.facadeModuleId
            if (facadeModuleId) {
              const fileName = facadeModuleId.split(/[/\\]/).pop() || 'chunk'
              return `js/${fileName}-[hash].js`
            }
            return 'js/[name]-[hash].js'
          },

          // 静态资源命名 - 优化正则表达式性能
          assetFileNames: (assetInfo: { name?: string | null }) => {
            const assetName = assetInfo.name
            if (!assetName) {
              return 'assets/[name]-[hash][extname]'
            }

            // 使用更高效的文件类型检测
            const mediaExt = /\.(mp4|webm|ogg|mp3|wav|flac|aac)(\?.*)?$/i
            const imageExt = /\.(png|jpe?g|gif|svg)(\?.*)?$/i
            const fontExt = /\.(woff2?|eot|ttf|otf)(\?.*)?$/i

            if (mediaExt.test(assetName)) {
              return `media/[name]-[hash][extname]`
            }
            if (imageExt.test(assetName)) {
              return `images/[name]-[hash][extname]`
            }
            if (fontExt.test(assetName)) {
              return `fonts/[name]-[hash][extname]`
            }
            return `assets/[name]-[hash][extname]`
          }
        }
      },
      // 设置 chunk 大小警告限制
      chunkSizeWarningLimit: 1000,
      // 启用 CSS 代码分割
      cssCodeSplit: true,
      // 使用 esbuild 进行压缩（Vite 默认，更快且无需额外依赖）
      minify: 'esbuild'
    },
    server: {
      host: 'localhost', // 仅允许本地访问，提高安全性
      port: 5173,
      // 启用 CORS 以便 WebView 可以访问
      cors: {
        origin: true, // 允许所有来源
        credentials: true
      },
      // 监听文件变化 - 使用 Vite 默认的高效文件监听
      watch: {
        usePolling: true,
        // 如果必须使用轮询，建议间隔不低于 3000ms
        interval: 300
      }
    }
  }

  return baseConfig
})