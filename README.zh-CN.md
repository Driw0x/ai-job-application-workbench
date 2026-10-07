# AI Job Application Workbench

[English](README.md) | [Français](README.fr.md) | [简体中文](README.zh-CN.md)

这是一个本地运行的 **AI 辅助求职工作台**。收集职位、评估匹配度、选择有依据的候选人事实、复用/调整/创建简历、生成申请材料、人工审核并跟踪申请。整个流程保留人工参与，提交由用户手动完成。

## 工作流程

`SEARCH → REVIEW → SUBMIT → TRACK`

搜索职位或手动添加。检查匹配度、资格要求和职位时效。加入候选清单后排队准备材料。通过文档查看器审核简历、求职信、分析、公司资料和面试准备。批准后手动提交，再记录进展。应用支持 Ignore/去重、Trash/Restore、撤回和清理功能。清理已删除或已关闭的申请可能删除生成文档，请仔细阅读确认信息。

## 架构

前端使用 React + TypeScript + Vite，后端使用 FastAPI、本地 SQLite、支持的 LLM 提供商，以及显式配置的外部知识库。应用本身保持独立，知识库位于仓库外部并可由用户配置。

```text
backend/app/        API、工作流程、提供商、文档及用量记录
backend/scripts/    分阶段简历刷新和材料维护
backend/tests/      隔离的合成数据测试
frontend/src/       英文界面和交互测试
docs/               三语知识库格式说明
data/               本地数据库和申请材料（忽略；运行时创建）
```

## 环境要求与安装

已在 Windows 上使用 Python 3.13.1、Node.js 24.13.0 和 npm 11.10.0 完成本地验证。文档提供 Linux/macOS 安装命令，但尚未在这些平台上完成验证。

Python 3.11+、Node.js 22.12+ 或 24+，以及 Node 附带的 npm。仅 ChatGPT/Codex 文档准备需要 Codex CLI。

```sh
git clone https://github.com/Driw0x/ai-job-application-workbench ai-job-application-workbench
cd ai-job-application-workbench
python -m venv .venv
```

在仓库根目录使用 PowerShell：

```powershell
. .venv/Scripts/Activate.ps1
python -m pip install -r backend/requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --app-dir backend --env-file .env --host 127.0.0.1 --port 8000
```

Linux/macOS 使用 `source .venv/bin/activate` 激活环境，并使用 `cp .env.example .env` 复制配置；pip 和 uvicorn 命令相同。在另一个终端运行：

```sh
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

打开 `http://127.0.0.1:5173`。API 文档位于 `http://127.0.0.1:8000/docs`。两个服务均应绑定回环地址；应用没有多用户访问控制。

## 配置与知识库

在本地复制 `.env.example`。将 `KNOWLEDGE_BASE_PATH` 设置为外部知识库目录，或在 Search profile 中保存目录。保存的目录在后续启动时优先于环境变量，并同时用于搜索和文档生成。在控制面板选择 Markdown 来源。知识库可包含个人资料、技能、教育、经历、项目、约束及已有简历版本。只使用有来源支持的候选人事实。

`DATABASE_PATH` 可覆盖默认的 `data/job_tracker.db`。申请材料仍存放在 `data/applications`。不分发个人数据库：启动时初始化空表结构。知识库应位于本仓库之外。文档准备需要兼容的知识库和用户自己的 DOCX 模板，具体格式见下方说明。自定义提示词搜索不需要知识库。

## 自定义提示词

选择 **Custom prompt**，输入条件并保存。对于职位发现和匹配筛选，保存的提示词是候选人/搜索条件的唯一来源。不会隐式合并知识库文件、知识库个人资料或之前从知识库生成的提示词。固定安全规则、输出结构和已检索的职位证据仍然有效。空提示词会被拒绝。切回 **Knowledge Base** 可恢复已保存的来源选择。此模式不能替代准备简历和求职信所需的候选人知识库。

## ChatGPT / Codex 与其他提供商

通过 ChatGPT 准备文档时，按常用的受支持方式安装 Codex CLI，并确保 `codex` 位于 `PATH`。在提供商设置选择 **Continue with ChatGPT**，完成浏览器 OAuth，然后显式选择提供商、可用模型和推理强度。ChatGPT/Codex 集成使用 `codex app-server` 准备材料，通过 Responses 流式接口执行支持的直接网页搜索。功能取决于账号和模型能力；ChatGPT 订阅并不保证所有操作可用。不会自动切换到 API 计费。

使用 **OpenAI API** 时，在设置中输入 API key，测试/保存，确认潜在 API 费用，再显式选择 OpenAI 和可用模型。根据能力使用 Responses、Structured Outputs、推理和网页搜索。API 费用可能独立于 ChatGPT 订阅。应用还支持 Anthropic、Gemini 和 DeepSeek，并检查各提供商的能力限制，包括 DeepSeek 不支持直接网页搜索。

