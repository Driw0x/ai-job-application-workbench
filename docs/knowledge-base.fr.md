# Contrat de Knowledge Base externe

[English](knowledge-base.md) | [Français](knowledge-base.fr.md) | [简体中文](knowledge-base.zh-CN.md)

[Retour au README](../README.fr.md)

## Emplacement et structure prise en charge

Définissez `KNOWLEDGE_BASE_PATH` ou enregistrez un dossier dans Search profile. La KB reste externe et privée. La recherche utilise les fichiers Markdown explicitement sélectionnés. La préparation de documents utilise la structure compatible ci-dessous : contrat de données, sans dépendance à un repository ni candidat particulier. Les variantes générées et leur manifeste servent uniquement à la réutilisation/enregistrement des variantes.

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

Les fichiers anglais préférés sont `Profile.md`, `Domains.md`, `Job Search.md`. Les anciens `Profil.md`, `Domaines.md`, `Stage M2.md` restent compatibles. Le template de lettre anglais est prioritaire sur `lettre-motivation-template.docx`. Fichiers/dossiers cachés, dossiers privés de candidatures et chemins sortant de la racine sont exclus des sources de recherche sélectionnables. Un dossier voisin n'active jamais implicitement une KB.

## Profil et preuves

Documentez parcours, compétences, contraintes et résultats réels des projets. Ce bloc de contact est entièrement fictif ; remplacez chaque valeur par vos informations exactes. Les libellés anglais et anciens libellés français sont acceptés.

```markdown
- **Name** : Alex Example
- **Address** : 1 Example Street
- **Postal code** : 12345
- **City** : Example City
- **Email** : alex@example.test
- **Phone** : 01 23 45 67 89
```

Les dossiers de `02_Projects` définissent les identifiants canoniques. Fournissez des preuves Markdown factuelles pour chaque projet. Sélectionnez quatre projets distincts pertinents lorsqu'ils existent ; utilisez moins de projets plutôt que d'inventer. La découverte par prompt personnalisé utilise uniquement ses critères enregistrés, sans cette KB. Les CV/lettres exigent toujours sources candidat et templates.

## Template de CV

Fournissez votre propre `01_Career/CV/Templates/resume-template.docx`. Aucun template personnel inclus. Si `Templates` contient exactement un DOCX portant un autre nom, il est sélectionné automatiquement. Avec plusieurs templates, utilisez le nom préféré pour lever toute ambiguïté. Le renderer DOCX existant attend le titre dans le troisième paragraphe principal et un premier tableau à deux colonnes. À gauche : profil, compétences, langues ; à droite : projets sélectionnés, puis formation. Titres compatibles : `PROFILE`, `SKILLS`, `LANGUAGES`, `SELECTED PROJECTS`, `EDUCATION`, ou leurs anciens équivalents français.

Identité, contacts, formation et autres faits statiques du template doivent être exacts : ils sont conservés, jamais inventés. Les compétences comportent quatre groupes de trois à cinq éléments. Chaque projet comporte un titre, une description séparée et deux à trois puces ; un à quatre projets sont acceptés. Les styles des paragraphes titres/puces existants servent à reconstruire le contenu. Un ancien CV sans descriptions exige ADAPT avant REUSE. La validation peut refuser un contenu trop long ; condensez le texte sans réduire polices/marges. La pagination finale Word/PDF exige une revue humaine.

## Variantes et refresh

Les fichiers générés s'appellent `resume_A.docx`, `resume_B.docx`, etc. Un unique ancien `*_A.docx` fonctionne aussi. `CV_Variants.md` associe une variante à un à quatre noms de dossiers de projets exacts et distincts :

```markdown
## Variant A — General profile

Projects :

1. Example Project
2. Second Example Project
```

Les titres `Variant`/`Variante` et libellés `Projects`/`Projets` sont reconnus. ADAPT conserve les projets de la variante source ; CREATE enregistre un nouvel ensemble réutilisable si nécessaire. Le refresh de candidature peut ajouter des projets pertinents documentés jusqu'à quatre en conservant l'ordre existant et les métadonnées historiques de décision. Le refresh d'une variante globale conserve identité/ensemble/ordre du manifeste. Les artefacts de candidature sont des copies séparées. La création de variante écrit dans Generated et le manifeste de la KB externe : sauvegardez-la.

## Template de lettre

Fournissez `01_Career/Cover Letter/Templates/cover-letter-template.docx`. Chaque placeholder ci-dessous doit apparaître exactement une fois sous forme `{{TOKEN}}`, y compris s'il est réparti entre plusieurs runs DOCX :

```text
CANDIDATE_NAME CANDIDATE_ADDRESS CANDIDATE_ZIP_CODE CANDIDATE_EMAIL
CANDIDATE_PHONE COMPANY_NAME COMPANY_ADDRESS COMPANY_ZIP_CODE
CANDIDATE_CITY DATE JOB_TITLE SALUTATION
PARAGRAPH_1 PARAGRAPH_2 PARAGRAPH_3 PARAGRAPH_4 PARAGRAPH_5
CLOSING SIGNATURE_NAME
```

L'adresse/code postal d'entreprise peuvent rester vides si inconnus ; les informations manquantes sont signalées, jamais inventées. Les contacts candidat sont obligatoires. Cinq paragraphes utilisent les faits documentés et le contexte projet commun. La mise en forme du template est conservée. Le nom généré historique `lettre-motivation.docx` reste pour compatibilité des artefacts.

## Outputs locaux et confidentialité

Les dossiers sous `data/applications` contiennent `offer.md`, `analysis.md`, `company.md`, `interview-prep.md`, la lettre DOCX et le CV sélectionné dans `cv/`. SQLite conserve état du workflow, métadonnées, événements, paramètres et consommation observée. Rien de cela n'est public par défaut. Les credentials sont séparés ; voir [sécurité et providers](../README.fr.md). Ne commitez pas KB, documents générés, prompts privés, base ou credentials.
