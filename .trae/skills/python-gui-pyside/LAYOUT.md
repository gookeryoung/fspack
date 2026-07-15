# 四区主窗口布局规范

规定 Header/Sidebar/Content/Status 四区的结构、尺寸、交互与状态机；通用色彩比例、组件设计、交互模式见 `UI-DESIGN.md`，设计令牌定义见 `SKILL.md`。

## 一、结构总览

四区由顶层 `QVBoxLayout`（margin=0, spacing=0）自上而下组装；中区用 `QSplitter` 横向切分侧边栏与内容区。整体层级：

```
QMainWindow
└─ central (QWidget, QVBoxLayout, margin=0, spacing=0)
   ├─ header_bar (QFrame#header_bar)            ← Header
   ├─ tab_stack (QStackedWidget)                ← 顶层 Tab 切换层
   │  └─ <功能域页面> (QWidget, QVBoxLayout)
   │     └─ sidebar_splitter (QSplitter, Horizontal, handleWidth=4)
   │        ├─ sidebar (QListWidget#sidebar)    ← Sidebar
   │        └─ main_stack (QStackedWidget#main_stack)  ← Content
   │           ├─ setup_page
   │           ├─ running_page
   │           └─ results_page
   └─ statusBar()                               ← Status（QMainWindow 自带）
```

要点：

- **双层 QStackedWidget**：`tab_stack` 切换顶层功能域，`main_stack` 切换功能域内的工作流阶段。两层职责分离，避免单层页面数过多。
- **QSplitter 而非固定布局**：侧边栏与内容区、列表与详情均用 `QSplitter`，允许用户拖拽调整比例，禁用 `setGeometry` 硬编码几何。
- **零边距根布局**：`central_layout` 的 margin 与 spacing 均为 0，四区紧贴；区间分隔由各区自带边框/背景色承担。
- **minimumSize 兜底**：主窗口 `setMinimumSize(800, 600)`，低于此值触发滚动条而非压缩控件。

## 二、Header（头部栏）

### 2.1 职责

承载顶层 Tab 切换（功能域导航）与全局辅助操作（设置、关于）。不承载业务表单或数据展示。

### 2.2 结构与尺寸

| 属性 | 值 | 令牌 |
|------|-----|------|
| 容器 | `QFrame#header_bar`，`QHBoxLayout` | — |
| 高度 | 固定 40px（min=max） | `HEADER_HEIGHT` |
| 背景 | 主色 | `COLOR_PRIMARY` |
| 文字/图标 | 主色上文字（白） | `COLOR_TEXT_ON_PRIMARY` |
| 左右内边距 | 8px | `SPACING_SM` |
| 上下内边距 | 4px | `SPACING_XS` |
| 间距 | 4px | `SPACING_XS` |

### 2.3 内容布局

```
[Tab 按钮 1] [Tab 按钮 2] [Tab 按钮 3] <弹簧> [设置] [关于]
```

- 左侧：`checkable` `QPushButton` 组，通过 `QButtonGroup(exclusive=True)` 互斥；选中态用 `setChecked(True)`。
- 中间：`QSpacerItem` 水平弹簧填充剩余空间。
- 右侧：普通 `QPushButton`（设置、关于等）。

### 2.4 状态样式（QSS）

| 状态 | 背景 | 装饰 |
|------|------|------|
| 默认 | transparent | 无 |
| hover | `rgba(255,255,255,0.15)` | 无 |
| checked（选中） | `COLOR_PRIMARY_DARK` | 底部 3px `COLOR_ACCENT` 下划线（padding-bottom 减 1px 补偿） |
| disabled | transparent，文字变暗 | 无 |

### 2.5 交互

- 点击 Tab 按钮 → `QButtonGroup.idClicked` 信号 → `_on_tab_changed(tab_id)` → `tab_stack.setCurrentIndex(tab_id)`。
- 选中态由 `QButtonGroup` 自动维护，无需手动 `setChecked`。
- 非主任务 Tab 隐藏侧边栏：`sidebar.setVisible(tab_id == 0)`。

## 三、Sidebar（侧边栏）

### 3.1 职责

在当前功能域内切换工作流阶段（如主任务 Tab 下的 配置→执行中→结果）。仅承载阶段导航项，不承载业务操作按钮。

### 3.2 结构与尺寸

