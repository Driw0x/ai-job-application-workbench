# AI Job Application Workbench

[English](README.md) | [Français](README.fr.md) | [简体中文](README.zh-CN.md)

Un **atelier de candidature assisté par IA**, exécuté localement. Collectez les offres, évaluez leur pertinence, sélectionnez des faits documentés, réutilisez/adaptez/créez des CV, générez les documents, relisez-les et suivez vos candidatures. L'utilisateur reste dans la boucle ; l'envoi reste manuel.

## Workflow

`SEARCH → REVIEW → SUBMIT → TRACK`

Découvrez une offre ou ajoutez-la manuellement. Examinez pertinence, éligibilité et fraîcheur. Sélectionnez-la pour préparer ses documents. Relisez CV, lettre, analyse, profil d'entreprise et préparation d'entretien dans les viewers. Validez, envoyez manuellement, puis suivez les étapes. Ignore/déduplication, Trash/Restore, retrait et nettoyage sont pris en charge. Le nettoyage des candidatures supprimées ou closes peut effacer les documents générés : relisez la confirmation.

## Architecture

Frontend React + TypeScript + Vite, backend FastAPI, SQLite locale, providers LLM pris en charge et Knowledge Base externe configurée explicitement. L'application est autonome ; la Knowledge Base reste externe et configurable.

```text
backend/app/        API, workflows, providers, documents et registre de consommation
backend/scripts/    refresh de CV avec revue et maintenance des artefacts
backend/tests/      tests synthétiques isolés
frontend/src/       interface anglaise et tests interactifs
docs/               contrat de Knowledge Base en trois langues
data/               base locale et candidatures (ignorées ; créées à l'exécution)
```

## Prérequis et installation

Validation locale sous Windows avec Python 3.13.1, Node.js 24.13.0 et npm 11.10.0. Les commandes Linux/macOS sont documentées mais n'ont pas encore été validées sur ces plateformes.

Python 3.11+, Node.js 22.12+ ou 24+, npm fourni avec Node. Codex CLI est requis uniquement pour préparer les documents via ChatGPT/Codex.

```sh
git clone https://github.com/Driw0x/ai-job-application-workbench ai-job-application-workbench
cd ai-job-application-workbench
python -m venv .venv
```

PowerShell, depuis la racine :

```powershell
. .venv/Scripts/Activate.ps1
python -m pip install -r backend/requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --app-dir backend --env-file .env --host 127.0.0.1 --port 8000
```

Sur Linux/macOS, activez avec `source .venv/bin/activate` et copiez avec `cp .env.example .env` ; les commandes pip et uvicorn restent identiques. Dans un second terminal :

```sh
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Ouvrez `http://127.0.0.1:5173`. Documentation API : `http://127.0.0.1:8000/docs`. Gardez les deux services sur loopback ; l'application ne possède pas de contrôle d'accès multi-utilisateur.

## Configuration et Knowledge Base

Copiez `.env.example` localement. Définissez `KNOWLEDGE_BASE_PATH` vers votre KB externe, ou enregistrez son dossier dans Search profile. Le dossier enregistré est prioritaire sur l'environnement aux démarrages suivants et sert à la recherche comme aux documents. Sélectionnez les sources Markdown dans le dashboard. La KB peut décrire profil, compétences, formation, expérience, projets, contraintes et variantes de CV. Seuls les faits documentés peuvent être utilisés.

`DATABASE_PATH` remplace éventuellement `data/job_tracker.db`. Les artefacts restent dans `data/applications`. Aucune base personnelle n'est distribuée : le démarrage initialise un schéma vide. Gardez la KB hors du repository. La préparation de documents exige une KB compatible et vos propres templates DOCX ; voir le contrat ci-dessous. La recherche par prompt personnalisé fonctionne sans KB.

## Prompts personnalisés

Choisissez **Custom prompt**, saisissez les critères, puis enregistrez. Pour la découverte et l'évaluation de pertinence, le prompt enregistré constitue l'unique source de critères candidat/recherche. Aucun fichier KB, profil KB ni ancien prompt dérivé de la KB n'est fusionné implicitement. Les règles fixes de sécurité, schémas de sortie et preuves issues des offres restent applicables. Un prompt vide est refusé. Revenez à **Knowledge Base** pour restaurer la sélection enregistrée. Ce mode ne remplace pas la KB candidat nécessaire aux CV et lettres.

## ChatGPT / Codex et autres providers

Pour préparer via ChatGPT, installez Codex CLI selon votre méthode prise en charge habituelle et rendez `codex` accessible dans `PATH`. Dans les paramètres providers, choisissez **Continue with ChatGPT**, terminez OAuth dans le navigateur, puis sélectionnez explicitement provider, modèle disponible et effort de raisonnement. L'intégration ChatGPT/Codex utilise `codex app-server` pour la préparation et Responses en streaming pour la recherche Web directe compatible. La disponibilité dépend du compte et du modèle ; un abonnement ChatGPT ne garantit pas toutes les opérations. Aucun basculement automatique vers la facturation API.

Pour **OpenAI API**, saisissez une clé dans les paramètres, testez/enregistrez-la, acceptez les coûts API potentiels, puis sélectionnez OpenAI et un modèle disponible. Responses, Structured Outputs, raisonnement et recherche Web sont utilisés selon leurs capacités. L'API peut être facturée indépendamment de l'abonnement ChatGPT. Anthropic, Gemini et DeepSeek sont également pris en charge ; leurs capacités sont vérifiées, notamment l'absence de recherche Web directe DeepSeek.

