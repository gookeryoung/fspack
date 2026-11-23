import { defineConfig } from 'unocss'
import {
  presetAttributify,
  presetTypography,
  presetWind3,
  transformerDirectives,
  transformerVariantGroup,
  transformerCompileClass
} from 'unocss'

export default defineConfig({
  presets: [
    presetWind3(),
    presetAttributify(), // 启用属性化模式，如 <div flex="~ items-center">
    presetTypography() // 排版预设，提供更好的文本样式
  ],
  transformers: [
    transformerDirectives(), // 支持 @apply 等指令
    transformerVariantGroup(), // 支持分组语法，如 hover:(bg-red-500 text-white)
    transformerCompileClass() // 编译时优化CSS类
  ],
  shortcuts: [
    // 布局相关快捷方式
    ['btn', 'px-4 py-2 rounded inline-block bg-blue-600 text-white cursor-pointer hover:bg-blue-700'],
    ['flex-center', 'flex items-center justify-center'],
    ['flex-between', 'flex items-center justify-between'],
    ['card', 'border border-gray-200 rounded-md p-4 shadow-sm hover:shadow-md'],
    ['text-normal', 'text-sm text-gray-600 dark:text-gray-400 overflow-hidden text-ellipsis flex-grow-1'],
    ['text-bold', 'text-normal font-bold'],
    ['text-muted', 'text-xs text-blue-500 font-bold dark:text-gray-300'],
    // 项目特定的快捷方式
    ['demo-section', 'p-6 border border-gray-200 rounded-lg mb-4'],
    ['button-group', 'flex flex-wrap gap-2']
  ],
  theme: {
    colors: {
      primary: {
        DEFAULT: '#409eff',
        light: '#ecf5ff',
        dark: '#3a8ee6'
      },
      success: {
        DEFAULT: '#67c23a',
        light: '#f0f9eb',
        dark: '#5daf34'
      },
      warning: {
        DEFAULT: '#e6a23c',
        light: '#fdf6ec',
        dark: '#cf9236'
      },
      danger: {
        DEFAULT: '#f56c6c',
        light: '#fef0f0',
        dark: '#dd6161'
      },
      info: {
        DEFAULT: '#909399',
        light: '#f4f4f5',
        dark: '#a8abb2'
      }
    }
  },
  // 文件扫描配置 - 优化性能
  content: {
    pipeline: {
      include: [
        // 精确匹配项目文件类型，提升扫描速度
        /\.(vue|ts|js|tsx|jsx)($|\?)/,
        // 明确指定项目源码目录
        'src/**/*.{vue,ts,js,tsx,jsx}',
        // HTML入口文件
        'index.html'
      ],
      // 排除不必要的目录，减少扫描时间
      exclude: [
        'node_modules/**',
        '.git/**',
        'dist/**',
        'deploy/**',
        'build/**',
        '.nuxt/**',
        '.output/**',
        '.temp/**',
        '.cache/**',
        'coverage/**'
      ]
    }
  }
})
