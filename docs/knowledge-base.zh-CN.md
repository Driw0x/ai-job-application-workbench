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

提供自己的 `01_Career/CV/Templates/resume-template.docx`。不附带个人模板。如果 `Templates` 中恰好只有一个使用其他文件名的 DOCX，会自动选择该文件。存在多个模板时，使用首选文件名明确指定。渲染器兼容旧双栏模板和 ATS 模板。前三段分别为姓名、学历和个性化标题，随后为联系信息。所有输出统一使用 A4 纵向、单栏、Arial 字体，不使用表格或浮动文本框。栏目顺序为 `PROFIL`、`COMPÉTENCES`、`PROJETS`、`FORMATION`、`EXPÉRIENCES`、`LANGUES`、`CENTRES D'INTÉRÊT`。兼容旧英文及法文标题；没有事实依据的栏目不会编造。

保留身份、联系方式、教育及其他静态事实。技能分四组，每组三至五项，紧凑排列。新项目包含标题、技术、说明和两至三条要点。REUSE 保留内容并应用 ATS 格式；ADAPT 和 CREATE 根据事实进行个性化。可点击链接仅集中在顶部联系信息行。不会为了强制一页而删除内容；最终 Word/PDF 分页需要人工审核。

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
