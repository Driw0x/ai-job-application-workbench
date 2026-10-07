from __future__ import annotations

import copy
import io
import logging
import re
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from docx import Document


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
logger = logging.getLogger(__name__)
CV_HEADINGS = {
    "PROFILE": "PROFIL", "SKILLS": "COMPÉTENCES", "LANGUAGES": "LANGUES",
    "SELECTED PROJECTS": "PROJETS SÉLECTIONNÉS", "EDUCATION": "FORMATION",
}



class CvDocumentError(ValueError):
    pass


def canonical_project_id(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")


def normalized_project_text(value: str) -> str:
    return canonical_project_id(value).replace("-", "")


def project_catalog(repo: Path) -> dict[str, str]:
    root = repo / "02_Projects"
    return {
        canonical_project_id(path.name): path.name
        for path in sorted(root.iterdir())
        if path.is_dir() and any(path.glob("*.md"))
    } if root.is_dir() else {}


def cv_template_path(repo: Path) -> Path:
    directory = repo / "01_Career" / "CV" / "Templates"
    preferred = directory / "resume-template.docx"
    if preferred.is_file():
        return preferred
    candidates = sorted(directory.glob("*.docx"))
    return candidates[0] if len(candidates) == 1 else preferred


def cv_variants(repo: Path) -> dict[str, dict[str, object]]:
    manifest = repo / "01_Career" / "CV" / "CV_Variants.md"
    generated = repo / "01_Career" / "CV" / "Generated"
    if not manifest.is_file():
        return {}
    sections = re.split(r"(?m)^## (?:Variant|Variante) ", manifest.read_text(encoding="utf-8"))[1:]
    variants = {}
    for section in sections:
        lines = section.splitlines()
        variant = lines[0].split(" ", 1)[0]
        match = re.search(
            r"(?m)^(?:Projects|Projets) :\s*\n((?:\s*\d+\. [^\r\n]+(?:\n|$))+)",
            section,
        )
        path = generated / f"resume_{variant}.docx"
        if not path.is_file():
            candidates = sorted(generated.glob(f"*_{variant}.docx"))
            if len(candidates) == 1:
                path = candidates[0]
        if match and path.is_file():
            projects = [canonical_project_id(line.split(". ", 1)[1].strip()) for line in match[1].splitlines() if line.strip()]
            if 1 <= len(projects) <= 4 and len(set(projects)) == len(projects):
                variants[variant] = {"path": path, "project_order": projects, "project_set": frozenset(projects)}
    return variants


def next_variant_name(variants: dict[str, object]) -> str:
    used = set(variants)
    for code in range(ord("A"), ord("Z") + 1):
        if (candidate := chr(code)) not in used:
            return candidate
    raise CvDocumentError("No variant identifier available")


def register_variant(repo: Path, variant: str, project_order: list[str], catalog: dict[str, str], headline: str) -> None:
    manifest = repo / "01_Career" / "CV" / "CV_Variants.md"
    projects = "\n".join(f"{index}. {catalog[project_id]}" for index, project_id in enumerate(project_order, 1))
    entry = (
        f"\n## Variant {variant} — {headline}\n\nProjects :\n\n{projects}\n\n"
        "Purpose:\n\n- Reusable variant created by AI Job Application Workbench.\n"
    )
    current = manifest.read_text(encoding="utf-8") if manifest.is_file() else "# CV Variants\n"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(current.rstrip() + "\n" + entry, encoding="utf-8")


def _parse_xml(xml: bytes) -> ET.Element:
    for _, (prefix, uri) in ET.iterparse(io.BytesIO(xml), events=("start-ns",)):
        if not prefix.startswith("ns"):
            ET.register_namespace(prefix, uri)
    return ET.fromstring(xml)


def document_text(path: Path) -> str:
    try:
        with ZipFile(path) as archive:
            if archive.testzip() is not None or "word/document.xml" not in archive.namelist():
                raise CvDocumentError("Invalid or incomplete DOCX")
            root = ET.fromstring(archive.read("word/document.xml"))
    except (BadZipFile, KeyError, ET.ParseError, OSError) as error:
        raise CvDocumentError(f"Unreadable DOCX : {error}") from error
    return "\n".join(
        text for paragraph in root.iter(W + "p")
        if (text := "".join(node.text or "" for node in paragraph.iter(W + "t")).strip())
    )


def validate_docx(path: Path) -> None:
    if not path.is_file() or path.suffix.lower() != ".docx":
        raise CvDocumentError("Generated resume is not a DOCX file")
    if not document_text(path).strip():
        raise CvDocumentError("Generated resume is empty")


def validate_cv_layout(path: Path) -> None:
    """Prevent REUSE from bypassing the current content layout."""
    lines = [CV_HEADINGS.get(line, line) for line in document_text(path).splitlines()]
    try:
        skills = lines[lines.index("COMPÉTENCES") + 1:lines.index("LANGUES")]
        projects = lines[lines.index("PROJETS SÉLECTIONNÉS") + 1:lines.index("FORMATION")]
        counts = []
        for line in skills:
            if line.startswith("• "):
                counts[-1] += 1
            else:
                counts.append(0)
        if len(counts) != 4 or any(count < 3 or count > 5 for count in counts):
            raise ValueError
        cursor = 0
        while cursor < len(projects):
            if projects[cursor].startswith("• ") or projects[cursor + 1].startswith("• "):
                raise ValueError
            cursor += 2  # Title followed by a distinct general description.
            bullets = 0
            while cursor < len(projects) and projects[cursor].startswith("• "):
                bullets += 1
                cursor += 1
            if bullets not in {2, 3}:
                raise ValueError
        if not projects:
            raise ValueError
    except (ValueError, IndexError) as error:
        raise CvDocumentError("Legacy or incompatible resume layout: choose ADAPT with descriptions and 3 to 5 skills per group") from error


def validate_cv_projects(
    path: Path, expected: list[str], catalog: dict[str, str], rendered_titles: dict[str, str] | None = None,
) -> None:
    lines = [CV_HEADINGS.get(line, line) for line in document_text(path).splitlines()]
    normalized = [canonical_project_id(line) for line in lines]
    try:
        start = normalized.index("projets-selectionnes") + 1
        end = normalized.index("formation", start)
    except ValueError as error:
        raise CvDocumentError("Resume projects section not found") from error
    section = normalized_project_text("\n".join(lines[start:end]))
    titles = {**catalog, **(rendered_titles or {})}
    found = [project_id for project_id, title in titles.items() if normalized_project_text(title) in section]
    if not 1 <= len(expected) <= 4 or len(found) != len(expected) or set(found) != set(expected):
        raise CvDocumentError("Resume must contain exactly the expected projects (1 to 4 distinct projects)")


def cv_project_order(path: Path, catalog: dict[str, str]) -> list[str]:
    lines = [CV_HEADINGS.get(line, line) for line in document_text(path).splitlines()]
    normalized = [canonical_project_id(line) for line in lines]
    try:
        start = normalized.index("projets-selectionnes") + 1
        end = normalized.index("formation", start)
    except ValueError as error:
        raise CvDocumentError("Resume projects section not found") from error
    order = []
    for line in lines[start:end]:
        normalized_line = normalized_project_text(line)
        matches = [key for key, title in catalog.items() if normalized_line.startswith(normalized_project_text(title))]
        if matches:
            order.append(max(matches, key=lambda key: len(catalog[key])))
    if not 1 <= len(order) <= 4 or len(set(order)) != len(order):
        raise CvDocumentError("Resume must contain exactly the expected projects (1 to 4 distinct projects)")
    validate_cv_projects(path, order, catalog)
    return order


def _set_text(paragraph: ET.Element, values: list[str]) -> None:
    nodes = list(paragraph.iter(W + "t"))
    if len(nodes) < len(values):
        raise CvDocumentError("Incompatible resume template structure")
    for node, value in zip(nodes, values):
        node.text = value
    for node in nodes[len(values):]:
        node.text = ""


def _paragraph_text(paragraph: ET.Element) -> str:
    return "".join(node.text or "" for node in paragraph.iter(W + "t")).strip()


def _find(paragraphs: list[ET.Element], text: str) -> int:
    try:
        return next(index for index, paragraph in enumerate(paragraphs) if CV_HEADINGS.get(_paragraph_text(paragraph), _paragraph_text(paragraph)) == text)
    except StopIteration as error:
        raise CvDocumentError(f"Section '{text}' missing from resume template") from error


def _estimated_height(paragraphs, width: float) -> float:
    """Estimate wrapping and vertical spacing without requiring an Office engine."""
    height = previous_after = 0.0
    for paragraph in paragraphs:
        style = paragraph.style
        normal = paragraph.part.document.styles["Normal"]
        size = max((run.font.size.pt if run.font.size else
                    (style.font.size or normal.font.size).pt for run in paragraph.runs), default=8.5)
        formatting = paragraph.paragraph_format
        inherited = style.paragraph_format
        before = formatting.space_before if formatting.space_before is not None else inherited.space_before
        after = formatting.space_after if formatting.space_after is not None else inherited.space_after
        if after is None:
            after = normal.paragraph_format.space_after
        indent = formatting.left_indent.pt if formatting.left_indent else 0
        available = max(1, width - indent)
        lines = 0
        # ponytail: approximate glyph widths; final PDF review remains necessary for Office font substitution.
        for line in paragraph.text.split("\n"):
            used = 0.0
            lines += 1
            for word in line.split():
                word_width = sum(0.25 if char in "ilI.,:;'!|" else
                                 0.8 if char in "MW@" else 0.4 for char in word) * size
                if used and used + size * 0.25 + word_width > available:
                    lines += 1
                    used = 0
                while word_width > available:
                    lines += 1
                    word_width -= available
                used += word_width + size * 0.25
        leading = formatting.line_spacing if formatting.line_spacing is not None else inherited.line_spacing
        # Calibrated on the existing CV template and its validated one-page PDF.
        line_height = leading.pt if hasattr(leading, "pt") else size * 1.65 * (leading or 1)
        height += max(previous_after, before.pt if before else 0) + lines * line_height
        previous_after = after.pt if after else 0
    return height + previous_after


def _balance_skills(root: ET.Element, entries: list, groups: list[list[ET.Element]]) -> None:
    def measurements():
        stream = io.BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            for info, data in entries:
                archive.writestr(info, ET.tostring(root) if info.filename == "word/document.xml" else data)
        document = Document(stream)
        left, right = document.tables[0].rows[0].cells
        widths = []
        for cell in (left, right):
            margins = cell._tc.tcPr.find(W + "tcMar")
            padding = sum(int(node.get(W + "w", "0")) / 20 for node in margins
                          if node.tag in {W + "start", W + "end", W + "left", W + "right"}) if margins is not None else 0
            widths.append(cell.width.pt - padding)
        section = document.sections[0]
        header = _estimated_height(document.paragraphs, section.page_width.pt - section.left_margin.pt - section.right_margin.pt)
        return (_estimated_height(left.paragraphs, widths[0]), _estimated_height(right.paragraphs, widths[1]),
                section.page_height.pt - section.top_margin.pt - section.bottom_margin.pt - header - 12)

    left_height, right_height, available = measurements()
    while left_height > right_height + 12 or left_height > available:
        candidates = [group for group in groups if len(group) > 3]
        if not candidates:
            break
        # Items arrive in descending relevance; never remove the first three of any category.
        group = max(candidates, key=lambda items: (len(items), len(_paragraph_text(items[-1]))))
        paragraph = group.pop()
        next(parent for parent in root.iter() if paragraph in list(parent)).remove(paragraph)
        left_height, right_height, available = measurements()
    if max(left_height, right_height) > available:
        raise CvDocumentError("Resume too long for one page: condense profile and projects without reducing font size or margins")
    if abs(left_height - right_height) > 36:
        logger.warning("Resume columns remain unbalanced: left %.0f pt, right %.0f pt; condense content if necessary.",
                       left_height, right_height)


def create_cv(source: Path, destination: Path, content: object, replace: bool = False) -> None:
    if destination.exists() and not replace:
        raise CvDocumentError(f"Target resume already exists : {destination.name}")
    validate_docx(source)
    try:
        with ZipFile(source) as archive:
            xml = archive.read("word/document.xml")
            entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
        root = _parse_xml(xml)
        paragraphs = list(root.iter(W + "p"))

        _set_text(paragraphs[2], [content.headline])
        profile = _find(paragraphs, "PROFIL")
        _set_text(paragraphs[profile + 1], [content.profile])

        skills = _find(paragraphs, "COMPÉTENCES")
        languages = _find(paragraphs, "LANGUES")
        parents = {child: parent for parent in root.iter() for child in parent}
        skill_section = parents[paragraphs[skills]]
        skill_title = paragraphs[skills + 1]
        skill_bullet = paragraphs[skills + 2]
        insert_at = list(skill_section).index(paragraphs[skills]) + 1
        for paragraph in paragraphs[skills + 1:languages]:
            skill_section.remove(paragraph)
        skill_rows = []
        for group in content.skill_groups:
            title = copy.deepcopy(skill_title)
            _set_text(title, [group.title])
            skill_section.insert(insert_at, title)
            insert_at += 1
            rows = []
            for item in group.items:
                bullet = copy.deepcopy(skill_bullet)
                _set_text(bullet, ["• ", item])
                skill_section.insert(insert_at, bullet)
                rows.append(bullet)
                insert_at += 1
            skill_rows.append(rows)

        projects = _find(paragraphs, "PROJETS SÉLECTIONNÉS")
        formation = _find(paragraphs, "FORMATION")
        parents = {child: parent for parent in root.iter() for child in parent}
        section = parents[paragraphs[projects]]
        if parents[paragraphs[formation]] is not section:
            raise CvDocumentError("Incompatible projects and education sections")
        title_template = paragraphs[projects + 1]
        bullet_template = next(p for p in paragraphs[projects + 2:formation] if _paragraph_text(p).startswith("•"))
        insert_at = list(section).index(paragraphs[projects]) + 1
        for paragraph in paragraphs[projects + 1:formation]:
            section.remove(paragraph)
        for project in content.projects:
            title = copy.deepcopy(title_template)
            _set_text(title, [project.title, f"  |  {project.technologies}"])
            section.insert(insert_at, title)
            insert_at += 1
            description = copy.deepcopy(bullet_template)
            _set_text(description, ["", project.description])
            indentation = description.find(f"{W}pPr/{W}ind")
            if indentation is not None:
                description.find(W + "pPr").remove(indentation)
            for properties in description.iter(W + "rPr"):
                for tag, value in (("i", "1"), ("b", "0"), ("color", "637083")):
                    node = properties.find(W + tag)
                    if node is None:
                        node = ET.SubElement(properties, W + tag)
                    node.set(W + "val", value)
            section.insert(insert_at, description)
            insert_at += 1
            for value in project.bullets:
                bullet = copy.deepcopy(bullet_template)
                _set_text(bullet, ["• ", value])
                section.insert(insert_at, bullet)
                insert_at += 1

        _balance_skills(root, entries, skill_rows)
        updated_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    except CvDocumentError:
        raise
    except (BadZipFile, KeyError, ET.ParseError, OSError, AttributeError, IndexError, ValueError) as error:
        raise CvDocumentError(f"Unable to adapt DOCX : {error}") from error

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".docx", delete=False) as handle:
            temporary = Path(handle.name)
        with ZipFile(temporary, "w", ZIP_DEFLATED) as output:
            for info, data in entries:
                output.writestr(info, updated_xml if info.filename == "word/document.xml" else data)
        validate_docx(temporary)
        validate_cv_layout(temporary)
        temporary.replace(destination)
    except (OSError, BadZipFile, CvDocumentError) as error:
        if temporary:
            temporary.unlink(missing_ok=True)
        if isinstance(error, CvDocumentError):
            raise
        raise CvDocumentError(f"Unable to write resume : {error}") from error