| 属性 | 值 | 令牌 |
|------|-----|------|
| 容器 | `QListWidget#sidebar` | — |
| 宽度 | min 160 / max 280 / 初始 220 | `SIDEBAR_WIDTH` |
| 背景 | 主色 | `COLOR_PRIMARY` |
| 文字/图标 | 主色上文字（白） | `COLOR_TEXT_ON_PRIMARY` |
| 右边框 | 1px `COLOR_PRIMARY_DARK` | — |
| 项内边距 | 8px 上下 / 16px 左右 | `SPACING_SM` / `SPACING_MD` |
| 项最小高度 | 32px | `CONTROL_HEIGHT` |
| 左侧选中条 | 3px 透明→`COLOR_ACCENT` | — |

### 3.3 图标着色策略

侧边栏为主色深色背景，图标须用白色变体：

```python
icon_on_primary = load_themed_icon(icon_path, theme.COLOR_TEXT_ON_PRIMARY)
item = QListWidgetItem(icon_on_primary, "配置")
```

主色背景上禁止使用主色图标（对比度不足）。

### 3.4 状态样式（QSS）

| 状态 | 背景 | 左侧条 |
|------|------|--------|
| 默认 | transparent | 3px transparent |
| hover | `rgba(255,255,255,0.1)` | 3px transparent |
| selected | `COLOR_PRIMARY_DARK` | 3px `COLOR_ACCENT` |

### 3.5 交互

- `currentRowChanged` → `_on_sidebar_changed(row)` → 映射 row 到阶段枚举 → `_switch_stage(stage)`。
- `_switch_stage` 同步反向更新侧边栏选中项时，须 `blockSignals(True)` 包裹避免循环触发：

```python
self.sidebar.blockSignals(True)
self.sidebar.setCurrentRow(page_index)
self.sidebar.blockSignals(False)
```

### 3.6 可见性联动

侧边栏仅在与工作流强相关的 Tab 显示；纯管理类 Tab 整页切换，隐藏侧边栏：

```python
def _on_tab_changed(self, tab_id: int) -> None:
    self.tab_stack.setCurrentIndex(tab_id)
    self.sidebar.setVisible(tab_id == 0)
```

## 四、Content（内容区）

### 4.1 职责

承载业务表单、列表、详情、预览等核心交互。通过 `QStackedWidget` 整页切换阶段，各页内部用 `QGroupBox` 分组、`QSplitter` 切分主从面板。

### 4.2 双层堆叠模式

| 层级 | 容器 | 切换粒度 | 触发 |
|------|------|----------|------|
| 顶层 | `tab_stack` | 功能域 | Header Tab 按钮 |
| 内层 | `main_stack` | 工作流阶段 | Sidebar 选中项 / 业务流程 |

内层 `main_stack` 仅在需要阶段切换的功能域内存在；纯管理类功能域直接用 `QWidget` 承载页面，无内层堆叠。

### 4.3 页面内部分组

- **`QGroupBox` 分组**：相关控件聚合为逻辑分组，标题用 `FONT_SIZE_HEADING` 加粗。
- **`QSplitter` 主从切分**：列表与详情用 `QSplitter`，伸缩比例通过 `setStretchFactor` 设定（如列表:详情 = 2:3）。
- **`QStackedWidget` 双态切换**：详情区常用「空态/非空态」双 `QStackedWidget`，根据选中项有无切换。

### 4.4 尺寸与背景

| 属性 | 值 | 令牌 |
|------|-----|------|
| 背景 | 应用底色 | `COLOR_BG_APP` |
| 页面内边距 | 12px（表单页）/ 0px（列表页贴边） | `SPACING_MD` 或 0 |
| 间距 | 8px | `SPACING_SM` |
| 卡片/分组背景 | 卡片白 | `COLOR_BG_CARD` |
| 卡片边框 | 1px `COLOR_BORDER`，圆角 `RADIUS_MD` | — |

### 4.5 阶段切换状态机

阶段枚举驱动整页切换与控件可用性，典型三阶段工作流：

```
SETUP ──启动任务──→ RUNNING ──完成/取消──→ RESULTS
  ↑                                        │
  └──────────重新配置──────────────────────┘
```

切换时须同步：

1. `main_stack.setCurrentIndex(page_index)`
2. `sidebar.setCurrentRow(page_index)`（blockSignals 防循环）
3. `_update_stage_actions()` 更新按钮/菜单可用性
4. 状态栏永久部件可见性（进度条仅 RUNNING 可见）

## 五、Status（状态栏）

### 5.1 职责

承载全局汇总信息、当前任务进度、当前操作对象。不承载主操作按钮。

### 5.2 结构与尺寸

| 属性 | 值 | 令牌 |
|------|-----|------|
| 容器 | `QMainWindow.statusBar()` | — |
| 高度 | 自适应（约 28px） | `STATUSBAR_HEIGHT` |
| 背景 | 卡片白 | `COLOR_BG_CARD` |
| 顶边框 | 1px `COLOR_BORDER` | — |
| 文字 | 次级文字，`FONT_SIZE_SMALL` | `COLOR_TEXT_SECONDARY` |

