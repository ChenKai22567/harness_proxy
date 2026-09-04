# Harness代理启动 (Harness Proxy Launcher)

为 **Antigravity** 与 **Codex (ChatGPT)** 量身定制的现代极简专用代理启动管理中心。

---

## 🌟 核心特性

- **🛡️ 纯进程级沙箱隔离**：严格只为 Antigravity 与 Codex 的进程树挂载 Chromium `--proxy-server` 与进程级环境变量，**绝不修改 Windows 系统代理、WinHTTP 或 TUN 配置**。
- **🌐 自由配置与多用户支持**：
  - 支持任意代理内核（Clash for Windows、Clash Verge、Mihomo Party、v2rayN、Sing-box 等）。
  - 内置常见端口一键预设（7890、7897、10808、2080）。
  - 支持【测试连通】与【自动探测可用代理端口】。
- **🔍 智能路径与环境发现**：
  - 自动穿透扫描 Antigravity 安装路径。
  - 自动动态解析 Microsoft Store 版 `OpenAI.Codex` (`ChatGPT.exe`)。
  - 支持在偏好设置中自由【浏览文件】手动指定任意路径。
  - 内置自包含的 DevTools 代理扩展补丁（`node-proxy-bootstrap.cjs` 与 `shims/`），免外部路径依赖。
- **📊 实时运行与零流量回环监控**：
  - 实时异步探测本地回环监听状态与响应延时（零外网流量消耗）。
  - 托盘休眠自动节流，恢复窗口即时唤醒探活。
  - 实时监控应用运行状态、进程 PID、代理参数挂载标识。
- **🧩 组件与子进程管理**：
  - 按核心、渲染、网络、Codex API、GPU、存储、崩溃恢复等功能聚合子进程。
  - 可分别设置是否纳入一键启动、Antigravity 浏览器补丁和 GPU 加速策略。
  - 区分受管与外部会话；外部会话可在确认后验证并接管，停止/重启按钮无需永久禁用。
  - 区分活动进程、可清理的孤立辅助进程和 Windows 已终止记录；残留不会再阻止重新启动。
- **🔌 Codex 标准代理链路**：
  - 启动参数覆盖 Chromium，进程环境使用 Codex 支持的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 与 `NO_PROXY`。
  - Codex 的安全 WebSocket 与 HTTPS 共用标准代理路由；预检分别显示本地监听、HTTPS 出站、浏览器/API/WebSocket 三层状态。
- **🖥️ 现代桌面与极简浅色美学**：
  - 基于 Windows 11 Fluent 浅色风格（CustomTkinter），使用系统感灰白表面、克制圆角与深灰主操作色。
  - 下拉框采用有描边的一体化字段与 1 px 细线箭头；自绘列表使用真正的透明圆角外缘和两段直线型对勾，提供紧凑留白与中性悬停反馈，并可靠保持至选择或外部点击。
  - 代理网络固定宽度排版，杜绝数字跳动造成的视觉抖动。
  - 控制台日志支持自然折行（Word Wrap），免去横向滚动条干扰。
  - 支持点击关闭时自动最小化到 Windows 任务栏系统托盘。

---

## 🚀 快速使用

### 方式一：直接运行源码（开发者推荐）
1. 进入目录：
   ```powershell
   cd harness_proxy
   ```
2. 安装依赖并启动界面：
   ```powershell
   python -m pip install -r requirements.txt
   python main.py
   ```

### 方式二：一键创建桌面快捷方式
在 PowerShell 中执行：
```powershell
& ".\install_desktop_shortcut.ps1"
```
桌面即可生成 **`Harness代理启动.lnk`** 快捷方式，双击直接启动。

### 方式三：打包为独立免安装绿色软件（发给他人使用）
运行打包脚本：
```powershell
python build.py
```
构建成功后，在 `dist\HarnessProxyLauncher` 目录下会生成完整的绿色免安装应用包。将整个 `HarnessProxyLauncher` 文件夹压缩打包（ZIP）发给任何人，对方双击 `HarnessProxyLauncher.exe` 即可使用，**对方电脑无需安装 Python 或任何环境**。

---

## 🧭 优化与后续计划

本次 UI 修复、子进程功能展示/开关设计、当前性能问题、分阶段优化任务及量化验收方法，统一记录在 [`DESIGN_AND_PERFORMANCE_PLAN.md`](DESIGN_AND_PERFORMANCE_PLAN.md)。

---

## 📁 目录结构

```text
harness_proxy\
├── assets\                      # 静态资源与内置补丁
│   ├── icon.ico                 # 应用高清徽标
│   ├── icon.png                 # 托盘与面板图标
│   ├── node-proxy-bootstrap.cjs # 浏览器代理注入钩子
│   └── shims\                   # DevTools 代理重定向脚本
├── core\                        # 核心业务与控制调度层
│   ├── config_manager.py        # 配置中心 (config.json)
│   ├── launcher_engine.py       # 启动引擎与环境参数注入
│   ├── component_registry.py    # 稳定功能角色与可控策略声明
│   ├── process_classifier.py    # 子进程功能分类与按需资源采样
│   ├── process_model.py         # 组件及进程数据结构
│   ├── process_detector.py      # 进程探测与路径解析
│   ├── proxy_prober.py          # TCP/HTTPS 代理探活与多端口扫描
│   └── supervisor.py            # 受管会话归属、验证与安全停止
├── gui\                         # 现代界面层 (CustomTkinter)
│   ├── main_window.py           # 主窗口与状态流
│   ├── component_dialog.py      # 组件、子进程与启动策略界面
│   ├── settings_dialog.py       # 偏好与代理配置弹窗
│   ├── tray_manager.py          # Windows 托盘集成
│   └── widgets.py               # 统一美化的下拉框等复用控件
├── tests\                       # UI、资源路径、单实例及打包回归测试
├── logs\                        # 运行日志目录
├── build.py                     # 自动化打包脚本
├── config.example.json          # 可公开的默认配置示例
├── config.json                  # 本地持久化配置（首次运行生成，不入库）
├── requirements.txt             # Python 运行与构建依赖
├── DESIGN_AND_PERFORMANCE_PLAN.md # 子进程设计与性能优化计划
├── install_desktop_shortcut.ps1 # 桌面快捷方式安装器
├── main.py                      # 程序主入口
└── README.md                    # 说明文档
```
