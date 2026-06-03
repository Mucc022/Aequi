# Aequora Project Blueprint

这个文件是 Aequora 的项目总地图、任务板和交接日志。它面向 vibe coding 工作流：用户可以用直白自然语言在这里记录想法、痛点和下一步，Codex 每次开始任务前先读它，每次结束前检查并更新它。

## 使用规则

给用户：

- 可以直接在“任务池”里写需求，不需要写技术方案。
- 建议写成“我现在遇到什么问题 / 我想要什么效果 / 什么算解决”。
- 如果不确定怎么表达，先写主观体验，例如“启动太慢”“界面看不懂”“下载完不知道文件在哪里”。

给 Codex：

- 每次开始 Aequora 任务前，先读 `AGENTS.md`、`PROJECT_BLUEPRINT.md`，再看相关代码。
- 每次结束前，检查本次需求是否解决，并更新“任务池”和“工作日志”。
- 不要把这个文件当成正式规格冻结；它是活文档，允许随着 vibe coding 变化。
- 如果用户提出的新需求很模糊，先把它整理成清楚的任务条目，再实现。
- 更新任务时保留用户原意，补充工程判断和验证结果。

## 项目一句话

Aequora 是一个 Windows-first 的本地学习资料整理工具：用户丢进本地媒体、文件夹或网页链接，程序帮他整理出音频、字幕、文本、结果目录和后台记录。

## 当前产品方向

- 优先保证普通用户可以双击打开、看懂界面、完成任务、找到结果。
- GUI 是主体验，CLI 是开发、调试和批处理工具。
- 任务处理过程要透明：候选选择、进度、失败原因、结果位置都要清楚。
- 出错不能静默；日志、调试入口、恢复方式要保留。
- 项目文档要服务“新 Codex 窗口快速接续”和“用户自己能看懂当前进度”。

## 当前技术地图

- GUI: `media2text/gui_fluent.py`，PySide6 + qfluentwidgets。
- GUI 启动: `start.bat` -> `run_gui.pyw` -> `run_gui.py`。
- CLI: `media_tool.py` -> `media2text/cli.py`。
- 核心执行: `media2text/orchestrator.py`。
- 下载/元数据: `media2text/ytdlp_pipeline.py`。
- 网页候选发现: `media2text/scraper_engine.py`。
- 转写: `media2text/transcriber.py`。
- FFmpeg: `media2text/ffmpeg_utils.py`。
- 回退/清理 ledger: `media2text/run_ledger.py`。
- 配置: `config.json` + `media2text/config.py`。

## 当前仓库状态

- Git 历史已经重开，当前 `main` 是 Aequora 的新初始历史。
- 远端只保留 `origin/main`。
- 旧 `Unified Media2Text` 分支和 tag 已清理。
- `README.md` 是用户说明。
- `AGENTS.md` 是 Codex 交接说明。
- 本文件是项目路线图和任务日志。
- `CODEX_MEMORY.md` 是可上传/复用的长期记忆草稿。

## 任务池

状态说明：

- `Idea`: 只是想法，还没确定要做。
- `Ready`: 可以开始做。
- `Doing`: 正在做。
- `Done`: 已完成并验证。
- `Blocked`: 被外部条件卡住。

| ID | 状态 | 用户原话/自然语言需求 | 期望结果 | 最近处理 |
| --- | --- | --- | --- | --- |
| AQR-001 | Done | 需要 README 和 git 重新整理，把当前项目当第一版 | README 反映 Aequora 当前状态；git 历史从当前版本开始 | 已完成，`main` 新历史已推送 |
| AQR-002 | Done | 新 Codex 窗口要能快速读懂项目 | 仓库里有 Codex 交接文件 | 已完成，新增 `AGENTS.md` |
| AQR-003 | Done | 需要项目总地图和规则，每次任务前后都检查更新 | 有一个活文档记录蓝图、任务和日志 | 本次新增 `PROJECT_BLUEPRINT.md` |
| AQR-004 | Ready | 需要继续优化产品体验 | 先从启动、界面可懂性、结果可找到、失败可恢复这些方向挑下一个具体任务 | 等用户指定下一步或 Codex 根据现状建议 |
| AQR-005 | Done | 帮我安装一下环境，启用这个项目 | 本机安装 Python、项目虚拟环境、依赖和 FFmpeg；双击启动优先使用项目 `.venv` | 已完成，烟测通过 |
| AQR-006 | Done | 网页上明明有多个视频，但候选没有发现记录/只发现无关链接；想要深度挖掘 | Wix 页面能进入 Thunderbolt JSON 深层数据，抓到真实播放器 mp4，不把页脚社交链接当视频 | 已修复并用 Four Seas 页面验证发现 4 条真实视频 |
| AQR-007 | Done | 给一个母链接，自动把所有链接爬出来并分类标签好；希望有图形化交互，整合在新建任务下面；结果要直接显示并可勾选复制；结果应像网页上的课程目录，而不是底层资源链接 | 新建任务页有“链接地图”面板；Four Seas/Wix 课程目录页优先显示课程日期条目，支持勾选复制，并保留 JSON/CSV/TXT 导出 | 已新增 `crawl_links.py`，并整合进 `gui_fluent.py` |
| AQR-008 | Done | 输出文件名前面的 `017_`、`018_` 这种编号有点鸡肋，想做成默认不勾选的选项 | 默认文件名不再加顺序编号；需要时可在 GUI 高级设置勾选，或 CLI 使用 `--result-index` | 已新增配置项、GUI 复选框和 CLI 开关 |

