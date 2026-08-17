import js from '@eslint/js'
import globals from 'globals'
import tseslint from 'typescript-eslint'
import pluginVue from 'eslint-plugin-vue'
import { defineConfig } from 'eslint/config'

export default defineConfig([
  {
    ignores: ['**/node_modules/**', '**/dist/**', '**/deploy/**']
  },
  {
    files: ['**/*.{js,mjs,cjs,ts,mts,cts,vue}'],
    plugins: { js },
    extends: ['js/recommended'],
    languageOptions: { globals: globals.browser }
  },
  tseslint.configs.recommended,
  pluginVue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser
      }
    }
  },
  {
    files: ['**/*.{ts,tsx,vue}'],
    rules: {
      // 允许在某些情况下使用 any，但给出警告而不是错误
      '@typescript-eslint/no-explicit-any': [
        'warn',
        {
          // 在以下情况下允许使用 any
          fixToUnknown: false, // 不自动修复为 unknown
          // 可以在特定变量名模式中允许 any
          ignoreRestArgs: true // 忽略 rest 参数
        }
      ],
      // 其他 TypeScript 相关规则的优化
      '@typescript-eslint/no-unused-vars': [
        'warn',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_'
        }
      ],
      '@typescript-eslint/ban-ts-comment': [
        'warn',
        {
          'ts-ignore': 'allow-with-description',
          'ts-nocheck': 'allow-with-description',
          'ts-check': 'allow-with-description'
        }
      ]
    }
  }
])
