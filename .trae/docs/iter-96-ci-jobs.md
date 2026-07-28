# iter-96: CI 三 job 增强（Windows 矩阵 + slow-e2e cron + benchmark 门禁）

## 需求清单

- [x] test job 添加 `windows-latest` 矩阵，覆盖 Windows 路径/mingw/NSIS 流程
- [x] 新增 `slow-e2e` job，每周六 04:00 UTC cron 定时运行 slow 端到端测试，
      PR/push 不触发避免阻塞
- [x] 新增 `benchmark` job，每次 push 到 main 跑 pytest-benchmark，与基线对比
      退化 >10% 失败
- [x] 三 job 独立并行，避免拉长 PR 反馈时间
- [x] `.benchmarks/` 加入 `.gitignore`，从 git 索引移除本地基线（避免与 CI 基线混淆）
- [x] 全套门禁通过（ruff / pyrefly / pytest / coverage ≥ 95%）

## 迭代目标

对应 req-47 阶段 3「CI 与跨平台」的 iter-96 CI 三 job 增强项。原 `ci.yml`
仅 ubuntu-latest Python 3.8/3.14，无 Windows 矩阵；slow 端到端测试无 cron
定时运行，回归风险高；`pytest-benchmark` �线已建立但未纳入 CI 回归门禁。

本迭代扩展 CI 为 4 个 job：

1. **lint**（不变）：ubuntu-latest，ruff + pyrefly
2. **test**（扩展）：ubuntu-latest + windows-latest × Python 3.8/3.14 = 4 矩阵
3. **slow-e2e**（新增）：ubuntu-latest，cron `0 4 * * 6`，安装 mingw-w64 +
   wine，运行 slow 端到端测试
4. **benchmark**（新增）：ubuntu-latest，push to main 触发，pytest-benchmark
   + cache 基线 + 退化 >10% 门禁

## 改动文件清单

### 修改

- `.github/workflows/ci.yml`
  - `on:` 新增 `schedule: - cron: '0 4 * * 6'`（每周六 04:00 UTC）
  - `test` job：
    - `runs-on: ${{ matrix.os }}`（原 `ubuntu-latest`）
    - `matrix.os: [ubuntu-latest, windows-latest]`
    - `timeout-minutes: 20`（原 15，Windows runner 较慢）
    - job name 改为 `Test (${{ matrix.os }}, Python ${{ matrix.python-version }})`
  - 新增 `slow-e2e` job：
    - `if: github.event_name == 'schedule'`（仅 cron 触发）
    - `timeout-minutes: 45`（端到端构建耗时长）
    - 安装 mingw-w64 + wine + wine32（i386 架构支持）
    - 缓存 `~/.fspack/cache/`（runtime/wheels 下载缓存）
    - 运行 `pytest -m slow --cov=fspack --cov-report=term-missing -v`
  - 新增 `benchmark` job：
    - `if: github.event_name == 'push' && github.ref == 'refs/heads/main'`（仅 main push）
    - `timeout-minutes: 15`
    - `actions/cache` 缓存 `.benchmarks/` 目录，key 含 `github.run_id` 确保每次
      更新基线，`restore-keys` 回退到上次基线
    - 运行 `pytest tests/test_perf_baseline.py -m slow --benchmark-only
      --benchmark-save=main --benchmark-compare
      --benchmark-compare-fail=mean:10% --benchmark-min-rounds=10
      --benchmark-warmup=on`
    - `actions/upload-artifact` 上传 `.benchmarks/`（retention 30 天）
- `.gitignore`
  - 「测试与覆盖率」段新增 `.benchmarks/`
- `git rm --cached -r .benchmarks/`
  - 从 git 索引移除 `.benchmarks/Windows-CPython-3.11-64bit/0001_iter80-baseline.json`
    （本地硬件基线，与 CI runner 硬件不同，不应混用）

## 关键决策与依据

### Windows 矩阵：4 组合而非全量

矩阵为 `os: [ubuntu-latest, windows-latest] × python-version: ["3.8", "3.14"]`
= 4 组合。选择 3.8 + 3.14 而非全量 3.8~3.14 的原因：

- 3.8 是最低支持版本，验证兼容性边界
- 3.14 是最新稳定版，验证新特性
- 中间版本（3.9~3.13）变化平缓，4 组合已覆盖关键边界
- Windows runner 慢于 Linux，全量 7 × 2 = 14 组合会拉长 CI 时间

### slow-e2e 仅 cron 触发，PR 不触发

slow 端到端测试需要真实下载 embed python + mingw 编译 + wine 运行，单次
构建 2-5 分钟，9 类项目 + Linux 端到端测试总耗时 30-45 分钟。若 PR 触发会
严重阻塞反馈循环。cron 每周六 04:00 UTC（北京时间 12:00）运行，回归风险
可控。

### benchmark 仅 push to main 触发

