# 外部知识库格式

[English](knowledge-base.md) | [Français](knowledge-base.fr.md) | [简体中文](knowledge-base.zh-CN.md)

[返回 README](../README.zh-CN.md)

## 位置与支持的结构

设置 `KNOWLEDGE_BASE_PATH`，或在 Search profile 保存目录。知识库保持外部、私有。搜索使用显式选择的 Markdown 文件。文档准备使用以下兼容结构，这是数据格式约定，不依赖特定仓库或候选人。可选的已有版本及清单仅用于简历版本复用/登记。

```text
candidate-knowledge-base/
  01_Career/
    Profile.md
    Domains.md
    Job Search.md
    CV/
      Templates/resume-template.docx
      Generated/resume_A.docx
      CV_Variants.md
    Cover Letter/Templates/cover-letter-template.docx
  02_Projects/
    Example Project/Example Project.md
  03_Knowledge/
    Example Topic.md
```

优先使用英文文件 `Profile.md`、`Domains.md`、`Job Search.md`，同时兼容旧文件 `Profil.md`、`Domaines.md`、`Stage M2.md`。英文求职信模板优先于旧的 `lettre-motivation-template.docx`。隐藏文件/目录、私有申请目录和越出根目录的路径不能被选为搜索来源。仅把目录放在应用旁边不会激活知识库。

## 个人资料与证据

记录真实背景、技能、约束和项目成果。以下联系信息完全虚构，请全部替换成自己的真实信息。支持英文和旧法文联系字段。

```markdown
- **Name** : Alex Example
- **Address** : 1 Example Street
- **Postal code** : 12345
- **City** : Example City
- **Email** : alex@example.test
- **Phone** : 01 23 45 67 89
```

`02_Projects` 下的目录定义规范项目标识。每个项目都应有事实性 Markdown 证据。来源充分时选择四个不同的相关项目，宁可少用，也不编造。自定义提示词发现仅使用保存的搜索条件，不使用此知识库。简历/求职信准备仍需要候选人证据和模板。

## 简历模板

提供自己的 `01_Career/CV/Templates/resume-template.docx`。不附带个人模板。如果 `Templates` 中恰好只有一个使用其他文件名的 DOCX，会自动选择该文件。存在多个模板时，使用首选文件名明确指定。现有 DOCX 渲染器要求第三个顶层段落为简历标题，第一个表格为两列。左侧为简介、技能、语言；右侧为所选项目及教育。支持标题 `PROFILE`、`SKILLS`、`LANGUAGES`、`SELECTED PROJECTS`、`EDUCATION`，以及旧法文对应标题。

模板中身份、联系信息、教育及其他静态事实必须真实，这些内容会被保留而非编造。技能分四组，每组三至五项。项目包含标题、独立说明和两至三条要点，支持一至四个项目。重建内容沿用已有标题/要点段落样式。缺少说明的旧简历必须先 ADAPT 才能 REUSE。布局验证可能拒绝过长内容，应压缩文本而非缩小字体/页边距。最终 Word/PDF 分页需要人工审核。

## 简历版本与刷新

生成文件使用 `resume_A.docx`、`resume_B.docx` 等名称，也兼容唯一的旧 `*_A.docx` 文件。`CV_Variants.md` 将版本映射到一至四个不同且精确匹配的项目目录名：

```markdown
## Variant A — General profile

Projects :

1. Example Project
2. Second Example Project
```

识别 `Variant`/`Variante` 标题及 `Projects`/`Projets` 标签。ADAPT 保留来源版本的项目集合；CREATE 在需要时登记新的可复用集合。申请简历刷新可以追加有依据的相关项目至四个，同时保留已有顺序和历史决策元数据。全局版本刷新保留清单中的身份、集合和顺序。申请材料使用独立副本。创建版本会写入外部知识库的 Generated 目录和清单，请提前备份。

## 求职信模板

提供 `01_Career/Cover Letter/Templates/cover-letter-template.docx`。以下每个占位符必须以 `{{TOKEN}}` 形式恰好出现一次，即使跨越多个 DOCX run：

```text
CANDIDATE_NAME CANDIDATE_ADDRESS CANDIDATE_ZIP_CODE CANDIDATE_EMAIL
CANDIDATE_PHONE COMPANY_NAME COMPANY_ADDRESS COMPANY_ZIP_CODE
CANDIDATE_CITY DATE JOB_TITLE SALUTATION
PARAGRAPH_1 PARAGRAPH_2 PARAGRAPH_3 PARAGRAPH_4 PARAGRAPH_5
CLOSING SIGNATURE_NAME
```

公司地址/邮编未知时可为空；缺失信息会被报告而非编造。候选人联系方式必须完整。五段正文使用有来源支持的事实和共同项目上下文。保留原模板格式。为保持材料兼容性，生成文件仍使用旧名称 `lettre-motivation.docx`。

## 本地输出与隐私

`data/applications` 下的申请目录包含 `offer.md`、`analysis.md`、`company.md`、`interview-prep.md`、求职信 DOCX，以及 `cv/` 下的简历。SQLite 保存流程状态、元数据、事件、设置和实际用量。这些内容默认不公开。凭据单独保存，参见 [安全与提供商](../README.zh-CN.md)。不要提交知识库、生成文档、私有提示词、数据库或凭据。