### 5.3 内容布局

```
[左侧汇总文本（stretch=1）]        [当前操作对象（永久）] [进度条（永久，200px）]
```

- **左侧**：汇总标签，通过 `addWidget(widget, stretch=1)` 占据剩余空间，显示统计摘要。
- **右侧永久部件**：通过 `addPermanentWidget` 追加，从右往左排列：
  - 当前操作对象标签：`setMaximumWidth(400)` 防止过长挤压左侧。
  - `QProgressBar`：`setFixedWidth(200)`，初始 `setRange(0, 100)` 确定模式。
- **临时消息**：`showMessage(text, timeout)` 在最左临时覆盖显示，超时自动清除。

### 5.4 可见性联动

永久部件随工作流阶段切换可见性：

```python
self.progress.setVisible(is_running)
self.current_item_label.setVisible(is_running)
```

非执行阶段隐藏进度条与当前操作对象，避免占用状态栏空间。

### 5.5 进度条模式

| 场景 | 模式 | 设置 |
|------|------|------|
| 未启动 | 确定模式，值 0 | `setRange(0, 100); setValue(0)` |
| 执行中（总量未知） | 不确定模式（滚动动画） | `setRange(0, 0)` |
| 执行中（总量已知） | 确定模式 | `setRange(0, total); setValue(done)` |

初始为确定模式，避免未启动时显示无意义动画；仅任务真正启动时切换为不确定模式。

## 六、关联设计与阶段联动

### 6.1 Sidebar 与 Header Tab 联动

- Header 切换到主任务 Tab → 显示侧边栏，侧边栏保留上次选中阶段。
- Header 切换到管理类 Tab → 隐藏侧边栏，整页切换。
- 侧边栏不跨 Tab 持久化选中项（各 Tab 独立阶段）。

### 6.2 Sidebar 与 Content 阶段联动

- 侧边栏选中项变化 → `main_stack` 切换对应页 → `_update_stage_actions` 刷新控件可用性。
- 业务流程驱动阶段切换（如启动任务自动跳到 RUNNING）→ 反向同步侧边栏选中项（blockSignals 防循环）。

### 6.3 Status 与阶段联动

- SETUP/RESULTS：仅显示左侧汇总文本。
- RUNNING：追加显示当前操作对象标签与进度条。
- 进度条模式随任务状态切换（确定↔不确定）。

## 七、QSplitter 伸缩规则

| Splitter | 子部件 | 比例 | 说明 |
|----------|--------|------|------|
| `sidebar_splitter` | sidebar : main_stack | 0 : 1（stretch），初始 220:1060 | 侧边栏固定宽，内容区伸缩 |
| 列表-详情 splitter | 列表 : 详情区 | 2 : 3 | 详情区优先扩展 |
| 双列表 splitter | 列表 A : 列表 B | 1 : 1 | 等分 |

设置方式：

```python
splitter.setStretchFactor(0, 0)  # 子部件 0 不伸缩
splitter.setStretchFactor(1, 1)  # 子部件 1 伸缩
splitter.setSizes([220, 1060])   # 初始尺寸
```

`handleWidth` 设为 4px（拖拽手柄宽度），过窄难以点击。

## 八、响应式折叠

- **宽度 < 1000px**：侧边栏折叠为图标条（56px），仅显示图标不显示文字。
- **宽度 < 800px**：触发 `minimumSize`，出现滚动条，不强制压缩控件。
- **窗口最大化**：通过 `QSplitter` 伸缩因子自动分配额外空间给内容区与详情区。
- **配置持久化**：`splitter.sizes()` 与窗口几何一并保存到配置文件，下次启动恢复。

## 九、主题图标着色策略

不同区背景色不同，图标须使用对应着色变体：

| 区域 | 背景 | 图标着色 | 令牌 |
|------|------|----------|------|
| Header | 主色（深） | 白 | `COLOR_TEXT_ON_PRIMARY` |
| Sidebar | 主色（深） | 白 | `COLOR_TEXT_ON_PRIMARY` |
| Content | 应用底色（浅） | 主色 | `COLOR_PRIMARY` |
| Status | 卡片白（浅） | 次级文字 | `COLOR_TEXT_SECONDARY` |

着色通过 `load_themed_icon(svg_path, color)` 实现：读取 SVG 文本 → 移除所有 `fill` 属性 → 在根 `<svg>` 标签注入 `fill="<color>"` → `QSvgRenderer` 渲染到 `QPixmap` → 构造 `QIcon`。主题色变更时须重建所有图标。