benchmark 测试需 10-15 分钟，PR 触发会拉长反馈。仅 main 分支 push 触发，
确保主线性能稳定。PR 作者可本地跑 `pytest tests/test_perf_baseline.py -m slow
--benchmark-only` 验证。

### 基线缓存策略：cache + run_id

```yaml
key: benchmark-ubuntu-3.11-${{ github.run_id }}
restore-keys: |
  benchmark-ubuntu-3.11-
```

- `key` 含 `github.run_id`：每次运行创建新 cache entry（确保保存当前基线）
- `restore-keys` 回退到上次基线：`--benchmark-compare` 对比上次基线
- 首次运行无 cache：`--benchmark-compare` 无基线可对比，不失败（pytest-benchmark 行为）
- 后续运行有 cache：对比基线，`--benchmark-compare-fail=mean:10%` 退化 >10% 失败

### .benchmarks/ 从 git 移除

原 `.benchmarks/Windows-CPython-3.11-64bit/0001_iter80-baseline.json` 是
本地硬件基线，与 CI ubuntu-latest runner 硬件不同，对比会误报。移除 git
追踪后：

- 本地基线保留在工作区（不删除文件）
- CI 用 `actions/cache` 管理自己的基线
- 避免本地基线污染 CI 对比

## 代码实现情况

### test job 矩阵扩展

```yaml
test:
  name: Test (${{ matrix.os }}, Python ${{ matrix.python-version }})
  runs-on: ${{ matrix.os }}
  timeout-minutes: 20
  strategy:
    fail-fast: false
    matrix:
      os: [ubuntu-latest, windows-latest]
      python-version: ["3.8", "3.14"]
```

`fail-fast: false` 确保单个矩阵失败不取消其他组合，便于一次看到所有问题。

### slow-e2e job 条件触发

```yaml
slow-e2e:
  if: github.event_name == 'schedule'
```

`github.event_name == 'schedule'` 确保 push/PR 不触发该 job，仅 cron 触发。

### benchmark job 基线门禁

```yaml
benchmark:
  if: github.event_name == 'push' && github.ref == 'refs/heads/main'
  steps:
    - name: Restore benchmark baseline
      uses: actions/cache@v4
      with:
        path: .benchmarks/
        key: benchmark-ubuntu-3.11-${{ github.run_id }}
        restore-keys: |
          benchmark-ubuntu-3.11-
    - name: Run benchmark tests
      run: |
        uv run pytest tests/test_perf_baseline.py -m slow \
          --benchmark-only \
          --benchmark-save=main \
          --benchmark-compare \
          --benchmark-compare-fail=mean:10% \
          --benchmark-min-rounds=10 \
          --benchmark-warmup=on
```

## 整合优化情况

- 三个新/改 job 独立并行，lint/test 在 PR 上跑，slow-e2e/benchmark 不跑
- `actions/cache` 复用 fspack assets 缓存（slow-e2e）与 benchmark 基线缓存
- Windows 矩阵复用 release.yml 的 windows-latest runner 经验
- `.benchmarks/` 管理与 `.gitignore` 一致（与 `.coverage`/`.tox/` 同段）

## 测试验证结果

- `uv run ruff check src tests` — All checks passed
- `uv run ruff format --check src tests` — 98 files already formatted
- `uv run pyrefly check` — 0 errors (7 suppressed, 7 warnings)
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95` —
  1443 passed, 1 skipped, 32 deselected, coverage 97.61%
- YAML 语法验证：`yaml.safe_load` 解析通过，4 jobs（lint/test/slow-e2e/benchmark）
  triggers/matrix/if 条件均正确

## 遗留事项

- **Windows 矩阵首次运行风险**：Windows runner 上可能存在路径分隔符、PATH
  等兼容性问题，需首次 push 后观察并修复（iter-97 Linux 平台测试覆盖补强
  可一并处理 Windows 专属路径）
- **slow-e2e wine 稳定性**：GitHub Actions ubuntu-latest 上 wine 32 位支持
  可能不稳定（wine32 包依赖 i386 架构），首次 cron 运行后需观察
- **benchmark 基线首次建立**：首次 push to main 时无基线可对比，benchmark
  job 不失败；第二次 push 才开始对比门禁
- **req-47 阶段 3 剩余项**：iter-97 Linux 平台测试覆盖补强、iter-98 测试
  fixture 共享化、iter-99/100 macOS 支持

## 下一轮计划

iter-97：按 req-47 阶段 3 推进 Linux 平台测试覆盖补强。审查 tests/ 下所有
测试，识别 Windows 专属路径（embed python/`python3X._pth`/mingw 交叉编译/
`python.exe`）并补充 Linux 对等测试（standalone python/`libpython.so`/gcc
原生编译/`python3`）。目标：Linux 平台测试覆盖率从当前 ~40% 提升至 ≥80%。
