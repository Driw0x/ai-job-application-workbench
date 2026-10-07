# External Knowledge Base contract

[English](knowledge-base.md) | [Français](knowledge-base.fr.md) | [简体中文](knowledge-base.zh-CN.md)

[Back to README](../README.md)

## Location and supported structure

Set `KNOWLEDGE_BASE_PATH` or save a directory in Search profile. The KB stays external and private. Search can use explicitly selected Markdown files. Candidate document preparation uses the existing compatible structure below; this is a data contract, not a dependency on a particular repository or candidate. Optional generated variants and their manifest are needed only for variant reuse/registration.

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

Preferred English files are `Profile.md`, `Domains.md`, `Job Search.md`. Legacy `Profil.md`, `Domaines.md`, `Stage M2.md` remain supported. The English cover template takes precedence over legacy `lettre-motivation-template.docx`. Hidden files/directories, private application folders and paths escaping the configured root are excluded from selectable search sources. Merely placing a sibling directory near the app never activates a KB.

## Profile and evidence

Document actual background, skills, constraints and project outcomes. The following contact block is entirely fictitious; replace every value with your own truthful information. English and legacy French contact labels are supported.

```markdown
- **Name** : Alex Example
- **Address** : 1 Example Street
- **Postal code** : 12345
- **City** : Example City
- **Email** : alex@example.test
- **Phone** : 01 23 45 67 89
```

Project directories under `02_Projects` define canonical project identifiers. Give each project factual Markdown evidence. Select four relevant distinct projects when available; use fewer rather than inventing facts. Custom-prompt discovery uses only its saved search criteria, without this KB. Resume/letter preparation still needs candidate evidence and templates.

## Resume template

Supply your own `01_Career/CV/Templates/resume-template.docx`. No personal template is bundled. If `Templates` contains exactly one DOCX with a different filename, it is selected automatically. When multiple templates exist, use the preferred filename to select one unambiguously. The existing DOCX renderer expects the headline in the third top-level paragraph and a first table with two columns. The left cell contains profile, skills, languages; the right contains selected projects then education. Supported headings are `PROFILE`, `SKILLS`, `LANGUAGES`, `SELECTED PROJECTS`, `EDUCATION`, or the legacy French equivalents.

Keep identity/contact details, education and other static template facts truthful: those are retained rather than invented. Skills have four groups of three to five entries. Projects have a title, a separate description and two to three bullets; one to four projects are supported. Existing title/bullet paragraph styles provide formatting for rebuilt content. A legacy resume lacking descriptions requires ADAPT before REUSE. Layout validation can reject oversized content; condense text rather than shrinking fonts/margins. Final Word/PDF pagination needs human review.

## Variants and refresh

Generated files use `resume_A.docx`, `resume_B.docx`, etc. A unique legacy `*_A.docx` also works. `CV_Variants.md` maps a variant to one to four distinct exact project directory names:

```markdown
## Variant A — General profile

Projects :

1. Example Project
2. Second Example Project
```

Both `Variant`/`Variante` headings and `Projects`/`Projets` labels are recognized. ADAPT keeps the source variant project set; CREATE registers a new reusable set when necessary. Application refresh may append grounded relevant projects up to four while preserving existing order and historical decision metadata. Global-variant refresh retains its manifest identity/set/order. The application artifacts are separate copies. Variant creation writes the external KB's Generated directory and manifest, so back up the KB before use.

## Cover-letter template

Supply `01_Career/Cover Letter/Templates/cover-letter-template.docx`. Each placeholder below must occur exactly once as `{{TOKEN}}`, including across DOCX runs:

```text
CANDIDATE_NAME CANDIDATE_ADDRESS CANDIDATE_ZIP_CODE CANDIDATE_EMAIL
CANDIDATE_PHONE COMPANY_NAME COMPANY_ADDRESS COMPANY_ZIP_CODE
CANDIDATE_CITY DATE JOB_TITLE SALUTATION
PARAGRAPH_1 PARAGRAPH_2 PARAGRAPH_3 PARAGRAPH_4 PARAGRAPH_5
CLOSING SIGNATURE_NAME
```

Company address/postal code can be empty when unavailable; missing information is reported rather than fabricated. Candidate contact fields must be present. Five body paragraphs use grounded facts and the shared project context. Existing template formatting is preserved. The legacy generated filename `lettre-motivation.docx` is retained for artifact compatibility.

## Local outputs and privacy

Application folders under `data/applications` contain `offer.md`, `analysis.md`, `company.md`, `interview-prep.md`, the cover-letter DOCX and the selected resume under `cv/`. SQLite stores workflow state, metadata, events, settings and observed token usage. Nothing here is public by default. Credentials are separate; see [security and providers](../README.md). Do not commit your KB, generated documents, private prompts, database or credentials.