## 体验优化候选

这些不是承诺，只是后续可选方向：

- 启动体验：双击无反应时自动留下更明显的错误提示。
- 首页体验：让用户更快理解“粘贴链接/选择文件夹/开始解析”的流程。
- 结果体验：处理完成后更明显地打开结果目录、定位主结果文件。
- 失败体验：失败原因用普通语言解释，并给下一步按钮。
- 配置体验：把 Whisper 模型、语言、输出格式整理成更直观的预设。
- 打包体验：生成可分发的 EXE 或 zip，并保留 debug 启动方式。
- 文档体验：README 保持给普通用户，AGENTS/Blueprint 保持给 Codex 和维护者。

## 工作日志

### 2026-04-30

- 重写 `README.md`，从旧 `Unified Media2Text` 说明切换为 Aequora 当前说明。
- 更新 `.gitignore`，排除本地运行产物、日志、快捷方式、验证输出和旧脚本备份。
- 重开 git 历史，把当前项目作为新的 `main` 初始提交。
- 强制更新远端 `origin/main`，删除旧远端分支和旧 tag。
- 新增 `AGENTS.md`，作为未来 Codex 会话的项目交接文件。
- 新增 `PROJECT_BLUEPRINT.md`，作为项目总地图、任务池和日志。
- 新增 `CODEX_MEMORY.md`，作为可上传/复用的 Codex 项目记忆草稿。

### 2026-05-29

- 安装 Python 3.12.10，并在项目内创建 `.venv`。
- 安装 `requirements.txt` 里的 GUI、下载、Whisper 和打包依赖。
- 通过 WinGet 安装 FFmpeg 8.1.1，并修复用户 PATH，加入 Python 与 FFmpeg 路径。
- 更新 `start.bat`，优先使用 `.venv\Scripts\pythonw.exe` 启动 GUI。
- 验证通过：`compileall`、`media_tool.py --help`、FFmpeg 版本检查、跳过 Whisper 的本地音频导出烟测。
- 修复 Wix Thunderbolt 深层候选扫描：避免 `&registry...` 被 HTML 实体误读成 `®istry...` 导致 JSON 请求 400，并优先读取 Wix `VideoPlayer` 的真实 `src`。
- 用 `https://www.fourseas-chinese.org/2026-04-26-希伯来历史` 验证，候选发现从误抓页脚 YouTube 改为识别 4 条 `video.wixstatic.com` mp4。
- 新增 `crawl_links.py`，支持从母链接爬取同站链接，按 `page_internal`、`page_external`、`video`、`image`、`script`、`style` 等类别导出 `links.json`、`links.csv` 和 `links_by_category.txt`。
- 在 `media2text/gui_fluent.py` 的“新建任务”页加入“链接地图：从母链接爬取并分类”面板，可直接输入母链接、设置深度/最多页面数、启动爬取并打开结果目录。
- 链接爬取完成后在 GUI 表格内直接渲染结果，支持全选、全选视频、清空选择、复制已选链接；视频链接默认勾选。
- 优化 Four Seas/Wix 课程目录页解析：从 Thunderbolt `pageList` 中提取课程日期条目（如 `2026-04-14 创世记`），GUI 默认优先展示课程列表，避免把脚本、样式、图片资源当作主要结果。
- 统一链接地图复制 URL 格式：GUI 可读显示中文路径，但复制和导出使用浏览器标准百分号编码 URL，避免同一课程链接出现中文/编码两种形态。
- 更新 README，加入 GUI 和 CLI 两种链接爬取使用方式和常用参数。
- 将结果文件名前缀编号改为可选项：默认不再生成 `001_`、`002_` 前缀；GUI“新建任务 > 高级设置”新增“文件名前加序号”，CLI 新增 `--result-index`/`--no-result-index`。

## 每次任务结束检查表

- 是否更新了任务池状态？
- 是否在工作日志写了本次关键变化？
- 是否说明了验证命令和结果？
- 是否避免提交运行输出、本机快捷方式、日志和虚拟环境？
- 如果改了启动、GUI 或打包路径，是否保留了 debug 路径？