显式选择活动提供商和模型。支持按阶段覆盖配置和单独启用的备用提供商；备用切换默认关闭。检索可配置 SearXNG 实例，或使用支持网页搜索的提供商选择 **AI_DIRECT**。后续结构化筛选使用共同流程。

API key 保存在操作系统 keyring 中，服务名称为 `AIJobApplicationWorkbench`，不回退到明文存储。ChatGPT OAuth 文件在 Windows 位于 `%LOCALAPPDATA%/AIJobApplicationWorkbench/chatgpt/`，其他系统位于 `~/.config/AIJobApplicationWorkbench/chatgpt/`。凭据仅保存在本应用的本地环境中，不会提交到仓库。退出登录会删除本地 OAuth 状态，并尝试撤销授权。

## 简历、求职信与刷新

**REUSE** 复制合适的已有简历。**ADAPT** 保留项目集合，调整有依据的内容。**CREATE** 在来源充分时选择四个相关且有依据的项目，否则只使用实际可用的相关项目，数量为一至四个；可以登记可复用版本。绝不编造第四个项目。求职信使用相同项目上下文，生成五段有来源支持的内容。

准备和刷新使用面向招聘者的自然表达、事实依据、有意义且易读的指标，不输出原始日志或基准记录，使用易于理解的项目名称，并进行最终编辑检查。应用支持 DOCX 布局、技能平衡和验证。简历沿用模板语言，求职信沿用职位语言。应用界面以及分析、公司资料、面试准备的标题为英文。

刷新 REVIEW/SUBMIT 中符合条件且尚未发送的简历，无需重新创建申请。先检查 dry-run，再生成待审 DOCX，人工审核后应用：

```sh
python backend/scripts/refresh_cvs.py
python backend/scripts/refresh_cvs.py --stage data/cv-review
python backend/scripts/refresh_cvs.py --apply data/cv-review
```

CLI 脚本使用已导出的环境变量或控制面板保存的配置，不自行加载 `.env`。需要时显式传入 `--knowledge-base` 和 `--database`。申请简历刷新保留已有项目及顺序，并可追加有依据的相关项目至四个。保留状态、职位、历史、决策元数据、原始版本和其他文档。全局版本刷新（`--include-variants`）严格保留清单中的项目集合和顺序。文件或申请变更会使已审核计划失效。清理脚本默认 dry-run，修改前查看 `--help` 和报告。旧求职信迁移脚本调用后直接写入，需要导出的知识库/数据库配置。

应用提供 Markdown 和 DOCX 查看器以及集成导航。PDF 转换在 Windows 优先尝试 Microsoft Word，然后尝试 LibreOffice。如需 PDF，请单独安装可用转换器；缺少转换器会返回明确错误。使用前检查分页和内容。

## Token 用量

Numbers/Charts 视图显示搜索与文档生成的用量汇总和日期图表。用量记录保存提供商/模型/阶段信息，职位发现还显示实际观测的网页搜索元数据。未知用量显示为不可用，实际观测的零仍为零。日期筛选使用 UTC。不添加虚构价格、外部分析服务或遥测。提供商请求会传输所选流程明确需要的上下文。

## 测试

```sh
cd backend
python -m pytest -q --tb=short
cd ../frontend
npm test
npm run typecheck
npm run build
```

测试使用合成知识库/模板和临时数据库，不使用真实凭据或计费调用。前端交互使用 Vitest/jsdom，不需要额外的浏览器测试框架。在受限 Windows 环境中，将 `TEMP` 和 `TMP` 设置为仓库之外可写的临时目录。真实提供商/OAuth 冒烟测试需要自己的账号；PDF 需要转换器。

## 安全与负责任使用

绝不提交 `.env`、API key、OAuth 凭据、token、私有知识库/提示词、个人简历/求职信、生成输出或个人 SQLite 数据库。`.gitignore` 排除常见路径和缓存，但发布前仍应检查暂存文件。不包含个人模板或源数据库。创建可复用版本会有意向配置的外部知识库写入简历和版本清单，请备份知识库。

使用前验证每个事实和文档。来源约束不能替代人工审核。应用不提供自动提交或批量申请机制。用户对提交内容负责。项目代码采用 [MIT 许可证](LICENSE)。第三方依赖保留各自的许可证；本仓库不包含依赖源码或构建产物。

## 文档

- [外部知识库与模板格式](docs/knowledge-base.zh-CN.md)
- [AI Knowledge Workflows](https://github.com/Driw0x/ai-knowledge-workflows) — 可选的配套仓库，提供可复用的 AI 知识工作流。