Sélectionnez explicitement provider et modèle actifs. Des overrides par étape et un fallback activé séparément sont disponibles ; le fallback est désactivé par défaut. Configurez votre instance SearXNG ou choisissez **AI_DIRECT** avec un provider Web compatible. L'évaluation structurée utilise ensuite le pipeline commun.

Les clés API utilisent le keyring système, service `AIJobApplicationWorkbench`, sans repli en texte brut. Les fichiers OAuth ChatGPT se trouvent dans `%LOCALAPPDATA%/AIJobApplicationWorkbench/chatgpt/` sous Windows ou `~/.config/AIJobApplicationWorkbench/chatgpt/` ailleurs. Les credentials sont stockés localement pour cette application et ne sont jamais commités dans le repository. La déconnexion supprime l'état OAuth local et tente une révocation.

## CV, lettres et refresh

**REUSE** copie un CV existant adapté. **ADAPT** conserve son ensemble de projets en adaptant les faits documentés. **CREATE** choisit quatre projets pertinents documentés lorsqu'ils existent, sinon uniquement les projets pertinents disponibles, de un à quatre ; il peut enregistrer une variante réutilisable. Aucun quatrième projet inventé. Le contexte de lettre utilise les mêmes projets sélectionnés, avec cinq paragraphes fondés sur les sources.

La préparation et le refresh utilisent une formulation naturelle destinée aux recruteurs, des faits vérifiables, des métriques utiles et lisibles, sans logs ni benchmarks bruts, avec des noms de projets compréhensibles et une revue éditoriale finale. La mise en page DOCX, l'équilibrage des compétences et la validation sont pris en charge. Le CV suit la langue du template ; la lettre suit celle de l'offre. L'interface et les titres des analyses, profils d'entreprise et préparations d'entretien sont en anglais.

Actualisez les CV REVIEW/SUBMIT éligibles non envoyés sans recréer les candidatures. Inspectez le dry-run, générez une version à relire, relisez manuellement les DOCX, puis appliquez :

```sh
python backend/scripts/refresh_cvs.py
python backend/scripts/refresh_cvs.py --stage data/cv-review
python backend/scripts/refresh_cvs.py --apply data/cv-review
```

Les scripts utilisent les variables d'environnement exportées ou la configuration du dashboard ; ils ne chargent pas `.env` eux-mêmes. Passez `--knowledge-base` et `--database` si nécessaire. Le refresh de candidature conserve les projets existants et leur ordre, puis peut ajouter des projets pertinents documentés pour atteindre quatre. Statut, offre, historique, métadonnées de décision, variante originale et autres documents restent intacts. Le refresh des variantes globales (`--include-variants`) conserve exactement l'ensemble et l'ordre du manifeste. Toute modification des fichiers ou de la candidature invalide le plan relu. Le script de nettoyage commence en dry-run ; examinez son `--help` et son rapport avant toute modification. Le script historique de migration des lettres écrit dès son invocation et exige la configuration KB/base exportée.

L'application inclut des viewers Markdown et DOCX avec navigation intégrée. La conversion PDF essaie Microsoft Word sous Windows, puis LibreOffice. Installez séparément un convertisseur si nécessaire ; son absence produit une erreur explicite. Relisez pagination et contenu avant utilisation.

## Consommation de tokens

Les vues Numbers/Charts affichent les totaux recherche versus préparation de documents et les graphiques par date. Le registre conserve provider/modèle/étape ; la découverte expose aussi les métadonnées Web observées. Une consommation inconnue reste indisponible ; un zéro observé reste zéro. Les filtres de dates utilisent UTC. Aucun prix inventé, service analytique externe ni télémétrie n'est ajouté. Les requêtes providers transmettent le contexte explicitement nécessaire au workflow choisi.

## Tests

```sh
cd backend
python -m pytest -q --tb=short
cd ../frontend
npm test
npm run typecheck
npm run build
```

Les tests utilisent KB/templates synthétiques et bases temporaires, sans credentials réels ni appels facturables. Les interactions frontend utilisent Vitest/jsdom ; aucun framework navigateur supplémentaire n'est requis. Sous Windows restreint, définissez `TEMP` et `TMP` vers un dossier temporaire accessible hors du repository. Un smoke test réel provider/OAuth exige votre compte ; le PDF exige un convertisseur.

## Sécurité et usage responsable

Ne commitez jamais `.env`, clés API, credentials OAuth, tokens, KB/prompts privés, CV/lettres personnels, outputs générés ou bases SQLite personnelles. `.gitignore` exclut les chemins usuels et caches ; inspectez les fichiers préparés avant publication. Aucun template personnel ni base source n'est inclus. La création de variantes écrit volontairement CV et manifeste dans la KB externe configurée : sauvegardez-la.

Validez chaque affirmation et document avant utilisation. Le grounding ne remplace pas la relecture humaine. L'application ne fournit aucun mécanisme d'envoi automatique ou de candidature en masse. Vous restez responsable des éléments envoyés. Le code du projet est disponible sous [licence MIT](LICENSE). Les dépendances tierces conservent leurs propres licences ; leurs sources et les builds ne sont pas inclus dans ce repository.

## Documentation

- [Knowledge Base externe et contrat des templates](docs/knowledge-base.fr.md)
- [AI Knowledge Workflows](https://github.com/Driw0x/ai-knowledge-workflows) — Dépôt compagnon facultatif consacré à des workflows IA réutilisables autour des connaissances.