// @vitest-environment jsdom

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrowserRouter } from "react-router-dom";

vi.mock("docx-preview", () => ({ renderAsync: vi.fn() }));

import { renderAsync } from "docx-preview";
import App, { AiUsageCard, ApplicationList, CoverLetterViewer, CvViewer, Detail, DocxPreview, FileLink, Kanban, MarkdownViewer } from "./App";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const application = {
  id: 7, company: "Example Research", position: "Stage ML", url: "https://example.test", source: "Test",
  location: "Paris", detected_at: "2026-10-04", status: "PREPARED", notes: "", updated_at: "2026-10-04",
  analysis_path: "analysis.md", interview_prep_path: "interview-prep.md", cv_path: "cv/test.docx",
  cover_letter_path: "lettre-motivation.docx", cover_letter_word_count: 320,
  company_description: "Example Research mène des recherches of l’énergie et la mobilité.",
  company_postal_address: "1 avenue du Test\n92852 Rueil-Malmaison Cedex",
  company_domain: "SAF et machine learning scientifique",
  company_completed_achievements: ["Base scientifique existante"],
  company_planned_developments: ["Prédiction de propriétés"],
  company_comparable_actors: ["Example Laboratory"],
};
let host: HTMLDivElement; let root: Root;

beforeEach(() => {
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  vi.mocked(renderAsync).mockReset();
  vi.mocked(renderAsync).mockResolvedValue(undefined);
  vi.stubGlobal("print", vi.fn());
  Object.defineProperties(URL, {
    createObjectURL: { configurable: true, value: vi.fn(() => "blob:test") },
    revokeObjectURL: { configurable: true, value: vi.fn() },
  });
});

afterEach(async () => {
  await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); window.history.replaceState(null, "", "/");
});

describe("suivi contextuel", () => {
  const callbacks = {
    openDocument: vi.fn(), mutate: vi.fn().mockResolvedValue(null),
    save: vi.fn().mockImplementation(async (_id, body) => ({ ...application, ...body })),
    prepare: vi.fn().mockResolvedValue(undefined), preparing: false, back: vi.fn(),
  };

  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, text: async () => "" }));
    vi.stubGlobal("confirm", vi.fn(() => true)); callbacks.mutate.mockClear(); callbacks.prepare.mockClear();
  });

  it.each([
    ["ELIGIBLE", "Eligible", "No restriction detected."],
    ["ELIGIBILITY_UNCERTAIN", "⚠ Eligibility uncertain", "Some requirements need verification."],
    ["INELIGIBLE", "Ineligible", "A restriction prevents this application."],
  ] as const)("sélectionne depuis le détail sauf inéligibilité explicite : %s", async (eligibility_status, label, explanation) => {
    const detected = { ...application, status: "DETECTED", eligibility_status, eligibility_reason: "Eligibility determined by job screening." };
    await act(async () => root.render(<Detail application={detected} {...callbacks} />)); await flush();
    const select = [...host.querySelectorAll("button")].find(button => button.textContent === "Select")!;
    expect(select.disabled).toBe(eligibility_status === "INELIGIBLE");
    const notice = host.querySelector(".eligibility-notice")!;
    expect(notice.querySelector("strong")?.textContent).toBe(label);
    expect(notice.querySelector("small.muted")?.textContent).toBe(explanation);
    expect(notice.querySelector("strong")?.nextElementSibling).toBe(notice.querySelector("small"));
    expect(host.textContent).not.toMatch(/ELIGIBLE|INELIGIBLE|ELIGIBILITY_UNCERTAIN|screening/);
    await act(async () => select.click());
    if (eligibility_status === "INELIGIBLE") expect(callbacks.prepare).not.toHaveBeenCalled();
    else expect(callbacks.prepare).toHaveBeenCalledWith(detected);
    const ignore = [...host.querySelectorAll("button")].find(button => button.textContent === "Ignore")!;
    expect(ignore.disabled).toBe(false);
    await act(async () => ignore.click());
    expect(callbacks.mutate).toHaveBeenCalledWith("/applications/7/ignore", { details: "Ignored by user" });
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("affiche une éligibilité lisible par défaut sans statut renseigné", async () => {
    await act(async () => root.render(<Detail application={application} {...callbacks} />)); await flush();
    const notice = host.querySelector(".eligibility-notice")!;
    expect(notice.querySelector("strong")?.textContent).toBe("Eligible");
    expect(notice.querySelector("small")?.textContent).toBe("No restriction detected.");
    expect(notice.querySelector("strong")?.className).toBe("success");
  });

  it("sépare le CV du suivi pour PREPARED", async () => {
    const prepared = { ...application, cv_action: "ADAPT" as const, cv_variant: "D", next_action: "Relire l'analyse, le CV et la lettre de motivation puis valider" };
    await act(async () => root.render(<Detail application={prepared} {...callbacks} />)); await flush();
    const headings = [...host.querySelectorAll("h3")];
    expect(headings.some(item => item.textContent === "Preparation")).toBe(true);
    const followUp = headings.find(item => item.textContent === "Track")?.closest("form");
    expect(followUp?.textContent).not.toContain("Variant CV");
    expect(followUp?.textContent).not.toContain("Fichier CV");
    expect(followUp?.textContent).not.toContain("Interview date");
    expect(host.textContent).toContain("test.docx");
    expect((followUp?.querySelector('input[readonly]') as HTMLInputElement).value).toBe("Relire l'analyse, le CV et la lettre de motivation puis valider");
    expect(host.textContent).toContain("Open cover letter");
    expect(host.textContent).not.toContain("Ouvrir candidature");
    expect(host.textContent).toContain("SAF et machine learning scientifique");
    expect(host.textContent).toContain("Base scientifique existante");
    expect(host.textContent).toContain("Prédiction de propriétés");
    expect(host.textContent).toContain("Example Laboratory");
  });

  it("impose l’envoi manuel avant confirmation", async () => {
    const awaiting = { ...application, status: "AWAITING_VALIDATION", next_action: "Envoyer la candidature of le site de l'entreprise puis confirmer l'envoi", missing_information: [{ field: "COMPANY_ADDRESS", label: "Company address" }, { field: "COMPANY_ZIP_CODE", label: "Company postal code" }] };
    await act(async () => root.render(<Detail application={awaiting} {...callbacks} />)); await flush();
    expect(host.textContent).toContain("Open job");
    expect(host.textContent).toContain("Confirm submission");
    expect(host.textContent).toContain("Open cover letter");
    expect(host.textContent).not.toContain("Démarrer la candidature");
    expect(host.textContent).toContain("Submit it on the company website");
    expect(host.textContent).toContain("Missing information");
    expect(host.textContent).toContain("Company address");
    expect(host.textContent).toContain("Company postal code");
    expect(host.textContent).toContain("These fields were left blank in the generated documents.");
    const confirm = [...host.querySelectorAll("button")].find(button => button.textContent === "Confirm submission") as HTMLButtonElement;
    await act(async () => confirm.click());
    expect(window.confirm).toHaveBeenCalledWith("Confirm that you have submitted this application?");
    expect(callbacks.mutate).toHaveBeenCalledWith("/applications/7/submission/confirm");
  });

  it("affiche envoi, entretien et clôture selon le statut", async () => {
    await act(async () => root.render(<Detail application={{ ...application, status: "SENT", sent_at: "2026-10-04T10:00:00Z", next_action: "Relancer si aucune réponse", next_action_at: "2026-10-14T10:00:00Z" }} {...callbacks} />));
    expect(host.textContent).toContain("Submission date"); expect((host.querySelector('input[readonly]') as HTMLInputElement).value).toBe("Relancer si aucune réponse");
    await act(async () => root.render(<Detail application={{ ...application, status: "HR_INTERVIEW", next_action: "Préparer l'entretien" }} {...callbacks} />));
    expect(host.textContent).toContain("Interview date"); expect(host.textContent).toContain("Interview notes");
    await act(async () => root.render(<Detail application={{ ...application, status: "REJECTED", next_action: undefined }} {...callbacks} />));
    expect(host.textContent).toContain("Application closed."); expect(host.textContent).not.toContain("Next action");
  });

  const notes = () => [...host.querySelectorAll("label")].find(label => label.firstChild?.textContent === "General notes")?.querySelector("textarea") as HTMLTextAreaElement;
  async function changeNotes(value: string) {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(notes(), value);
      notes().dispatchEvent(new InputEvent("input", { bubbles: true }));
    });
  }
  const submitNotes = () => host.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

  it("conserve le brouillon pendant le polling et le réinitialise au changement de candidature", async () => {
    await act(async () => root.render(<Detail application={application} {...callbacks} />));
    await changeNotes("Brouillon local");
    await act(async () => root.render(<Detail application={{ ...application, notes: "Valeur backend", updated_at: "2026-10-07" }} {...callbacks} />));
    expect(notes().value).toBe("Brouillon local");
    await act(async () => root.render(<Detail application={{ ...application, id: 8, notes: "Autre candidature" }} {...callbacks} />));
    expect(notes().value).toBe("Autre candidature");
  });

  it("accepte les valeurs sauvegardées puis les rafraîchissements lorsque le brouillon est enregistré", async () => {
    const save = vi.fn().mockResolvedValue({ ...application, notes: "Valeur enregistrée" });
    await act(async () => root.render(<Detail application={application} {...callbacks} save={save} />));
    await changeNotes("Saisie"); await act(async () => { submitNotes(); });
    expect(save).toHaveBeenCalledWith(application.id, expect.objectContaining({ notes: "Saisie" }));
    expect(notes().value).toBe("Valeur enregistrée");
    expect(host.textContent).toContain("Changes saved");
    await act(async () => root.render(<Detail application={{ ...application, notes: "Nouvelle valeur backend" }} {...callbacks} save={save} />));
    expect(notes().value).toBe("Nouvelle valeur backend");
  });

  it("préserve les modifications saisies pendant une sauvegarde et après une erreur", async () => {
    let finish!: (value: typeof application) => void;
    const save = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
      .mockRejectedValueOnce(new Error("Sauvegarde indisponible"));
    await act(async () => root.render(<Detail application={application} {...callbacks} save={save} />));
    await changeNotes("Première saisie"); act(() => { submitNotes(); });
    await changeNotes("Saisie suivante");
    await act(async () => finish({ ...application, notes: "Première saisie" }));
    expect(notes().value).toBe("Saisie suivante");
    await act(async () => { submitNotes(); });
    expect(host.textContent).toContain("Sauvegarde indisponible");
    await act(async () => root.render(<Detail application={{ ...application, notes: "Backend" }} {...callbacks} save={save} />));
    expect(notes().value).toBe("Saisie suivante");
  });

  it("ignore une sauvegarde terminée après le changement de candidature", async () => {
    let finish!: (value: typeof application) => void;
    const save = vi.fn(() => new Promise<typeof application>(resolve => { finish = resolve; }));
    await act(async () => root.render(<Detail application={application} {...callbacks} save={save} />));
    await changeNotes("Ancien brouillon"); act(() => { submitNotes(); });
    await act(async () => root.render(<Detail application={{ ...application, id: 8, notes: "Autre candidature" }} {...callbacks} save={save} />));
    await act(async () => finish({ ...application, notes: "Ancienne sauvegarde" }));
    expect(notes().value).toBe("Autre candidature");
    expect(host.textContent).not.toContain("Changes saved");
  });

  it("ignore les anciens fichiers et signale une panne réseau locale", async () => {
    let finish!: (value: Response) => void;
    const old = new Promise<Response>(resolve => { finish = resolve; });
    vi.stubGlobal("fetch", vi.fn(async input => String(input).includes("/applications/7/") ? old
      : { ok: true, text: async () => "# Document actuel" } as Response));
    await act(async () => root.render(<Detail application={application} {...callbacks} />));
    await act(async () => root.render(<Detail application={{ ...application, id: 8 }} {...callbacks} />));
    await act(async () => finish({ ok: true, text: async () => "# Ancien document" } as Response));
    expect(host.querySelector(".documents")?.textContent).toContain("Document actuel");
    expect(host.querySelector(".documents")?.textContent).not.toContain("Ancien document");
    vi.mocked(fetch).mockRejectedValue(new Error("Réseau indisponible"));
    await act(async () => root.render(<Detail application={{ ...application, id: 9 }} {...callbacks} />));
    expect(host.querySelector('[role="alert"]')?.textContent).toBe("Application files indisponibles : Réseau indisponible");
  });
});

describe("actions Kanban SEARCH", () => {
  const callbacks = {
    open: vi.fn(), openDocument: vi.fn(), prepare: vi.fn().mockResolvedValue(undefined),
    ignore: vi.fn().mockResolvedValue(undefined), mutate: vi.fn().mockResolvedValue(null),
  };

  beforeEach(() => Object.values(callbacks).forEach(callback => callback.mockClear()));

  it.each(["ELIGIBLE", "ELIGIBILITY_UNCERTAIN", "INELIGIBLE"] as const)("ouvre directement l’URL externe sans déclencher une action métier : %s", async eligibility_status => {
    const detected = { ...application, status: "DETECTED", eligibility_status };
    await act(async () => root.render(<Kanban applications={[detected]} preparing={new Set()} canPrepare {...callbacks} />));
    const actions = host.querySelector(".ticket-actions") as HTMLElement;
    expect([...actions.querySelectorAll("button")].map(button => button.textContent)).toEqual(["Select", "Ignore"]);
    const link = actions.querySelector("a") as HTMLAnchorElement;
    expect(link.textContent).toBe("View job ↗");
    expect(link.getAttribute("href")).toBe(application.url);
    expect(link.target).toBe("_blank");
    expect(link.rel).toBe("noopener noreferrer");
    link.addEventListener("click", event => event.preventDefault());
    await act(async () => link.click());
    expect(callbacks.open).not.toHaveBeenCalled();
    expect(callbacks.prepare).not.toHaveBeenCalled();
    expect(callbacks.ignore).not.toHaveBeenCalled();
    expect(callbacks.mutate).not.toHaveBeenCalled();
  });

  it.each([
    ["ELIGIBLE", "Eligible", "No restriction detected."],
    ["ELIGIBILITY_UNCERTAIN", "⚠ Eligibility uncertain", "Some requirements need verification."],
    ["INELIGIBLE", "Ineligible", "A restriction prevents this application."],
  ] as const)("sélectionne depuis le Kanban sauf inéligibilité explicite : %s", async (eligibility_status, label, explanation) => {
    const detected = { ...application, status: "DETECTED", eligibility_status, eligibility_reason: "Eligibility determined by job screening." };
    await act(async () => root.render(<Kanban applications={[detected]} preparing={new Set()} canPrepare {...callbacks} />));
    const actions = host.querySelector(".ticket-actions")!;
    const select = [...actions.querySelectorAll("button")].find(button => button.textContent === "Select")!;
    expect(select.disabled).toBe(eligibility_status === "INELIGIBLE");
    const notice = host.querySelector(".eligibility-notice");
    if (eligibility_status === "ELIGIBLE") expect(notice).toBeNull();
    else {
      expect(notice?.querySelector("strong.failure-text")?.textContent).toBe(label);
      expect(notice?.querySelector("small.muted")?.textContent).toBe(explanation);
      expect(notice?.querySelector("strong")?.nextElementSibling).toBe(notice?.querySelector("small"));
    }
    expect(host.textContent).not.toMatch(/ELIGIBLE|INELIGIBLE|ELIGIBILITY_UNCERTAIN|screening/);
    await act(async () => select.click());
    if (eligibility_status === "INELIGIBLE") expect(callbacks.prepare).not.toHaveBeenCalled();
    else expect(callbacks.prepare).toHaveBeenCalledWith(detected);
    const ignore = [...actions.querySelectorAll("button")].find(button => button.textContent === "Ignore")!;
    expect(ignore.disabled).toBe(false);
    await act(async () => ignore.click());
    expect(callbacks.ignore).toHaveBeenCalledWith(detected.id);
    expect(callbacks.open).not.toHaveBeenCalled();
    expect(callbacks.mutate).not.toHaveBeenCalled();
  });

  it.each([
    [undefined, "Some requirements need verification."],
    ["", "Some requirements need verification."],
    ["   ", "Some requirements need verification."],
    ["  Une habilitation de sécurité peut être nécessaire.  ", "Une habilitation de sécurité peut être nécessaire."],
  ])("affiche une explication utile, même sans raison : %s", async (eligibility_reason, explanation) => {
    const detected = { ...application, status: "DETECTED", eligibility_status: "ELIGIBILITY_UNCERTAIN" as const, eligibility_reason };
    await act(async () => root.render(<Kanban applications={[detected]} preparing={new Set()} canPrepare {...callbacks} />));
    const description = host.querySelector(".eligibility-notice small.muted")!;
    expect(description.textContent).toBe(explanation);
    expect(description.getAttribute("title")).toBe(explanation);
  });

  it("masque le lien lorsque l’URL est absente", async () => {
    await act(async () => root.render(<Kanban applications={[{ ...application, status: "DETECTED", url: "" }]} preparing={new Set()} canPrepare {...callbacks} />));
    expect(host.textContent).not.toContain("Voir l’job");
    expect(host.querySelector(".eligibility-notice")).toBeNull();
    expect([...host.querySelectorAll(".ticket-actions button")].map(button => button.textContent)).toEqual(["Select", "Ignore"]);
  });

  it("désactive la validation quand les artefacts sont incomplets", async () => {
    await act(async () => root.render(<Kanban applications={[{ ...application, artifacts: { complete: false, missing: ["cv.docx"], invalid: [] } }]} preparing={new Set()} canPrepare {...callbacks} />));
    const validate = [...host.querySelectorAll("button")].find(button => button.textContent === "Approve")!;
    expect(validate.disabled).toBe(true);
    await act(async () => validate.click());
    expect(callbacks.mutate).not.toHaveBeenCalled();
  });
});

describe("mapping Kanban", () => {
  const callbacks = {
    open: vi.fn(), openDocument: vi.fn(), prepare: vi.fn().mockResolvedValue(undefined),
    ignore: vi.fn().mockResolvedValue(undefined), mutate: vi.fn().mockResolvedValue(null),
  };

  it("place WITHDRAWN uniquement dans WITHDRAW sans déplacer les autres colonnes", async () => {
    const trackingStatuses = ["SENT", "ACKNOWLEDGED", "HR_INTERVIEW", "TECH_INTERVIEW", "FINAL_INTERVIEW", "OFFER", "REJECTED", "NO_RESPONSE"];
    const applications = [
      { ...application, id: 1, company: "Search", status: "DETECTED" },
      { ...application, id: 2, company: "Review", status: "PREPARED" },
      { ...application, id: 3, company: "Submit", status: "AWAITING_VALIDATION" },
      ...trackingStatuses.map((status, index) => ({ ...application, id: index + 4, company: status, status })),
      { ...application, id: 12, company: "Retirée", status: "WITHDRAWN" },
    ];
    await act(async () => root.render(<Kanban applications={applications} preparing={new Set()} canPrepare {...callbacks} />));
    const column = (name: string) => [...host.querySelectorAll(".kanban section")].find(section => section.querySelector("h3")?.textContent === name) as HTMLElement;
    expect([...host.querySelectorAll(".kanban h3")].map(heading => heading.textContent)).toEqual(["SEARCH", "REVIEW", "SUBMIT", "TRACK", "WITHDRAW"]);
    expect(column("SEARCH").textContent).toContain("Search");
    expect(column("REVIEW").textContent).toContain("Review");
    expect(column("SUBMIT").textContent).toContain("Submit");
    expect(column("TRACK").textContent).not.toContain("Retirée");
    trackingStatuses.forEach(status => expect(column("TRACK").textContent).toContain(status));
    expect(column("WITHDRAW").textContent).toContain("Retirée");
  });
});

describe("tri des candidatures", () => {
  const applications = [
    { ...application, id: 1, company: "zèbre", position: "Zulu", detected_at: "2026-10-10", status: "SENT", cv_variant: "Z" },
    { ...application, id: 2, company: "Alpha", position: "Yankee", detected_at: "2026-02-01", status: "PREPARED", cv_variant: "A" },
    { ...application, id: 3, company: "Bravo", position: "Xray", detected_at: "2026-01-15", status: "REJECTED" },
  ];
  const values = (column: number) => [...host.querySelectorAll("tbody tr")].map(row => row.children[column].textContent);
  const header = (label: string) => [...host.querySelectorAll("thead button")].find(button => button.textContent?.startsWith(label)) as HTMLButtonElement;

  it("trie Company par ordre ascendant", async () => {
    await act(async () => root.render(<ApplicationList applications={applications} open={vi.fn()} />));
    await act(async () => header("Company").click());
    expect(values(0)).toEqual(["Alpha", "Bravo", "zèbre"]);
    expect(header("Company").textContent).toBe("Company ↑");
    expect(header("Company").closest("th")?.getAttribute("aria-sort")).toBe("ascending");
  });

  it("trie Company par ordre descendant au second clic", async () => {
    await act(async () => root.render(<ApplicationList applications={applications} open={vi.fn()} />));
    await act(async () => { header("Company").click(); header("Company").click(); });
    expect(values(0)).toEqual(["zèbre", "Bravo", "Alpha"]);
  });

  it("trie Date chronologiquement et change de colonne", async () => {
    await act(async () => root.render(<ApplicationList applications={applications} open={vi.fn()} />));
    await act(async () => header("Date").click());
    expect(values(0)).toEqual(["Bravo", "Alpha", "zèbre"]);
    await act(async () => header("Position").click());
    expect(values(1)).toEqual(["Xray", "Yankee", "Zulu"]);
    expect(header("Date").closest("th")?.getAttribute("aria-sort")).toBe("none");
  });

  it("conserve tri avec filtre et place CV absent en fin", async () => {
    await act(async () => root.render(<ApplicationList applications={applications} open={vi.fn()} />));
    await act(async () => header("CV").click());
    expect(values(4)).toEqual(["A", "Z", "—"]);
    await act(async () => header("CV").click());
    expect(values(4)).toEqual(["Z", "A", "—"]);
    await act(async () => header("Position").click());
    const company = host.querySelector('input[placeholder="Company"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(company, "a");
      company.dispatchEvent(new window.InputEvent("input", { bubbles: true, inputType: "insertText", data: "a" }));
    });
    expect(values(0)).toEqual(["Bravo", "Alpha"]);
  });
});

describe("filtre de statut des candidatures", () => {
  const groups = [
    ["Search", ["DETECTED"]],
    ["Review", ["SHORTLISTED", "PREPARED"]],
    ["Submit", ["AWAITING_VALIDATION", "APPROVED", "SUBMITTING"]],
    ["Track", ["SENT", "ACKNOWLEDGED", "HR_INTERVIEW", "TECH_INTERVIEW", "FINAL_INTERVIEW", "OFFER", "REJECTED", "NO_RESPONSE"]],
    ["Withdrawn", ["WITHDRAWN"]],
    ["Ignored job", ["IGNORED"]],
  ] as const;
  const applications = groups.flatMap(([, statuses]) => statuses).map((status, index) => ({ ...application, id: index + 1, company: status, status }));
  const visibleCompanies = () => [...host.querySelectorAll("tbody tr")].map(row => row.children[0].textContent);
  const statusButton = (label: string) => [...host.querySelectorAll(".status-chip")].find(button => button.textContent === label) as HTMLButtonElement;
  const clickStatus = async (label: string) => act(async () => statusButton(label).click());
  const showAll = async () => {
    const select = host.querySelector(".pagination-bar select") as HTMLSelectElement;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set?.call(select, "all"); select.dispatchEvent(new Event("change", { bubbles: true })); });
  };

  it("affiche les chips et filtre chaque groupe", async () => {
    await act(async () => root.render(<ApplicationList applications={applications} open={vi.fn()} />));
    await showAll();
    expect([...host.querySelectorAll(".status-chip")].map(button => button.textContent)).toEqual(groups.map(([label]) => label));
    expect((host.querySelector(".pagination-bar select") as HTMLSelectElement).value).toBe("all");
    expect(host.textContent).not.toContain("Exclure des statuts");
    expect(visibleCompanies()).toEqual(applications.map(item => item.company));

    for (const [label, statuses] of groups) {
      await clickStatus(label);
      expect(visibleCompanies()).toEqual([...statuses]);
      expect(statusButton(label).getAttribute("aria-pressed")).toBe("true");
      await clickStatus(label);
    }
  });

  it("combine plusieurs statuts avec un OR", async () => {
    await act(async () => root.render(<ApplicationList applications={applications} open={vi.fn()} />));
    await clickStatus("Search");
    await clickStatus("Review");
    expect(visibleCompanies()).toEqual(["DETECTED", "SHORTLISTED", "PREPARED"]);
  });

  it("désactive une chip et vide tous les statuts sélectionnés", async () => {
    await act(async () => root.render(<ApplicationList applications={applications} open={vi.fn()} />));
    await showAll();
    await clickStatus("Search");
    await clickStatus("Review");
    await clickStatus("Search");
    expect(visibleCompanies()).toEqual(["SHORTLISTED", "PREPARED"]);

    await act(async () => ([...host.querySelectorAll("button")].find(button => button.textContent === "Clear") as HTMLButtonElement).click());
    expect(visibleCompanies()).toEqual(applications.map(item => item.company));
    expect([...host.querySelectorAll(".status-chip")].every(button => button.getAttribute("aria-pressed") === "false")).toBe(true);
    expect([...host.querySelectorAll("button")].some(button => button.textContent === "Clear")).toBe(false);
  });
});

describe("pagination des candidatures", () => {
  const applications = Array.from({ length: 27 }, (_, index) => ({ ...application, id: index + 1, company: `Company ${index + 1}` }));
  const visibleCompanies = () => [...host.querySelectorAll("tbody tr")].map(row => row.children[0].textContent);
  const button = (label: string) => [...host.querySelectorAll(".pagination-controls button")].find(item => item.textContent === label) as HTMLButtonElement;

  it.each([
    [0, "0 jobs"], [1, "1–1 of 1 job"], [7, "1–7 of 7 jobs"], [10, "1–10 of 10 jobs"],
    [11, "1–10 of 11 jobs"], [20, "1–10 of 20 jobs"], [21, "1–10 of 21 jobs"], [47, "1–10 of 47 jobs"],
  ])("gère %i résultat(s)", async (count, counter) => {
    const items = Array.from({ length: count }, (_, index) => ({ ...application, id: index + 1, company: `Company ${index + 1}` }));
    await act(async () => root.render(<ApplicationList applications={items} open={vi.fn()} />));
    expect(visibleCompanies()).toHaveLength(Math.min(count, 10)); expect(host.querySelector(".pagination-bar")?.textContent).toContain(counter);
  });

  it("affiche 10 jobs par page et permet la navigation", async () => {
    await act(async () => root.render(<ApplicationList applications={applications} open={vi.fn()} />));
    expect(visibleCompanies()).toEqual(applications.slice(0, 10).map(item => item.company));
    expect(host.querySelector(".pagination-bar")?.textContent).toContain("1–10 of 27 jobs");
    expect(button("‹ Previous").disabled).toBe(true);
    await act(async () => button("2").click());
    expect(visibleCompanies()).toEqual(applications.slice(10, 20).map(item => item.company));
    expect(host.querySelector(".pagination-bar")?.textContent).toContain("11–20 of 27 jobs");
    await act(async () => button("Next ›").click());
    expect(visibleCompanies()).toEqual(applications.slice(20).map(item => item.company));
    expect(button("Next ›").disabled).toBe(true);
  });

  it("affiche tout puis revient à la première page", async () => {
    await act(async () => root.render(<ApplicationList applications={applications} open={vi.fn()} />));
    await act(async () => button("2").click());
    const select = host.querySelector(".pagination-bar select") as HTMLSelectElement;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set?.call(select, "all"); select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(visibleCompanies()).toHaveLength(27); expect(host.querySelector(".pagination-controls")).toBeNull(); expect(host.querySelector(".pagination-bar")?.textContent).toContain("27 jobs");
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set?.call(select, "10"); select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(visibleCompanies()).toEqual(applications.slice(0, 10).map(item => item.company));
  });

  it("revient à la première page après filtrage", async () => {
    await act(async () => root.render(<ApplicationList applications={applications} open={vi.fn()} />));
    await act(async () => button("2").click());
    const search = host.querySelector('input[placeholder="Search"]') as HTMLInputElement;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(search, "Company 2"); search.dispatchEvent(new InputEvent("input", { bubbles: true })); });
    expect(visibleCompanies()[0]).toBe("Company 2"); expect(host.querySelector(".pagination-bar")?.textContent).toContain("1–9 of 9 jobs");
  });

  it("recule vers la dernière page valide lorsque la liste diminue", async () => {
    await act(async () => root.render(<ApplicationList applications={applications.slice(0, 21)} open={vi.fn()} />));
    await act(async () => button("3").click()); expect(visibleCompanies()).toEqual(["Company 21"]);
    await act(async () => root.render(<ApplicationList applications={applications.slice(0, 20)} open={vi.fn()} />));
    expect(visibleCompanies()).toEqual(applications.slice(10, 20).map(item => item.company));
    expect(host.querySelector(".pagination-bar")?.textContent).toContain("11–20 of 20 jobs");
  });

  it("compacte les numéros lorsqu’il existe beaucoup de pages", async () => {
    const items = Array.from({ length: 120 }, (_, index) => ({ ...application, id: index + 1, company: `Company ${index + 1}` }));
    await act(async () => root.render(<ApplicationList applications={items} open={vi.fn()} />));
    expect(host.querySelector(".pagination-controls")?.textContent).toContain("1"); expect(host.querySelector(".pagination-controls")?.textContent).toContain("…"); expect(button("12")).toBeTruthy();
  });
});

const zeroUsage = { input_tokens: 0, output_tokens: 0, total_tokens: 0, calls: 0, unknown_calls: 0 };
const emptyUsage = { period: "7d", timezone: "UTC", granularity: "day", total: zeroUsage, search: zeroUsage, documents: zeroUsage, daily: [] };
const recordedUsage = {
  ...emptyUsage,
  total: { input_tokens: 12_300, output_tokens: 600, total_tokens: 12_900, calls: 3, unknown_calls: 0 },
  search: { input_tokens: 12_000, output_tokens: 400, total_tokens: 12_400, calls: 2, unknown_calls: 0 },
  documents: { input_tokens: 300, output_tokens: 200, total_tokens: 500, calls: 1, unknown_calls: 0 },
  daily: [{ date: "2026-10-05", search_tokens: 6200, document_tokens: 200 }, { date: "2026-10-06", search_tokens: 6200, document_tokens: 300 }],
};

describe("consommation IA", () => {
  const control = (label: string) => [...host.querySelectorAll<HTMLButtonElement>(".ai-usage button")].find(button => button.textContent === label)!;
  const mockUsage = (data: unknown) => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => data });
    vi.stubGlobal("fetch", fetchMock); return fetchMock;
  };

  it("ouvre la section et affiche les chiffres, le détail et les valeurs exactes", async () => {
    const fetchMock = mockUsage(recordedUsage);
    await act(async () => root.render(<AiUsageCard />)); await flush();
    expect(host.querySelector<HTMLDetailsElement>(".ai-usage")?.open).toBe(true);
    expect(host.querySelector("summary")?.textContent).toBe("AI usage");
    expect(control("Numbers").getAttribute("aria-pressed")).toBe("true");
    expect(control("7 days").getAttribute("aria-pressed")).toBe("true");
    expect([...host.querySelectorAll(".ai-usage-metrics article")].map(item => [item.querySelector("span")?.textContent, item.querySelector("strong")?.textContent])).toEqual([
      ["Total", "12.9k"], ["Search", "12.4k"], ["Document generation", "500"], ["AI calls", "3"],
    ]);
    expect(host.querySelector(".ai-usage-metrics strong")?.getAttribute("title")).toBe(new Intl.NumberFormat("en-US").format(12_900));
    expect([...host.querySelectorAll(".ai-usage-breakdown dd")].map(item => item.textContent)).toEqual(["12k", "400", "12.4k", "300", "200", "500"]);
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/stats/ai-usage?period=7d"), expect.objectContaining({ signal: expect.any(AbortSignal) }));
    await act(async () => host.querySelector("summary")?.click());
    expect(host.querySelector<HTMLDetailsElement>(".ai-usage")?.open).toBe(false);
    await act(async () => host.querySelector("summary")?.click());
    expect(host.querySelector<HTMLDetailsElement>(".ai-usage")?.open).toBe(true);
  });

  it("change les quatre périodes sans perdre la vue choisie ni recharger la page", async () => {
    const fetchMock = vi.fn(async input => {
      const period = new URL(String(input)).searchParams.get("period");
      return { ok: true, json: async () => ({ ...recordedUsage, period, granularity: period === "today" ? "hour" : "day", daily: period === "today" ? [{ date: "2026-10-06T14:00:00Z", search_tokens: 950, document_tokens: 300 }] : recordedUsage.daily }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    await act(async () => root.render(<AiUsageCard />)); await flush();
    await act(async () => control("Charts").click());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(host.querySelectorAll(".chart-series path")).toHaveLength(2);
    expect(host.querySelector(".ai-usage-chart")?.textContent).toContain("Search");
    expect(host.querySelector(".ai-usage-chart")?.textContent).toContain("Document generation");
    for (const label of ["Today", "7 days", "30 days", "All"]) {
      await act(async () => control(label).click()); await flush();
      expect(control(label).getAttribute("aria-pressed")).toBe("true");
      expect(control("Charts").getAttribute("aria-pressed")).toBe("true");
      expect(host.querySelector("svg")).not.toBeNull();
      if (label === "Today") {
        expect(host.textContent).toContain("times use UTC");
        expect(host.querySelector("svg")?.textContent).toContain("02:00 PM");
      }
    }
    expect(fetchMock.mock.calls.map(([input]) => new URL(String(input)).searchParams.get("period"))).toEqual(["7d", "today", "7d", "30d", "all"]);
    await act(async () => control("Numbers").click());
    expect(control("All").getAttribute("aria-pressed")).toBe("true");
    expect(host.querySelector("svg")).toBeNull();
    expect(host.querySelector(".ai-usage-metrics")?.textContent).toContain("12.9k");
    expect(window.location.pathname).toBe("/");
  });

  it("affiche l'absence de données dans les deux vues", async () => {
    mockUsage(emptyUsage);
    await act(async () => root.render(<AiUsageCard />)); await flush();
    expect(host.textContent).toContain("No AI usage recorded for this period.");
    await act(async () => control("Charts").click());
    expect(host.textContent).toContain("No AI usage recorded for this period.");
    expect(host.querySelector("svg")).toBeNull();
  });

  it("distingue usages unknown et zéros known et renouvelle les données du dashboard", async () => {
    const unknown = { input_tokens: null, output_tokens: null, total_tokens: null, calls: 1, unknown_calls: 1 };
    const fetchMock = mockUsage({ ...emptyUsage, total: unknown, search: unknown, documents: zeroUsage, daily: [{ date: "2026-10-06", search_tokens: null, document_tokens: null }] });
    await act(async () => root.render(<AiUsageCard />)); await flush();
    expect([...host.querySelectorAll(".ai-usage-metrics strong")].map(item => item.textContent)).toEqual(["Unknown", "Unknown", "0", "1"]);
    expect(host.textContent).toContain("1 call without usage data");
    await act(async () => control("Charts").click());
    expect(host.textContent).toContain("No known usage data for this chart.");
    expect(host.querySelector("svg")).toBeNull();

    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({ ...emptyUsage, total: { ...zeroUsage, calls: 1 }, search: { ...zeroUsage, calls: 1 }, daily: [{ date: "2026-10-06", search_tokens: 0, document_tokens: 0 }] }) });
    const refreshToken = { phases: { search: 1, ignored: 0, validation: 0, send: 0, tracking: 0, rejected: 0 }, followups: [], today: [], upcoming_interviews: [] };
    await act(async () => root.render(<AiUsageCard refreshToken={refreshToken} />)); await flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(host.querySelectorAll(".chart-series circle")).toHaveLength(2);
    expect(host.textContent).not.toContain("sans données de consommation");
    await act(async () => control("Numbers").click());
    expect([...host.querySelectorAll(".ai-usage-metrics strong")].map(item => item.textContent)).toEqual(["0", "0", "0", "1"]);
  });

  it("ne relie pas les points dont la consommation est inconnue", async () => {
    mockUsage({ ...recordedUsage, daily: [
      { date: "2026-10-04", search_tokens: 950, document_tokens: null },
      { date: "2026-10-05", search_tokens: null, document_tokens: 200 },
      { date: "2026-10-06", search_tokens: 12_400, document_tokens: 300 },
    ] });
    await act(async () => root.render(<AiUsageCard />)); await flush();
    await act(async () => control("Charts").click());
    const path = host.querySelector(".chart-series.search path")?.getAttribute("d");
    expect(path?.match(/M/g)).toHaveLength(2);
    expect(path).not.toContain("L");
    expect(host.querySelectorAll(".chart-series.search circle")).toHaveLength(2);
    expect(host.querySelector(".chart-series.search circle title")?.textContent).toContain("950 tokens");
  });

  it("rafraîchit après une activité IA sans perdre période, vue ou état replié", async () => {
    const fetchMock = mockUsage(recordedUsage);
    await act(async () => root.render(<AiUsageCard />)); await flush();
    await act(async () => control("30 days").click()); await flush();
    await act(async () => { control("Charts").click(); host.querySelector("summary")?.click(); });
    await act(async () => root.render(<AiUsageCard activity />)); await flush();
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({
      ...recordedUsage,
      total: { ...recordedUsage.total, input_tokens: 12_800, output_tokens: 700, total_tokens: 13_500, calls: 4 },
      search: { ...recordedUsage.search, input_tokens: 12_500, output_tokens: 500, total_tokens: 13_000, calls: 3 },
      daily: [recordedUsage.daily[0], { date: "2026-10-06", search_tokens: 6800, document_tokens: 300 }],
    }) });
    await act(async () => root.render(<AiUsageCard activity={false} />)); await flush();
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock).toHaveBeenLastCalledWith(expect.stringContaining("period=30d"), expect.any(Object));
    expect(host.querySelector<HTMLDetailsElement>(".ai-usage")?.open).toBe(false);
    expect(control("Charts").getAttribute("aria-pressed")).toBe("true");
    expect(control("30 days").getAttribute("aria-pressed")).toBe("true");
    expect([...host.querySelectorAll(".chart-series.search circle title")].at(-1)?.textContent).toContain(new Intl.NumberFormat("en-US").format(6800));
    await act(async () => { host.querySelector("summary")?.click(); control("Numbers").click(); });
    expect([...host.querySelectorAll(".ai-usage-metrics strong")].map(item => item.textContent)).toEqual(["13.5k", "13k", "500", "4"]);
  });

  it("affiche chargement et erreur puis ignore une réponse devenue obsolète", async () => {
    let finish!: (data: unknown) => void;
    const fetchMock = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
      .mockResolvedValueOnce({ ok: false, statusText: "Service unavailable", json: async () => ({ detail: "Service indisponible" }) })
      .mockResolvedValueOnce({ ok: true, json: async () => emptyUsage });
    vi.stubGlobal("fetch", fetchMock);
    await act(async () => root.render(<AiUsageCard />));
    expect(host.textContent).toContain("Loading usage…");
    await act(async () => control("30 days").click()); await flush();
    expect(host.querySelector(".error")?.textContent).toBe("Usage unavailable : Service indisponible");
    await act(async () => finish({ ok: true, json: async () => recordedUsage })); await flush();
    expect(host.querySelector(".error")?.textContent).toBe("Usage unavailable : Service indisponible");
    await act(async () => control("All").click()); await flush();
    expect(host.querySelector(".error")).toBeNull();
    expect(host.textContent).toContain("No AI usage recorded for this period.");
  });
});

describe("routing SPA", () => {
  function mockApi(activeNoWeb = false, aiConfig?: unknown, latest: unknown = null, searchConfig?: unknown, availableModels?: unknown[], initialProfile?: unknown) {
    const detail = { ...application, company: "Example Research", cv_action: "ADAPT", cv_variant: "D", next_action: "Relire l'analyse, le CV et la lettre de motivation puis valider", events: [] };
    const active = activeNoWeb ? { provider_id: "deepseek_api", auth_mode: "api_key", model: "deepseek-test", effort: "medium", label: "DeepSeek — API" } : null;
    const configuredActive = (aiConfig as { active?: { model?: string; effort?: string } } | undefined)?.active;
    let savedProfile = initialProfile;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/settings/search-profile") && _init?.method === "PUT" && savedProfile) {
        savedProfile = { ...savedProfile as object, ...JSON.parse(String(_init.body)) };
      }
      const data = url.endsWith("/applications") ? [detail]
        : url.endsWith("/stats") ? { phases: { search: 1, ignored: 2, validation: 3, send: 4, tracking: 5, rejected: 6 }, followups: [], today: [], upcoming_interviews: [] }
        : url.includes("/stats/ai-usage?") ? emptyUsage
        : url.endsWith("/ai/config") ? aiConfig ?? { chatgpt: { connected: false, mode: "Abonnement ChatGPT / Codex" }, api_keys: {
          openai: { configured: false, billing_confirmed: false }, anthropic_api: { configured: false, billing_confirmed: false },
          gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: false, billing_confirmed: false },
        }, active, capabilities: { structured_output: activeNoWeb, web_search: false, reasoning_effort: false, streaming: activeNoWeb, tool_calling: activeNoWeb } }
        : url.includes("/ai/models") ? { models: availableModels || [{ model: configuredActive?.model || "deepseek-test", reasoningEfforts: ["medium"] }], selected: configuredActive?.model || "deepseek-test", selectedEffort: configuredActive?.effort || "medium" }
        : url.endsWith("/codex/discovery/latest") ? latest
        : url.endsWith("/search/config") ? { max_offer_age_days: 90, ...((searchConfig ?? { mode: "SEARXNG", provider: null, model: null, effort: "medium", searxng_url: "http://localhost:8080", capabilities: { structured_output: false, web_search: false, reasoning_effort: false, streaming: false } }) as object) }
        : url.endsWith("/search/test") ? { connected: true, status: "SUCCESS", result_count: 1, provider_errors: [] }
        : url.endsWith("/ai/pipeline") ? { overrides: {
          screening: { provider: "default", effort: "medium" }, deep_analysis: { provider: "default", effort: "medium" },
          company_analysis: { provider: "default", effort: "medium" }, application_preparation: { provider: "default", effort: "medium" },
        }, ai_fallback: null }
        : url.endsWith("/settings/search-profile") ? savedProfile ?? (activeNoWeb
          ? { mode: "custom_prompt", custom_search_prompt: "", knowledge_base_available: false, knowledge_base_sources: [], knowledge_base_files: [], knowledge_base_selected_files: [], candidate_profile_available: false }
          : { mode: "knowledge_base", custom_search_prompt: "", knowledge_base_available: true, knowledge_base_sources: ["Profile", "Domaines", "Objectif professionnel"], knowledge_base_files: [{ path: "01_Career/Profile.md", label: "Profile" }, { path: "01_Career/Domaines.md", label: "Domaines" }, { path: "01_Career/Stage M2.md", label: "Objectif professionnel" }, { path: "02_Projects/Projet.md", label: "Projet" }], knowledge_base_selected_files: ["01_Career/Profile.md", "01_Career/Domaines.md", "01_Career/Stage M2.md"], candidate_profile_available: true })
        : url.endsWith("/applications/7") ? detail
        : undefined;
      if (url.includes("/files/")) return { ok: true, status: 200, text: async () => "# Analysis" } as Response;
      if (data === undefined) return { ok: false, status: 404, statusText: "Not Found", json: async () => ({ detail: "Application introuvable" }) } as Response;
      return { ok: true, status: 200, json: async () => data } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("affiche les compteurs dans l'ordre métier", async () => {
    mockApi(); window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    const cards = [...host.querySelectorAll(".cards article")].map(card => [
      card.querySelector("span")?.textContent,
      card.querySelector("strong")?.textContent,
    ]);
    expect(cards).toEqual([
      ["Search", "1"], ["Ignored", "2"], ["Review", "3"],
      ["Submit", "4"], ["Track", "5"], ["Rejected", "6"],
    ]);
    expect(host.querySelector(".search-profile")?.nextElementSibling).toBe(host.querySelector(".ai-usage"));
    expect(host.querySelector(".ai-usage")?.nextElementSibling).toBe(host.querySelector(".cards"));
  });

  describe("récence des new jobs", () => {
    const input = () => host.querySelector('input[aria-label="Maximum age in days"]') as HTMLInputElement;
    const searchButton = () => host.querySelector(".search-launch .primary") as HTMLButtonElement;
    async function changeAge(value: string) {
      await act(async () => {
        input().focus();
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input(), value);
        input().dispatchEvent(new window.InputEvent("input", { bubbles: true }));
      });
    }
    async function setup(initialAge = 90) {
      let savedAge = initialAge;
      const searchedAges: number[] = [];
      const fetchMock = mockApi(false, {
        chatgpt: { connected: true, mode: "Abonnement ChatGPT / Codex" },
        api_keys: {
          openai: { configured: false, billing_confirmed: false }, anthropic_api: { configured: false, billing_confirmed: false },
          gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: false, billing_confirmed: false },
        },
        active: { provider_id: "openai", auth_mode: "chatgpt_oauth", model: "test-model", effort: "medium", label: "ChatGPT — subscription" },
        capabilities: { structured_output: true, web_search: true, reasoning_effort: false, streaming: true },
      });
      const originalFetch = fetchMock.getMockImplementation()!;
      fetchMock.mockImplementation(async (url, init) => {
        if (String(url).endsWith("/search/config")) {
          if (init?.method === "PATCH") savedAge = JSON.parse(String(init.body)).max_offer_age_days;
          const response = await originalFetch(url, init);
          return { ok: true, json: async () => ({ ...await response.json(), max_offer_age_days: savedAge }) } as Response;
        }
        if (String(url).endsWith("/codex/discover")) {
          searchedAges.push(savedAge);
          return { ok: true, json: async () => ({}) } as Response;
        }
        return originalFetch(url, init);
      });
      await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
      return { fetchMock, searchedAges };
    }

    it.each([90, 60])("affiche le seuil backend %s, y compris sa valeur par défaut", async value => {
      await setup(value);
      expect(input().type).toBe("number"); expect(input().value).toBe(String(value));
      expect(input().min).toBe("1"); expect(input().max).toBe("365"); expect(input().step).toBe("1");
      const save = input().closest("label")?.nextElementSibling;
      expect(save?.textContent).toBe("Save");
      expect(save?.closest(".search-launch-settings")?.nextElementSibling).toBe(searchButton());
    });

    it("enregistre sans recherche, restaure après rechargement et recherche avec le seuil sauvegardé", async () => {
      const { fetchMock, searchedAges } = await setup();
      await changeAge("60");
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(false);
      await act(async () => input().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
      expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/search/config"), expect.objectContaining({ method: "PATCH", body: JSON.stringify({ max_offer_age_days: 60 }) }));
      expect(searchedAges).toEqual([]); expect(input().value).toBe("60");
      await act(async () => root.render(<BrowserRouter key="reloaded"><App /></BrowserRouter>)); await flush();
      expect(input().value).toBe("60");
      await act(async () => searchButton().click()); await flush();
      expect(searchedAges).toEqual([60]);
    });

    it.each(["", "0", "366", "30.5"])("refuse %s sans sauvegarder et garde la recherche utilisable", async value => {
      const { fetchMock, searchedAges } = await setup(60);
      await changeAge(value); await act(async () => input().blur());
      expect(host.querySelector(".error")?.textContent).toContain("integer between 1 and 365");
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(false);
      expect(input().value).toBe("60"); expect(searchButton().disabled).toBe(false);
      await act(async () => searchButton().click()); await flush();
      expect(searchedAges).toEqual([60]);
    });

    it("affiche une erreur de sauvegarde et recherche avec la dernière préférence", async () => {
      const { fetchMock, searchedAges } = await setup(60);
      const originalFetch = fetchMock.getMockImplementation()!;
      fetchMock.mockImplementation(async (url, init) => init?.method === "PATCH"
        ? { ok: false, status: 500, json: async () => ({ detail: "Sauvegarde indisponible" }) } as Response : originalFetch(url, init));
      await changeAge("45"); await act(async () => input().blur());
      expect(host.querySelector(".error")?.textContent).toContain("Sauvegarde indisponible");
      expect(input().value).toBe("60"); expect(searchButton().disabled).toBe(false);
      await act(async () => searchButton().click()); await flush();
      expect(searchedAges).toEqual([60]);
    });

    it("attend la sauvegarde au blur avant de déclencher la recherche", async () => {
      const { fetchMock, searchedAges } = await setup();
      const originalFetch = fetchMock.getMockImplementation()!;
      let finishSave!: () => void;
      fetchMock.mockImplementation(async (url, init) => {
        if (init?.method === "PATCH") await new Promise<void>(resolve => { finishSave = resolve; });
        return originalFetch(url, init);
      });
      await changeAge("45"); act(() => input().blur());
      expect(input().disabled).toBe(true); expect(searchButton().disabled).toBe(false);
      act(() => searchButton().click());
      expect(searchedAges).toEqual([]);
      await act(async () => finishSave()); await flush();
      expect(searchedAges).toEqual([45]);
      expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(1);
    });

    it("sauvegarde aussi la saisie lors d'un clic direct of recherche", async () => {
      const { searchedAges } = await setup();
      await changeAge("30"); await act(async () => searchButton().click()); await flush();
      expect(searchedAges).toEqual([30]); expect(input().value).toBe("30");
    });

    it("garde la préférence sauvegardée quand un ancien chargement de configuration termine après le PATCH", async () => {
      const { fetchMock, searchedAges } = await setup();
      await changeAge("60"); await act(async () => input().blur());
      const originalFetch = fetchMock.getMockImplementation()!;
      let finishConfig!: () => void;
      let delayed = false;
      fetchMock.mockImplementation(async (url, init) => {
        const response = await originalFetch(url, init);
        if (String(url).endsWith("/search/config") && !init?.method && !delayed) {
          delayed = true;
          const config = await response.json();
          await new Promise<void>(resolve => { finishConfig = resolve; });
          return { ok: true, json: async () => config } as Response;
        }
        return response;
      });
      const saveConfig = [...host.querySelectorAll(".search-settings + .actions button")].find(button => button.textContent === "Save") as HTMLButtonElement;
      act(() => saveConfig.click()); await flush();
      expect(delayed).toBe(true);
      await changeAge("45"); await act(async () => input().blur());
      expect(input().value).toBe("45");
      await act(async () => finishConfig()); await flush();
      expect(input().value).toBe("45");
      await act(async () => searchButton().click()); await flush();
      expect(searchedAges).toEqual([45]);
    });
  });

  it("affiche le panneau fournisseur sans exposer de clé", async () => {
    mockApi(); window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(host.textContent).toContain("AI provider");
    expect(host.textContent).toContain("Continue with ChatGPT");
    expect(host.textContent).toContain("API key");
    expect(host.querySelector('input[type="password"]')).toBeNull();
    const apiTab = [...host.querySelectorAll(".config-switch button")].find(button => button.textContent === "API key") as HTMLButtonElement;
    await act(async () => apiTab.click());
    expect(host.textContent).toContain("Anthropic / Claude");
    expect(host.textContent).toContain("Google Gemini");
    expect(host.textContent).toContain("DeepSeek");
    expect(host.textContent).toContain("No AI provider configured");
    expect(host.querySelector('input[type="password"]')).not.toBeNull();
    expect(host.querySelector(".provider-row")).toBeNull();
    const accountTab = [...host.querySelectorAll(".config-switch button")].find(button => button.textContent === "Account sign-in") as HTMLButtonElement;
    await act(async () => accountTab.click());
    expect(host.textContent).toContain("Continue with ChatGPT");
    expect(host.querySelector('input[type="password"]')).toBeNull();
    const search = [...host.querySelectorAll("button")].find(button => button.textContent === "Search for new jobs");
    expect(search?.disabled).toBe(true);
  });

  it("affiche SearXNG et les héritages par étape par défaut", async () => {
    mockApi(); window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(host.textContent).toContain("Web search");
    expect((host.querySelector(".search-settings select") as HTMLSelectElement).value).toBe("SEARXNG");
    const sections = [...host.querySelectorAll<HTMLDetailsElement>(".ai-provider .config-section")];
    expect(sections.map(section => section.querySelector("summary")?.textContent)).toEqual([
      "AI provider", "Web search", "Default settings", "Configure each stage",
    ]);
    expect(sections.map(section => section.open)).toEqual([true, true, true, false]);

    sections[0].querySelector("summary")?.click();
    expect(sections.map(section => section.open)).toEqual([false, true, true, false]);
    sections[1].querySelector("summary")?.click();
    expect(sections.map(section => section.open)).toEqual([false, false, true, false]);
    sections[2].querySelector("summary")?.click();
    expect(sections.map(section => section.open)).toEqual([false, false, false, false]);
    sections[0].querySelector("summary")?.click();
    expect(sections.map(section => section.open)).toEqual([true, false, false, false]);
    sections[1].querySelector("summary")?.click();
    sections[2].querySelector("summary")?.click();
    sections[3].querySelector("summary")?.click();
    expect(sections.map(section => section.open)).toEqual([true, true, true, true]);
    const apiTab = [...sections[0].querySelectorAll("button")].find(button => button.textContent === "API key") as HTMLButtonElement;
    await act(async () => apiTab.click());
    expect(sections[0].open).toBe(true);
    expect(sections[0].querySelector('input[type="password"]')).not.toBeNull();
    expect([...sections[3].querySelectorAll("select")].slice(0, 4).every(select => select.value === "default")).toBe(true);
    expect([...sections[3].querySelectorAll("select")].at(-1)?.value).toBe("");
  });

  it("désactive le test SearXNG pendant la requête puis restaure le bouton", async () => {
    const fetchMock = mockApi();
    const originalFetch = fetchMock.getMockImplementation()!;
    let finishTest!: (response: Response) => void;
    const pendingTest = new Promise<Response>(resolve => { finishTest = resolve; });
    fetchMock.mockImplementation(async input =>
      String(input).endsWith("/search/test") ? pendingTest : originalFetch(input));
    window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    const test = [...host.querySelectorAll("button")].find(button => button.textContent === "Test") as HTMLButtonElement;

    act(() => test.click());
    expect(test.textContent).toBe("Testing…");
    expect(test.disabled).toBe(true);
    act(() => test.click());
    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/search/test"))).toHaveLength(1);

    finishTest({ ok: true, status: 200, json: async () => ({ ok: true }) } as Response);
    await flush();
    expect(test.textContent).toBe("Test");
    expect(test.disabled).toBe(false);
    expect(host.textContent).toContain("Connection successful");
  });

  describe("statuts temporaires Web search", () => {
    const capabilities = { structured_output: true, web_search: true, reasoning_effort: true, streaming: true };
    const aiConfig = {
      chatgpt: { connected: true, mode: "Abonnement ChatGPT / Codex" },
      api_keys: {
        openai: { configured: true, billing_confirmed: true }, anthropic_api: { configured: false, billing_confirmed: false },
        gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: false, billing_confirmed: false },
      }, active: null, capabilities,
    };
    const directConfig = {
      mode: "AI_DIRECT", provider: "chatgpt_oauth", model: "web-model", effort: "high",
      searxng_url: "http://localhost:8080", capabilities,
    };
    const section = () => [...host.querySelectorAll(".config-section")].find(item => item.querySelector("summary")?.textContent === "Web search") as HTMLElement;
    const status = () => section().querySelector('[role="status"]') as HTMLElement;
    const button = (label: string) => [...section().querySelectorAll("button")].find(item => item.textContent === label) as HTMLButtonElement;
    const select = (label: string) => [...section().querySelectorAll("label")].find(item => item.firstChild?.textContent === label)?.querySelector("select") as HTMLSelectElement;
    const changeSelect = async (label: string, value: string) => act(async () => {
      const input = select(label); input.value = value; input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    async function setup(mode = "AI_DIRECT", modelError = "") {
      let config = { ...directConfig, mode };
      const fetchMock = mockApi(false, aiConfig, null, config, [
        { model: "web-model", reasoningEfforts: ["low", "medium", "high"], capabilities },
        { model: "other-web-model", reasoningEfforts: ["low", "medium"], capabilities },
      ]);
      const originalFetch = fetchMock.getMockImplementation()!;
      fetchMock.mockImplementation(async (input, init?: RequestInit) => {
        if (modelError && String(input).includes("/ai/models?")) {
          return { ok: false, status: 503, json: async () => ({ detail: modelError }) } as Response;
        }
        if (String(input).endsWith("/search/config")) {
          if (init?.method === "PUT") config = { ...config, ...JSON.parse(String(init.body)) };
          return { ok: true, status: 200, json: async () => config } as Response;
        }
        return originalFetch(input);
      });
      window.history.replaceState(null, "", "/");
      await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
      return fetchMock;
    }

    it.each([
      ["AI_DIRECT", "Test", "Web search available"],
      ["AI_DIRECT", "Save", "Settings saved"],
      ["SEARXNG", "Test", "Connection successful"],
    ])("affiche %s / %s sous les actions pendant trois secondes", async (mode, action, message) => {
      await setup(mode);
      expect(status().textContent).toBe("");
      expect(section().textContent).not.toContain("Web search available");
      expect(section().textContent).not.toContain("Web search directe indisponible");
      vi.useFakeTimers();
      try {
        await act(async () => button(action).click());
        expect(section().querySelectorAll('[role="status"]')).toHaveLength(1);
        expect(section().querySelector(".actions")?.nextElementSibling).toBe(status());
        expect(status().textContent).toBe(message); expect(status().className).toBe("success");
        expect([...section().querySelectorAll("p")].filter(item => item.textContent === message)).toHaveLength(1);
        await act(async () => vi.advanceTimersByTimeAsync(2999));
        expect(status().textContent).toBe(message);
        await act(async () => vi.advanceTimersByTimeAsync(1));
        expect(status().textContent).toBe("");
        expect(section().textContent).not.toContain("Web search available");
      } finally { vi.useRealTimers(); }
    });

    it.each([
      ["SEARXNG", "Search mode", "AI_DIRECT"],
      ["AI_DIRECT", "Provider", "openai"],
      ["AI_DIRECT", "Model", "other-web-model"],
      ["AI_DIRECT", "Effort", "medium"],
      ["SEARXNG", "URL SearXNG", "http://localhost:8081"],
    ])("efface le succès lors d'une modification de %s / %s", async (mode, field, value) => {
      await setup(mode);
      await act(async () => button("Test").click());
      expect(status().className).toBe("success");
      if (field === "URL SearXNG") {
        const input = section().querySelector("input") as HTMLInputElement;
        await act(async () => {
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, value);
          input.dispatchEvent(new window.InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
        });
      } else await changeSelect(field, value);
      expect(status().textContent).toBe("");
      expect(section().textContent).not.toContain("Connection successful");
      expect(section().textContent).not.toContain("Web search available");
      await act(async () => button("Test").click());
      const message = select("Search mode").value === "AI_DIRECT" ? "Web search available" : "Connection successful";
      expect(status().textContent).toBe(message);
      expect([...section().querySelectorAll("p")].filter(item => item.textContent === message)).toHaveLength(1);
    });

    it.each([
      ["AI_DIRECT", "Web search available"],
      ["SEARXNG", "Connection successful"],
    ])("remplace deux succès %s identiques et redémarre le délai", async (mode, message) => {
      await setup(mode); vi.useFakeTimers();
      try {
        await act(async () => button("Test").click());
        expect([...section().querySelectorAll("p")].filter(item => item.textContent === message)).toHaveLength(1);
        await act(async () => vi.advanceTimersByTimeAsync(2000));
        await act(async () => button("Test").click());
        expect([...section().querySelectorAll("p")].filter(item => item.textContent === message)).toHaveLength(1);
        await act(async () => vi.advanceTimersByTimeAsync(1000));
        expect(status().textContent).toBe(message);
        await act(async () => vi.advanceTimersByTimeAsync(1999));
        expect(status().textContent).toBe(message);
        await act(async () => vi.advanceTimersByTimeAsync(1));
        expect(status().textContent).toBe("");
      } finally { vi.useRealTimers(); }
    });

    it("garde l'erreur de chargement automatique des modèles hors du résultat de test", async () => {
      await setup("AI_DIRECT", "Catalogue des modèles indisponible");
      expect(status().textContent).toBe("");
      expect(section().textContent).not.toContain("Web search available");
      expect(section().textContent).not.toContain("Catalogue des modèles indisponible");
      expect(host.querySelector("main > .error")?.textContent).toContain("Catalogue des modèles indisponible");
    });

    it.each(["SEARXNG", "AI_DIRECT"])("remplace immédiatement un succès %s par le test en cours et garde l'erreur jusqu'à une modification", async mode => {
      const fetchMock = await setup(mode);
      const originalFetch = fetchMock.getMockImplementation()!;
      let finishTest!: (response: Response) => void;
      const message = mode === "AI_DIRECT" ? "Web search indisponible" : "SearXNG indisponible";
      vi.useFakeTimers();
      try {
        await act(async () => button("Test").click());
        fetchMock.mockImplementation(async (input, init?: RequestInit) => String(input).endsWith("/search/test")
          ? new Promise<Response>(resolve => { finishTest = resolve; }) : originalFetch(input, init));
        act(() => button("Test").click());
        expect(status().textContent).toBe("Testing…"); expect(status().className).toBe("muted");
        await act(async () => vi.advanceTimersByTimeAsync(4000));
        expect(status().textContent).toBe("Testing…");
        await act(async () => finishTest({ ok: false, status: 503, json: async () => ({ detail: message }) } as Response));
        expect(status().textContent).toBe(message); expect(status().className).toBe("failure-text");
        expect([...section().querySelectorAll("p")].filter(item => item.textContent === message)).toHaveLength(1);
        expect(section().textContent).not.toContain("Web search available");
        await act(async () => vi.advanceTimersByTimeAsync(4000));
        expect(status().textContent).toBe(message);
        if (mode === "AI_DIRECT") await changeSelect("Effort", "medium");
        else {
          const input = section().querySelector("input") as HTMLInputElement;
          await act(async () => {
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "http://localhost:8081");
            input.dispatchEvent(new window.InputEvent("input", { bubbles: true }));
          });
        }
        expect(status().textContent).toBe("");
      } finally { vi.useRealTimers(); }
    });

    it.each([
      ["SEARXNG", "Search mode", "AI_DIRECT"],
      ["AI_DIRECT", "Provider", "openai"],
      ["AI_DIRECT", "Model", "other-web-model"],
      ["AI_DIRECT", "Effort", "medium"],
    ])("ignore un ancien test après modification de %s / %s", async (mode, field, value) => {
      const fetchMock = await setup(mode);
      const originalFetch = fetchMock.getMockImplementation()!;
      let finishTest!: (response: Response) => void;
      fetchMock.mockImplementation(async (input, init?: RequestInit) => String(input).endsWith("/search/test")
        ? new Promise<Response>(resolve => { finishTest = resolve; }) : originalFetch(input, init));
      act(() => button("Test").click());
      expect(status().textContent).toBe("Testing…");
      await changeSelect(field, value);
      expect(status().textContent).toBe("");
      await act(async () => finishTest({ ok: true, json: async () => ({ connected: true }) } as Response));
      expect(status().textContent).toBe("");
    });

    it("restaure le modèle après un chargement fournisseur interrompu par le mode SearXNG", async () => {
      const fetchMock = await setup();
      const originalFetch = fetchMock.getMockImplementation()!;
      let finishModels!: (response: Response) => void; let deferModels = true;
      fetchMock.mockImplementation(async (input, init?: RequestInit) => {
        if (String(input).includes("/ai/models?") && String(input).includes("auth_mode=api_key") && deferModels) {
          deferModels = false;
          return new Promise<Response>(resolve => { finishModels = resolve; });
        }
        return originalFetch(input, init);
      });
      await changeSelect("Provider", "openai");
      await changeSelect("Search mode", "SEARXNG");
      await act(async () => finishModels({ ok: true, json: async () => ({ models: [{ model: "web-model", capabilities }] }) } as Response));
      await changeSelect("Search mode", "AI_DIRECT");
      expect(select("Provider").value).toBe("openai"); expect(select("Model").value).toBe("web-model");
      expect(button("Test").disabled).toBe(false); expect(status().textContent).toBe("");
    });

    it("ignore l'ancien test quand l'enregistrement suivant termine avant lui", async () => {
      const fetchMock = await setup("SEARXNG");
      const originalFetch = fetchMock.getMockImplementation()!;
      let finishTest!: (response: Response) => void; let finishSave!: (response: Response) => void;
      fetchMock.mockImplementation(async (input, init?: RequestInit) => {
        if (String(input).endsWith("/search/test")) return new Promise<Response>(resolve => { finishTest = resolve; });
        if (String(input).endsWith("/search/config") && init?.method === "PUT") return new Promise<Response>(resolve => { finishSave = resolve; });
        return originalFetch(input, init);
      });
      vi.useFakeTimers();
      try {
        act(() => button("Test").click());
        act(() => button("Save").click());
        expect(status().textContent).toBe("Saving…");
        await act(async () => finishSave({ ok: true, json: async () => ({}) } as Response));
        expect(status().textContent).toBe("Settings saved");
        await act(async () => vi.advanceTimersByTimeAsync(2000));
        await act(async () => finishTest({ ok: false, status: 503, json: async () => ({ detail: "Ancien test en échec" }) } as Response));
        expect(status().textContent).toBe("Settings saved");
        await act(async () => vi.advanceTimersByTimeAsync(1000));
        expect(status().textContent).toBe("");
      } finally { vi.useRealTimers(); }
    });
  });

  it("restaure le test de clé API après une erreur sans charger le test SearXNG", async () => {
    const fetchMock = mockApi();
    const originalFetch = fetchMock.getMockImplementation()!;
    let finishTest!: (response: Response) => void;
    const pendingTest = new Promise<Response>(resolve => { finishTest = resolve; });
    fetchMock.mockImplementation(async input =>
      String(input).endsWith("/ai/api-keys/openai/test") ? pendingTest : originalFetch(input));
    window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    const apiTab = [...host.querySelectorAll(".config-switch button")].find(button => button.textContent === "API key") as HTMLButtonElement;
    await act(async () => apiTab.click());
    const key = host.querySelector('input[type="password"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(key, "sk-invalid-key-123456789");
      key.dispatchEvent(new window.InputEvent("input", { bubbles: true, inputType: "insertText", data: "sk-invalid-key-123456789" }));
    });
    const tests = [...host.querySelectorAll("button")].filter(button => button.textContent === "Test") as HTMLButtonElement[];
    const apiTest = tests[0]; const searchTest = tests[1];

    act(() => apiTest.click());
    expect(apiTest.textContent).toBe("Testing…");
    expect(apiTest.disabled).toBe(true);
    expect(searchTest.textContent).toBe("Test");
    expect(searchTest.disabled).toBe(false);

    finishTest({ ok: false, status: 401, statusText: "Unauthorized", json: async () => ({ detail: "API key invalide" }) } as Response);
    await flush();
    expect(apiTest.textContent).toBe("Test");
    expect(apiTest.disabled).toBe(false);
    expect(host.textContent).toContain("API key invalide");
  });

  it("sépare le panneau affiché de la configuration active", async () => {
    const fetchMock = mockApi(false, {
      chatgpt: { connected: true, account: "user@example.com", mode: "Abonnement ChatGPT / Codex" },
      api_keys: {
        openai: { configured: true, last_four: "abcd", billing_confirmed: true }, anthropic_api: { configured: false, billing_confirmed: false },
        gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: false, billing_confirmed: false },
      },
      active: { provider_id: "openai", auth_mode: "chatgpt_oauth", model: "test-model", effort: "medium", label: "ChatGPT — subscription" },
      capabilities: { structured_output: true, web_search: true, reasoning_effort: true, streaming: true },
    });
    window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(host.textContent).toContain("user@example.com");
    expect(host.textContent).toContain("Active provider : ChatGPT — subscription");
    expect(host.textContent).toContain("Structured Output");
    const initialRequests = fetchMock.mock.calls.length;
    const apiTab = [...host.querySelectorAll(".config-switch button")].find(button => button.textContent === "API key") as HTMLButtonElement;
    await act(async () => apiTab.click());
    expect(fetchMock).toHaveBeenCalledTimes(initialRequests);
    expect(host.querySelector(".provider-row")).toBeNull();
    expect(host.textContent).toContain("Key configured : ••••abcd");
    expect(host.textContent).toContain("Active provider : ChatGPT — subscription");
    const defaultSettings = host.querySelectorAll('.codex-settings:not(.search-settings) select');
    expect((defaultSettings[0] as HTMLSelectElement).value).toBe("chatgpt_oauth");
    expect((defaultSettings[1] as HTMLSelectElement).value).toBe("test-model");
    const key = host.querySelector('input[type="password"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(key, "sk-temporary-not-saved");
      key.dispatchEvent(new window.InputEvent("input", { bubbles: true, inputType: "insertText", data: "sk-temporary-not-saved" }));
    });
    const accountTab = [...host.querySelectorAll(".config-switch button")].find(button => button.textContent === "Account sign-in") as HTMLButtonElement;
    await act(async () => accountTab.click());
    expect(host.textContent).toContain("user@example.com");
    await act(async () => apiTab.click());
    expect((host.querySelector('input[type="password"]') as HTMLInputElement).value).toBe("");
    expect(fetchMock).toHaveBeenCalledTimes(initialRequests);
  });

  it("ouvre le panneau API lorsqu'une clé existe sans compte connecté", async () => {
    mockApi(false, {
      chatgpt: { connected: false, mode: "Abonnement ChatGPT / Codex" },
      api_keys: {
        openai: { configured: true, last_four: "abcd", billing_confirmed: true }, anthropic_api: { configured: false, billing_confirmed: false },
        gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: false, billing_confirmed: false },
      }, active: null, capabilities: { structured_output: false, web_search: false, reasoning_effort: false, streaming: false },
    });
    window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(host.querySelector('input[type="password"]')).not.toBeNull();
    expect(host.querySelector(".provider-row")).toBeNull();
  });

  it("sélectionne, filtre et conserve les sources KB avant recherche", async () => {
    const files = [
      { path: "01_Career/Profile.md", label: "Profile" },
      { path: "01_Career/Stage M2.md", label: "Objectif professionnel" },
      { path: "02_Projects/Projet.md", label: "Projet" },
    ];
    const fetchMock = mockApi(true, undefined, null, undefined, undefined, {
      mode: "knowledge_base", custom_search_prompt: "", knowledge_base_available: true,
      knowledge_base_sources: ["Profile"], knowledge_base_files: files,
      knowledge_base_selected_files: [files[0].path], candidate_profile_available: true,
    });
    window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    const search = () => [...host.querySelectorAll("button")].find(button => button.textContent === "Search for new jobs")!;
    const checkboxes = () => [...host.querySelectorAll<HTMLInputElement>('.knowledge-base-files input[type="checkbox"]')];
    expect(checkboxes().map(input => input.checked)).toEqual([true, false, false]);
    expect(host.textContent).toContain("Objectif professionnel");
    await act(async () => checkboxes()[2].click());
    expect(search().disabled).toBe(true);
    expect(host.textContent).toContain("Save the search profile");
    const save = () => [...host.querySelectorAll<HTMLButtonElement>(".search-profile button")].find(button => button.textContent === "Save")!;
    await act(async () => save().click()); await flush();
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/settings/search-profile"), expect.objectContaining({
      method: "PUT", body: JSON.stringify({ mode: "knowledge_base", custom_search_prompt: "", knowledge_base_selected_files: [files[0].path, files[2].path] }),
    }));
    expect(search().disabled).toBe(false);
    await act(async () => root.render(<BrowserRouter key="reload"><App /></BrowserRouter>)); await flush();
    expect(checkboxes().map(input => input.checked)).toEqual([true, false, true]);
    const filter = host.querySelector<HTMLInputElement>('.search-profile input[type="search"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(filter, "02_Projects");
      filter.dispatchEvent(new window.Event("input", { bubbles: true }));
    });
    expect(checkboxes()).toHaveLength(1);
    expect(checkboxes()[0].checked).toBe(true);
    await act(async () => checkboxes()[0].click());
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(filter, "");
      filter.dispatchEvent(new window.Event("input", { bubbles: true }));
    });
    await act(async () => checkboxes()[0].click());
    expect(search().disabled).toBe(true);
    expect(host.textContent).toContain("Select at least one Knowledge Base source.");
    await act(async () => save().click()); await flush();
    expect(search().disabled).toBe(true);
    await act(async () => root.render(<BrowserRouter key="empty-reload"><App /></BrowserRouter>)); await flush();
    expect(checkboxes().every(input => !input.checked)).toBe(true);
  });

  it("organise les sources, replie les dossiers et sélectionne un sous-arbre", async () => {
    const files = [
      { path: "01_Career/Profile.md", label: "Profile" },
      { path: "02_Projects/Search Prototype/Overview.md", label: "Overview" },
      { path: "02_Projects/Search Prototype/Roadmap.md", label: "Roadmap" },
      { path: "02_Projects/Other/RAG.md", label: "RAG" },
    ];
    mockApi(true, undefined, null, undefined, undefined, {
      mode: "knowledge_base", custom_search_prompt: "", knowledge_base_root: "C:/kb",
      knowledge_base_available: true, knowledge_base_sources: ["Profile"], knowledge_base_files: files,
      knowledge_base_selected_files: [files[0].path], candidate_profile_available: true,
    });
    window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    const tree = host.querySelector(".knowledge-base-files")!;
    expect(tree.querySelectorAll(".knowledge-base-folder")).toHaveLength(4);
    expect(tree.querySelector("small")).toBeNull();
    const folder = [...tree.querySelectorAll<HTMLDetailsElement>("details")].find(item => item.querySelector("summary")?.firstChild?.textContent === "Search Prototype")!;
    expect(folder.open).toBe(true);
    await act(async () => { folder.open = false; folder.dispatchEvent(new Event("toggle")); });
    expect(folder.open).toBe(false);
    await act(async () => folder.querySelector("button")!.click());
    expect([...folder.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].every(input => input.checked)).toBe(true);
    expect(host.textContent).toContain("Edit sources (3 selected)");
    const filter = host.querySelector<HTMLInputElement>('input[type="search"]')!;
    const setFilter = async (value: string) => act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(filter, value);
      filter.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await setFilter("Search Prototype");
    expect(tree.querySelectorAll('input[type="checkbox"]')).toHaveLength(2);
    expect(folder.open).toBe(true);
    expect(tree.textContent).toContain("02_Projects");
    expect(tree.textContent).not.toContain("01_Career");
    await setFilter("RAG");
    expect(tree.querySelectorAll('input[type="checkbox"]')).toHaveLength(1);
    expect(tree.textContent).toContain("Other");
    await setFilter("");
    const restored = [...tree.querySelectorAll<HTMLDetailsElement>("details")].find(item => item.querySelector("summary")?.firstChild?.textContent === "Search Prototype")!;
    await act(async () => restored.querySelector("button")!.click());
    expect(host.textContent).toContain("Edit sources (1 selected)");
  });

  it("valide une racine KB, affiche les erreurs et recharge les sources retenues", async () => {
    const initialProfile = {
      mode: "knowledge_base", custom_search_prompt: "", knowledge_base_root: "C:/kb-a",
      knowledge_base_available: true, knowledge_base_sources: ["Profile", "Projet"],
      knowledge_base_files: [{ path: "Profile.md", label: "Profile" }, { path: "Projet.md", label: "Projet" }],
      knowledge_base_selected_files: ["Profile.md", "Projet.md"], candidate_profile_available: true,
    };
    const fetchMock = mockApi(true, undefined, null, undefined, undefined, initialProfile);
    const original = fetchMock.getMockImplementation()!;
    let current = initialProfile;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/settings/search-profile")) {
        if (init?.method === "PUT") {
          const payload = JSON.parse(String(init.body));
          if (payload.knowledge_base_root === "C:/missing") return { ok: false, status: 422, json: async () => ({ detail: "Knowledge Base invalide : dossier inexistant" }) } as Response;
          current = { ...current, knowledge_base_root: payload.knowledge_base_root, knowledge_base_files: [{ path: "Profile.md", label: "Profile" }], knowledge_base_selected_files: ["Profile.md"] };
        }
        return { ok: true, json: async () => current } as Response;
      }
      return original(input, init);
    });
    window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    const path = host.querySelector<HTMLInputElement>(".knowledge-base-root input")!;
    expect(path.value).toBe("C:/kb-a");
    const change = async (value: string) => {
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(path, value);
        path.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await act(async () => host.querySelector<HTMLButtonElement>(".knowledge-base-root button")!.click()); await flush();
    };
    await change("C:/missing");
    expect(host.textContent).toContain("Knowledge Base invalide : dossier inexistant");
    expect(host.querySelectorAll('.knowledge-base-files input[type="checkbox"]')).toHaveLength(2);
    await change("C:/kb-b");
    expect(host.textContent).toContain("Valid Knowledge Base");
    expect(host.textContent).toContain("Edit sources (1 selected)");
    expect(host.querySelectorAll('.knowledge-base-files input[type="checkbox"]')).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/settings/search-profile"), expect.objectContaining({ method: "PUT", body: JSON.stringify({ mode: "knowledge_base", custom_search_prompt: "", knowledge_base_root: "C:/kb-b" }) }));
    await act(async () => root.render(<BrowserRouter key="root-reload"><App /></BrowserRouter>)); await flush();
    expect(host.querySelector<HTMLInputElement>(".knowledge-base-root input")!.value).toBe("C:/kb-b");
  });

  it("masque la KB en mode personnalisé et recharge ses sources au retour", async () => {
    const profile = {
      mode: "knowledge_base", custom_search_prompt: "Stage ML à Lyon", knowledge_base_root: "C:/kb",
      knowledge_base_available: true, knowledge_base_sources: ["Profile"],
      knowledge_base_files: [{ path: "01_Career/Profile.md", label: "Profile" }],
      knowledge_base_selected_files: ["01_Career/Profile.md"], candidate_profile_available: true,
    };
    const fetchMock = mockApi(false, undefined, null, undefined, undefined, profile);
    const originalFetch = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/settings/search-profile?mode=knowledge_base")) {
        return { ok: true, json: async () => profile } as Response;
      }
      const response = await originalFetch(input, init);
      if (String(input).endsWith("/settings/search-profile") && init?.method === "PUT") {
        const saved = await response.json();
        return { ok: true, json: async () => ({ ...saved, knowledge_base_available: false, knowledge_base_files: [], candidate_profile_available: null }) } as Response;
      }
      return response;
    });
    window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    const panel = () => host.querySelector(".search-profile")!;
    const select = async (mode: string) => { await act(async () => (host.querySelector(`input[value="${mode}"]`) as HTMLInputElement).click()); await flush(); };
    expect(panel().textContent).toContain("Valid Knowledge Base");
    await select("custom_prompt");
    expect(panel().querySelector(".knowledge-base-root")).toBeNull();
    expect(panel().querySelector(".knowledge-base-files")).toBeNull();
    expect(panel().textContent).not.toMatch(/Valid Knowledge Base|Sources used|C:\/kb|Edit/);
    expect((panel().querySelector("textarea") as HTMLTextAreaElement).value).toBe(profile.custom_search_prompt);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("?mode=knowledge_base"))).toBe(false);
    const save = [...panel().querySelectorAll("button")].find(button => button.textContent === "Save")!;
    await act(async () => save.click()); await flush();
    await select("knowledge_base");
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("?mode=knowledge_base"), expect.anything());
    expect(panel().textContent).toContain("Valid Knowledge Base");
    expect(panel().textContent).toContain("Sources used : Profile");
    expect((panel().querySelector(".knowledge-base-root input") as HTMLInputElement).value).toBe(profile.knowledge_base_root);
    expect((panel().querySelector('.knowledge-base-files input[type="checkbox"]') as HTMLInputElement).checked).toBe(true);
  });

  it("active la recherche sans KB après sauvegarde du prompt", async () => {
    let profile = { mode: "custom_prompt", custom_search_prompt: "", knowledge_base_available: false, knowledge_base_sources: [], knowledge_base_files: [], knowledge_base_selected_files: [], candidate_profile_available: false };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      let data: unknown = url.endsWith("/applications") ? []
        : url.endsWith("/stats") ? { phases: { search: 0, ignored: 0, validation: 0, send: 0, tracking: 0, rejected: 0 }, followups: [], today: [], upcoming_interviews: [] }
        : url.endsWith("/ai/config") ? { chatgpt: { connected: true, mode: "Abonnement ChatGPT / Codex" }, api_keys: {
          openai: { configured: false, billing_confirmed: false }, anthropic_api: { configured: false, billing_confirmed: false },
          gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: false, billing_confirmed: false },
        }, active: { provider_id: "openai", auth_mode: "chatgpt_oauth", model: "test-model", effort: "medium", label: "ChatGPT — subscription" }, capabilities: { structured_output: true, web_search: true, reasoning_effort: false, streaming: true } }
        : url.endsWith("/ai/models") ? { models: [{ model: "test-model", reasoningEfforts: ["medium"] }], selected: "test-model", selectedEffort: "medium" }
        : url.endsWith("/codex/discovery/latest") ? null
        : url.endsWith("/search/config") ? { mode: "SEARXNG", provider: null, model: null, effort: "medium", searxng_url: "http://localhost:8080", capabilities: { structured_output: false, web_search: false, reasoning_effort: false, streaming: false } }
        : url.endsWith("/ai/pipeline") ? { overrides: {
          screening: { provider: "default", effort: "medium" }, deep_analysis: { provider: "default", effort: "medium" },
          company_analysis: { provider: "default", effort: "medium" }, application_preparation: { provider: "default", effort: "medium" },
        }, ai_fallback: null }
        : url.endsWith("/settings/search-profile") ? profile : undefined;
      if (url.endsWith("/settings/search-profile") && init?.method === "PUT") {
        const body = JSON.parse(String(init.body)); profile = { ...profile, ...body }; data = profile;
      }
      return data === undefined
        ? { ok: false, status: 404, json: async () => ({ detail: "Not Found" }) } as Response
        : { ok: true, status: 200, json: async () => data } as Response;
    });
    vi.stubGlobal("fetch", fetchMock); window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(host.textContent).not.toContain("None Knowledge Base détectée");
    const search = [...host.querySelectorAll("button")].find(button => button.textContent === "Search for new jobs") as HTMLButtonElement;
    expect(search.disabled).toBe(true); expect(host.textContent).toContain("Define your search profile first.");
    const textarea = host.querySelector("textarea") as HTMLTextAreaElement;
    expect(textarea.value).toBe(""); expect(textarea.placeholder).toContain("roles, location, availability");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(textarea, "Stage ML ou NLP à Lyon pendant 6 month");
      textarea.dispatchEvent(new window.InputEvent("input", { bubbles: true, inputType: "insertText", data: "Stage ML ou NLP à Lyon pendant 6 month" }));
    });
    const save = [...host.querySelectorAll(".search-profile button")].find(button => button.textContent === "Save") as HTMLButtonElement;
    await act(async () => save.click()); await flush();
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/settings/search-profile"), expect.objectContaining({ method: "PUT", body: expect.stringContaining("Stage ML") }));
    const enabledSearch = [...host.querySelectorAll("button")].find(button => button.textContent === "Search for new jobs") as HTMLButtonElement;
    expect(enabledSearch.disabled).toBe(false);
  });

  it("sépare la recherche Web du provider IA", async () => {
    mockApi(true); window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(host.textContent).toContain("Web search : SearXNG");
    expect(host.textContent).toContain("Define your search profile first.");
  });

  it("bloque AI_DIRECT lorsque le modèle ne fournit pas de vraie recherche Web", async () => {
    mockApi(true, undefined, null, {
      mode: "AI_DIRECT", provider: "deepseek_api", model: "deepseek-test", effort: "medium",
      searxng_url: "http://localhost:8080",
      capabilities: { structured_output: true, web_search: false, reasoning_effort: false, streaming: true },
    });
    window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(host.textContent).toContain("Direct web search is unavailable with this provider/model.");
    const test = [...host.querySelectorAll(".config-section button")].find(button => button.textContent === "Test") as HTMLButtonElement;
    expect(test.disabled).toBe(true);
    const search = [...host.querySelectorAll("button")].find(button => button.textContent === "Search for new jobs") as HTMLButtonElement;
    expect(search.disabled).toBe(true);
  });

  it("affiche, restaure et transmet les efforts supportés de la recherche Web", async () => {
    const capabilities = { structured_output: true, web_search: true, reasoning_effort: true, streaming: true };
    const fetchMock = mockApi(false, {
      chatgpt: { connected: true, mode: "Abonnement ChatGPT / Codex" },
      api_keys: {
        openai: { configured: false, billing_confirmed: false }, anthropic_api: { configured: false, billing_confirmed: false },
        gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: false, billing_confirmed: false },
      }, active: null, capabilities: { structured_output: false, web_search: false, reasoning_effort: false, streaming: false },
    }, null, {
      mode: "AI_DIRECT", provider: "chatgpt_oauth", model: "gpt-5.6-sol", effort: "high",
      searxng_url: "http://localhost:8080", capabilities,
    }, [
      { model: "gpt-5.6-sol", displayName: "GPT-5.6-Sol", reasoningEfforts: ["low", "medium", "high", "xhigh", "max"], capabilities },
      { model: "test-model", displayName: "Test Model", reasoningEfforts: ["low", "medium"], capabilities },
    ]);
    window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();

    const settings = host.querySelectorAll<HTMLSelectElement>(".search-settings select");
    expect([...settings].map(item => item.previousSibling?.textContent)).toEqual(["Search mode", "Provider", "Model", "Effort"]);
    expect(settings[3].value).toBe("high");
    expect([...settings[3].options].map(item => item.value)).toEqual(["low", "medium", "high", "xhigh", "max"]);
    expect(host.textContent).toContain("Web search : ChatGPT — GPT-5.6 Sol");
    expect(host.textContent).not.toContain("chatgpt_oauth");

    await act(async () => {
      settings[3].value = "max"; settings[3].dispatchEvent(new Event("change", { bubbles: true }));
    });
    const test = [...host.querySelectorAll(".config-section button")].find(button => button.textContent === "Test") as HTMLButtonElement;
    await act(async () => test.click()); await flush();
    const testCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/search/test")) as [unknown, RequestInit] | undefined;
    expect(JSON.parse(String(testCall?.[1]?.body)).effort).toBe("max");

    await act(async () => {
      settings[2].value = "test-model"; settings[2].dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(settings[3].value).toBe("medium");
    expect([...settings[3].options].map(item => item.value)).toEqual(["low", "medium"]);
  });

  describe("disponibilité de la recherche Web", () => {
    const available = { structured_output: true, web_search: true, reasoning_effort: true, streaming: true };
    const unavailable = { structured_output: false, web_search: false, reasoning_effort: false, streaming: false };
    const models = [
      { model: "gpt-5.6-sol", displayName: "GPT-5.6-Sol", reasoningEfforts: ["low", "medium", "high"], capabilities: available },
      { model: "no-web-model", displayName: "Sans recherche Web", reasoningEfforts: ["medium"], capabilities: unavailable },
    ];
    const section = () => [...host.querySelectorAll<HTMLDetailsElement>(".config-section")].find(item => item.querySelector("summary")?.textContent === "Web search")!;
    const settings = () => section().querySelectorAll<HTMLSelectElement>(".search-settings select");
    const button = (label: string) => [...section().querySelectorAll("button")].find(item => item.textContent === label)!;
    const change = async (index: number, value: string) => {
      await act(async () => {
        const select = settings()[index]; select.value = value; select.dispatchEvent(new Event("change", { bubbles: true }));
      });
      await flush();
    };
    const expectAvailable = (enabled: boolean) => {
      expect(section().textContent).not.toContain("Direct web search is unavailable with this provider/model.");
      expect(section().textContent).not.toContain("Web search available");
      expect(button("Test").disabled).toBe(!enabled);
      expect(button("Save").disabled).toBe(!enabled);
    };
    async function renderSearch(mode: "AI_DIRECT" | "SEARXNG" = "AI_DIRECT") {
      let config = {
        mode, provider: "chatgpt_oauth", model: "gpt-5.6-sol", effort: "medium",
        searxng_url: "http://localhost:8080", capabilities: mode === "AI_DIRECT" ? available : unavailable,
      };
      const fetchMock = mockApi(false, {
        chatgpt: { connected: true, mode: "Abonnement ChatGPT / Codex" },
        api_keys: {
          openai: { configured: false, billing_confirmed: false }, anthropic_api: { configured: false, billing_confirmed: false },
          gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: true, billing_confirmed: true },
        }, active: null, capabilities: unavailable,
      }, null, config, models);
      const originalFetch = fetchMock.getMockImplementation()!;
      fetchMock.mockImplementation(async (input, init) => {
        const url = String(input);
        if (url.endsWith("/search/config")) {
          if (init?.method === "PUT") {
            const body = JSON.parse(String(init.body));
            config = { ...config, ...body, capabilities: body.mode === "AI_DIRECT" ? available : unavailable };
          }
          return { ok: true, status: 200, json: async () => config } as Response;
        }
        if (url.includes("/ai/models") && new URL(url).searchParams.get("provider_id") === "deepseek_api") {
          return { ok: true, status: 200, json: async () => ({ models: [{ ...models[0], capabilities: unavailable }] }) } as Response;
        }
        return originalFetch(input, init);
      });
      window.history.replaceState(null, "", "/");
      await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
      return fetchMock;
    }

    it.each([false, true])("restaure AI_DIRECT après SearXNG sans changer le modèle (enregistrement : %s)", async save => {
      const fetchMock = await renderSearch();
      expectAvailable(true);
      expect(settings()[2].value).toBe("gpt-5.6-sol");
      expect(settings()[3].value).toBe("medium");
      await change(0, "SEARXNG");
      expect(section().querySelector("input")?.value).toBe("http://localhost:8080");
      expect(section().textContent).not.toContain("Web search available");
      if (save) {
        await act(async () => button("Save").click()); await flush();
        expect(fetchMock.mock.calls.some(([input, init]) => String(input).endsWith("/search/config") && init?.method === "PUT")).toBe(true);
      }
      await change(0, "AI_DIRECT");
      expectAvailable(true);
      expect([...settings()].map(item => item.value)).toEqual(["AI_DIRECT", "chatgpt_oauth", "gpt-5.6-sol", "medium"]);
      await act(async () => button("Test").click()); await flush();
      const testCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/search/test"));
      expect(testCall?.[1]?.method).toBe("POST");
      expect(JSON.parse(String(testCall?.[1]?.body))).toMatchObject({ mode: "AI_DIRECT", provider: "chatgpt_oauth", model: "gpt-5.6-sol", effort: "medium" });
    });

    it("restaure AI_DIRECT depuis SearXNG persisté avec capacités obsolètes", async () => {
      await renderSearch("SEARXNG");
      await change(0, "AI_DIRECT");
      expectAvailable(true);
      expect([...settings()].map(item => item.value)).toEqual(["AI_DIRECT", "chatgpt_oauth", "gpt-5.6-sol", "medium"]);
      expect(settings()[2].selectedOptions[0].textContent).toBe("GPT-5.6-Sol");
    });

    it("recalcule la disponibilité pour modèles compatible puis incompatible puis compatible", async () => {
      await renderSearch();
      expectAvailable(true);
      await change(2, "no-web-model");
      expectAvailable(false);
      expect(settings()).toHaveLength(3);
      await change(2, "gpt-5.6-sol");
      expectAvailable(true);
      expect(settings()[3].value).toBe("medium");
    });

    it("utilise les capacités du fournisseur courant même avec un identifiant de modèle commun", async () => {
      await renderSearch();
      expectAvailable(true);
      await change(1, "deepseek_api");
      expect(settings()[1].value).toBe("deepseek_api");
      expect(settings()[2].value).toBe("gpt-5.6-sol");
      expectAvailable(false);
      await change(0, "SEARXNG");
      await change(0, "AI_DIRECT");
      expectAvailable(false);
      await change(1, "chatgpt_oauth");
      expectAvailable(true);
      expect(settings()[2].value).toBe("gpt-5.6-sol");
      expect(settings()[3].value).toBe("medium");
    });
  });

  describe("catalogues de modèles par fournisseur et étape", () => {
    const capabilities = { structured_output: true, web_search: true, reasoning_effort: true, streaming: true };
    const row = () => host.querySelector(".pipeline-row")!;
    const provider = () => row().querySelector("select")!;
    const model = () => row().querySelectorAll("select")[1];
    const changeProvider = async (value: string) => act(async () => {
      provider().value = value; provider().dispatchEvent(new Event("change", { bubbles: true }));
    });
    async function setup() {
      let pipeline = { overrides: {
        screening: { provider: "default", effort: "medium" }, deep_analysis: { provider: "default", effort: "medium" },
        company_analysis: { provider: "default", effort: "medium" }, application_preparation: { provider: "default", effort: "medium" },
      }, ai_fallback: null };
      const fetchMock = mockApi(false, {
        chatgpt: { connected: true, mode: "Abonnement ChatGPT / Codex" }, api_keys: {
          openai: { configured: true, billing_confirmed: true }, anthropic_api: { configured: false, billing_confirmed: false },
          gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: false, billing_confirmed: false },
        }, active: null, capabilities,
      });
      const originalFetch = fetchMock.getMockImplementation()!;
      fetchMock.mockImplementation(async (input, init) => {
        const url = String(input);
        if (url.endsWith("/ai/pipeline")) {
          if (init?.method === "PUT") pipeline = JSON.parse(String(init.body));
          return { ok: true, json: async () => pipeline } as Response;
        }
        if (url.includes("/ai/models?")) {
          const prefix = new URL(url).searchParams.get("auth_mode") === "chatgpt_oauth" ? "account" : "api";
          return { ok: true, json: async () => ({ models: [
            { model: `${prefix}-model`, reasoningEfforts: ["medium", "high"], capabilities },
          ] }) } as Response;
        }
        return originalFetch(input, init);
      });
      await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
      return fetchMock;
    }

    it("affiche le catalogue du fournisseur courant après A, B puis A", async () => {
      const fetchMock = await setup();
      await changeProvider("chatgpt_oauth"); expect(model().value).toBe("account-model");
      await changeProvider("openai"); expect(model().value).toBe("api-model");
      expect([...model().options].map(option => option.value)).toEqual(["api-model"]);
      await changeProvider("chatgpt_oauth"); expect(model().value).toBe("account-model");
      expect([...model().options].map(option => option.value)).toEqual(["account-model"]);
      expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/ai/models?"))).toHaveLength(2);
      const saved = fetchMock.mock.calls.filter(([input, init]) => String(input).endsWith("/ai/pipeline") && init?.method === "PUT");
      expect(saved.map(([, init]) => JSON.parse(String(init?.body)).overrides.screening.model)).toEqual(["account-model", "api-model", "account-model"]);
    });

    it("ignore le chargement A terminé après la sélection B", async () => {
      const fetchMock = await setup(); const originalFetch = fetchMock.getMockImplementation()!;
      let finish!: (response: Response) => void;
      fetchMock.mockImplementation(async (input, init) => String(input).includes("/ai/models?") && String(input).includes("chatgpt_oauth")
        ? new Promise<Response>(resolve => { finish = resolve; }) : originalFetch(input, init));
      await changeProvider("chatgpt_oauth"); await changeProvider("openai");
      expect(provider().value).toBe("openai"); expect(model().value).toBe("api-model");
      await act(async () => finish({ ok: true, json: async () => ({ models: [{ model: "account-old", capabilities }] }) } as Response));
      expect(provider().value).toBe("openai"); expect(model().value).toBe("api-model");
      expect(fetchMock.mock.calls.filter(([input, init]) => String(input).endsWith("/ai/pipeline") && init?.method === "PUT")).toHaveLength(1);
    });

    it("conserve la dernière sélection A après des changements rapides A, B puis A", async () => {
      const fetchMock = await setup(); const originalFetch = fetchMock.getMockImplementation()!;
      const finishes: ((response: Response) => void)[] = [];
      fetchMock.mockImplementation(async (input, init) => String(input).includes("/ai/models?")
        ? new Promise<Response>(resolve => { finishes.push(resolve); }) : originalFetch(input, init));
      await changeProvider("chatgpt_oauth"); await changeProvider("openai"); await changeProvider("chatgpt_oauth");
      expect(finishes).toHaveLength(3);
      for (const [index, name] of [[2, "account-current"], [1, "api-old"], [0, "account-old"]] as const) {
        await act(async () => finishes[index]({ ok: true, json: async () => ({ models: [{ model: name, capabilities }] }) } as Response));
      }
      expect(provider().value).toBe("chatgpt_oauth"); expect(model().value).toBe("account-current");
      expect([...model().options].map(option => option.value)).toEqual(["account-current"]);
      expect(fetchMock.mock.calls.filter(([input, init]) => String(input).endsWith("/ai/pipeline") && init?.method === "PUT")).toHaveLength(1);
    });
  });

  it.each([
    ["Erreur textuelle", "Erreur textuelle"],
    [{ message: "Incomplete preparation", missing: ["cv.docx"] }, "Incomplete preparation"],
    [{ missing: ["cv.docx"] }, '{"missing":["cv.docx"]}'],
    [[{ loc: ["body", "company"], msg: "Company obligatoire" }, { msg: "URL invalide" }], "Company obligatoire; URL invalide"],
  ])("affiche une erreur API lisible : %j", async (detail, message) => {
    const fetchMock = mockApi(); const originalFetch = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => String(input).endsWith("/validate")
      ? { ok: false, status: 422, json: async () => ({ detail }) } as Response : originalFetch(input, init));
    window.history.replaceState(null, "", "/kanban");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    await act(async () => [...host.querySelectorAll("button")].find(button => button.textContent === "Approve")?.click());
    expect(host.querySelector("main > .error")?.textContent).toBe(`${message}×`);
    expect(host.textContent).not.toContain("[object Object]");
  });

  it("traite une erreur de déconnexion sans rejet non géré", async () => {
    const fetchMock = mockApi(false, {
      chatgpt: { connected: true, mode: "Abonnement ChatGPT / Codex" }, api_keys: {
        openai: { configured: false, billing_confirmed: false }, anthropic_api: { configured: false, billing_confirmed: false },
        gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: false, billing_confirmed: false },
      }, active: null, capabilities: { structured_output: false, web_search: false, reasoning_effort: false, streaming: false },
    });
    const originalFetch = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/auth/chatgpt/logout")) throw new Error("Déconnexion indisponible");
      return originalFetch(input, init);
    });
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    await act(async () => [...host.querySelectorAll("button")].find(button => button.textContent === "Disconnect")?.click());
    expect(host.querySelector("main > .error")?.textContent).toContain("Déconnexion indisponible");
  });

  it("traite une erreur réseau pendant le polling OAuth", async () => {
    const fetchMock = mockApi(); const originalFetch = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/auth/chatgpt/start")) return { ok: true, json: async () => ({ authorization_url: "https://example.test/oauth" }) } as Response;
      if (String(input).endsWith("/auth/chatgpt/status")) throw new Error("Connexion indisponible");
      return originalFetch(input, init);
    });
    vi.spyOn(window, "open").mockImplementation(() => null);
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    vi.useFakeTimers();
    try {
      await act(async () => [...host.querySelectorAll("button")].find(button => button.textContent === "Continue with ChatGPT")?.click());
      await act(async () => vi.advanceTimersByTimeAsync(1500));
      expect(host.querySelector("main > .error")?.textContent).toContain("Connexion indisponible");
    } finally { vi.useRealTimers(); }
  });

  it("traite l’échec du dernier rafraîchissement de préparation", async () => {
    const fetchMock = mockApi(); const originalFetch = fetchMock.getMockImplementation()!;
    let current = { ...application, status: "DETECTED", preparation_state: undefined as "queued" | undefined };
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/ai/config")) {
        const config = await (await originalFetch(input, init)).json();
        return { ok: true, json: async () => ({ ...config, capabilities: { ...config.capabilities, structured_output: true } }) } as Response;
      }
      if (url.endsWith("/applications/7/select")) {
        current = { ...current, status: "SHORTLISTED", preparation_state: "queued" };
        return { ok: true, json: async () => current } as Response;
      }
      if (url.endsWith("/applications")) return { ok: true, json: async () => [current] } as Response;
      if (url.endsWith("/applications/7")) throw new Error("Détail temporairement indisponible");
      return originalFetch(input, init);
    });
    window.history.replaceState(null, "", "/kanban");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    await act(async () => [...host.querySelectorAll("button")].find(button => button.textContent === "Select")?.click());
    expect(host.querySelector("main > .error")?.textContent).toContain("Détail temporairement indisponible");
    expect(host.textContent).toContain("Queued for preparation");
  });

  it("affiche les compteurs détaillés de la dernière recherche", async () => {
    mockApi(false, {
      chatgpt: { connected: true, mode: "Abonnement ChatGPT / Codex" },
      api_keys: {
        openai: { configured: false, billing_confirmed: false }, anthropic_api: { configured: false, billing_confirmed: false },
        gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: false, billing_confirmed: false },
      }, active: { provider_id: "openai", auth_mode: "chatgpt_oauth", model: "gpt-test", effort: "medium", label: "ChatGPT — subscription" },
      capabilities: { structured_output: true, web_search: true, reasoning_effort: true, streaming: true },
    }, {
      finished_at: "2026-10-04T12:00:00Z", status: "SUCCESS", model: "gpt-test", effort: "medium",
      provider_id: "openai", profile_mode: "knowledge_base", provider_results_raw: 12,
      new_count: 5, duplicate_count: 4, ignored_count: 2, rejected_count: 3, web_search_calls: 3, search_call_count: 3,
      verified_open: 8, verified_closed: 2, verified_invalid: 1, verification_unknown: 1,
      raw_result_count: 24, admitted_result_count: 18, merged_count: 12, known_url_count: 4,
      unknown_url_count: 8, likely_job_detail_count: 6, obvious_non_job_count: 2,
      candidate_count: 5, fetch_attempted: 5, fetch_succeeded: 4, fetch_failed: 1,
      ai_input_count: 5, ai_batch_count: 1, ai_decision_count: 5,
      ai_keep_count: 2, ai_reject_count: 2, ai_review_count: 1,
      ai_missing_decision_count: 2, ai_retry_count: 1,
      per_query_stats: '[{"raw_result_count":8,"admitted_result_count":6}]',
    });
    window.history.replaceState(null, "", "/");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(host.textContent).toContain("12 merged URLs");
    expect(host.textContent).toContain("24 raw");
    expect(host.textContent).toContain("6 likely jobs");
    expect(host.textContent).toContain("fetch 5 attempted/4 succeeded/1 failed");
    expect(host.textContent).toContain("AI: 1 batches · 5 inputs · 5 decisions");
    expect(host.textContent).toContain("2 KEEP · 2 REJECT · 1 REVIEW");
    expect(host.textContent).toContain("2 initially missing decisions · 1 retry");
    expect(host.textContent).toContain("Per query : Q1 8/6");
    expect(host.textContent).toContain("5 new");
    expect(host.textContent).toContain("4 already known");
    expect(host.textContent).toContain("2 expired jobs");
    expect(host.textContent).toContain("1 invalid links");
    expect(host.textContent).toContain("1 unknown checks");
    expect(host.textContent).toContain("3 rejected");
    expect(host.textContent).toContain("3 Search requests");
  });

  const directRun = {
    status: "SUCCESS", model: "gpt-test", search_mode: "AI_DIRECT", search_provider: "chatgpt_oauth",
    search_model: "gpt-test", search_call_count: 2, web_search_calls: 2,
    provider_results_raw: 0, new_count: 0, duplicate_count: 0, ignored_count: 0, rejected_count: 0,
    raw_result_count: 0, extracted_url_count: 0, invalid_url_count: 0, source_url_count: 0,
    admitted_result_count: 0, merged_count: 0, intra_query_duplicate_count: 0, global_duplicate_count: 0,
    known_url_count: 0, unknown_url_count: 0, likely_job_detail_count: 0, unknown_candidate_count: 0,
    obvious_non_job_count: 0, candidate_count: 0, fetch_attempted: 0, fetch_succeeded: 0, fetch_failed: 0,
    snippet_fallback_count: 0, ai_input_count: 0, ai_batch_count: 0, ai_decision_count: 0,
    ai_keep_count: 0, ai_reject_count: 0, ai_review_count: 0, ai_missing_decision_count: 0, ai_retry_count: 0,
    verified_open: 0, verified_closed: 0, verified_invalid: 0, verification_unknown: 0,
    open_or_unknown_count: 0, inserted_count: 0,
  };

  it("affiche les étapes communes et les observations Web AI_DIRECT", async () => {
    mockApi(false, undefined, {
      ...directRun, web_search_calls: 4, raw_result_count: 8, extracted_url_count: 7, invalid_url_count: 1,
      source_url_count: 3, admitted_result_count: 6, merged_count: 4, intra_query_duplicate_count: 1,
      global_duplicate_count: 2, known_url_count: 1, unknown_url_count: 3, likely_job_detail_count: 2,
      unknown_candidate_count: 1, candidate_count: 3, fetch_attempted: 3, fetch_succeeded: 2,
      fetch_failed: 1, snippet_fallback_count: 1, ai_input_count: 3, ai_batch_count: 1,
      ai_decision_count: 3, ai_keep_count: 1, ai_reject_count: 1, ai_review_count: 1,
      verification_unknown: 1, verified_open: 1, rejected_count: 1, open_or_unknown_count: 2,
      inserted_count: 2, search_input_tokens: 10, search_output_tokens: 20, search_total_tokens: 30,
      ai_input_tokens: 40, ai_output_tokens: 50, ai_total_tokens: 90,
    });
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    const funnel = host.querySelector(".search-profile-last details")?.textContent;
    expect(funnel).toContain("2 Search requests · 8 raw (AI output) · 4 observed web tools · 3 unique observed web sources");
    expect(funnel).toContain("7 extracted URLs · 1 invalid URLs · 6 admitted · 4 unique");
    expect(funnel).toContain("duplicates 1 intra/2 global · 1 known · 3 unknown");
    expect(funnel).toContain("2 likely jobs · 1 ambiguous · 0 non-jobs · 3 candidates");
    expect(funnel).toContain("fetch 3 attempted/2 succeeded/1 failed/1 snippets");
    expect(funnel).toContain("AI: 1 batches · 3 inputs · 3 decisions · 1 KEEP · 1 REJECT · 1 REVIEW");
    expect(funnel).toContain("Result : 2 retained · 2 imported");
    expect(funnel).toContain("Observed tokens : Search 10/20/30 · Screening 40/50/90 · Observed total 120");
  });

  it("préserve les vrais zéros AI_DIRECT sans repli of un ancien compteur", async () => {
    mockApi(false, undefined, { ...directRun, provider_results_raw: 99 });
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    const funnel = host.querySelector(".search-profile-last details")?.textContent;
    expect(funnel).toContain("2 Search requests · 0 raw (AI output) · 2 observed web tools · 0 unique observed web sources");
    expect(funnel).toContain("0 extracted URLs · 0 invalid URLs · 0 admitted · 0 unique");
    expect(funnel).toContain("0 known · 0 unknown");
    expect(funnel).toContain("fetch 0 attempted/0 succeeded/0 failed/0 snippets");
    expect(funnel).toContain("AI: 0 batches · 0 inputs · 0 decisions");
    expect(funnel).toContain("Result : 0 retained · 0 imported");
    expect(funnel).not.toContain("99");
    expect(host.textContent).toContain("0 merged URLs");
  });

  it.each(["AI_DIRECT", undefined])("affiche les métriques indisponibles et les anciens runs sans faux zéro (%s)", async searchMode => {
    mockApi(false, undefined, {
      status: "SUCCESS", model: "gpt-test", search_mode: searchMode, provider_results_raw: 12,
      duplicate_count: 4, new_count: 2, raw_result_count: null, merged_count: null,
      known_url_count: null, fetch_attempted: null, fetch_succeeded: null, fetch_failed: null,
      snippet_fallback_count: null, web_search_calls: null, source_url_count: null,
      ai_input_tokens: 10, ai_total_tokens: 10,
    });
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    const funnel = host.querySelector(".search-profile-last details")?.textContent;
    expect(funnel).toContain("Search : — Search requests · — raw");
    expect(funnel).toContain("— extracted URLs · — invalid URLs · — admitted · — unique");
    expect(funnel).toContain("— known · — unknown");
    expect(funnel).toContain("fetch —");
    expect(funnel).toContain("AI: — batches · — inputs · — decisions");
    expect(funnel).toContain("Observed tokens : Search —/—/— · Screening 10/—/10 · Observed total —");
    expect(funnel).not.toMatch(/\b0 (?:raw|batches|attempted|known|unknown)\b/);
    if (searchMode === "AI_DIRECT") expect(funnel).toContain("— observed web tools · — unique observed web sources");
    else expect(funnel).not.toContain("observed web tools");
  });

  it("actualise un run RUNNING externe, conserve son snapshot en erreur et arrête le polling au résultat", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      let latest = { ...directRun, status: "RUNNING", raw_result_count: 3, extracted_url_count: 2 };
      let unavailable = false;
      const fetchMock = mockApi(false, undefined, latest);
      const initial = fetchMock.getMockImplementation()!;
      fetchMock.mockImplementation(async input => {
        if (String(input).endsWith("/codex/discovery/latest")) {
          if (unavailable) throw new Error("Backend temporairement indisponible");
          return { ok: true, json: async () => latest } as Response;
        }
        return initial(input);
      });
      await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
      expect(host.textContent).toContain("2 Search requests · 3 raw");
      latest = { ...latest, raw_result_count: 8, extracted_url_count: 7 };
      await act(async () => vi.advanceTimersByTimeAsync(2000));
      expect(host.textContent).toContain("2 Search requests · 8 raw");
      expect(host.textContent).toContain("7 extracted URLs");
      unavailable = true;
      await act(async () => vi.advanceTimersByTimeAsync(2000));
      expect(host.textContent).toContain("2 Search requests · 8 raw");
      const applicationReads = () => fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/applications")).length;
      const beforeFinish = applicationReads();
      unavailable = false; latest = { ...latest, status: "SUCCESS", inserted_count: 2 };
      await act(async () => vi.advanceTimersByTimeAsync(2000));
      expect(host.textContent).toContain("2 imported");
      expect(applicationReads()).toBe(beforeFinish + 1);
      const finalReads = fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/codex/discovery/latest")).length;
      await act(async () => vi.advanceTimersByTimeAsync(6000));
      expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/codex/discovery/latest"))).toHaveLength(finalReads);
    } finally { vi.useRealTimers(); }
  });

  it("interroge les snapshots pendant que le lancement synchrone attend le screening", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      let latest: typeof directRun | null = null;
      let finish!: (response: Response) => void;
      const fetchMock = mockApi(false, {
        chatgpt: { connected: true, mode: "Abonnement ChatGPT / Codex" }, api_keys: {
          openai: { configured: false, billing_confirmed: false }, anthropic_api: { configured: false, billing_confirmed: false },
          gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: false, billing_confirmed: false },
        },
        active: { provider_id: "openai", auth_mode: "chatgpt_oauth", model: "gpt-test", effort: "medium", label: "ChatGPT" },
        capabilities: { structured_output: true, web_search: true, reasoning_effort: true, streaming: true },
      });
      const initial = fetchMock.getMockImplementation()!;
      fetchMock.mockImplementation(async input => {
        if (String(input).endsWith("/codex/discover")) return new Promise<Response>(resolve => { finish = resolve; });
        if (String(input).endsWith("/codex/discovery/latest")) return { ok: true, json: async () => latest } as Response;
        return initial(input);
      });
      await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
      const search = [...host.querySelectorAll("button")].find(button => button.textContent === "Search for new jobs")!;
      await act(async () => search.click());
      latest = { ...directRun, status: "RUNNING", raw_result_count: 8, extracted_url_count: 7 };
      await act(async () => vi.advanceTimersByTimeAsync(2000));
      expect(host.textContent).toContain("Searching…");
      expect(host.textContent).toContain("2 Search requests · 8 raw");
      expect(host.textContent).toContain("AI: 0 batches");
      latest = { ...latest, status: "SUCCESS", inserted_count: 2 };
      await act(async () => finish({ ok: true, json: async () => ({ created: 2 }) } as Response)); await flush();
      expect(host.textContent).toContain("2 imported");
      expect(host.textContent).toContain("Search for new jobs");
      const finalReads = fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/codex/discovery/latest")).length;
      await act(async () => vi.advanceTimersByTimeAsync(4000));
      expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/codex/discovery/latest"))).toHaveLength(finalReads);
    } finally { vi.useRealTimers(); }
  });

  it("charge le snapshot d'un échec rapide sans effacer l'erreur de lancement", async () => {
    let latest = { ...directRun, raw_result_count: 3, extracted_url_count: 2 };
    const fetchMock = mockApi(false, {
      chatgpt: { connected: true, mode: "Abonnement ChatGPT / Codex" }, api_keys: {
        openai: { configured: false, billing_confirmed: false }, anthropic_api: { configured: false, billing_confirmed: false },
        gemini_api: { configured: false, billing_confirmed: false }, deepseek_api: { configured: false, billing_confirmed: false },
      },
      active: { provider_id: "openai", auth_mode: "chatgpt_oauth", model: "gpt-test", effort: "medium", label: "ChatGPT" },
      capabilities: { structured_output: true, web_search: true, reasoning_effort: true, streaming: true },
    });
    const initial = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async input => {
      if (String(input).endsWith("/codex/discover")) {
        latest = { ...latest, status: "FAILED", raw_result_count: 8, extracted_url_count: 7 };
        return { ok: false, status: 502, json: async () => ({ detail: "Échec du screening" }) } as Response;
      }
      if (String(input).endsWith("/codex/discovery/latest")) return { ok: true, json: async () => latest } as Response;
      return initial(input);
    });
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(host.textContent).toContain("2 Search requests · 3 raw");
    const search = [...host.querySelectorAll("button")].find(button => button.textContent === "Search for new jobs")!;
    await act(async () => search.click()); await flush();
    expect(host.querySelector("main > .error")?.textContent).toContain("Échec du screening");
    expect(host.querySelector(".search-profile")?.textContent).toContain("FAILED");
    expect(host.textContent).toContain("2 Search requests · 8 raw");
    expect(host.textContent).toContain("7 extracted URLs");
    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/codex/discovery/latest")).length).toBeGreaterThanOrEqual(2);
    expect(search.disabled).toBe(false);
  });

  it("enregistre Kanban, détail et Analysis dans l’historique", async () => {
    mockApi(); window.history.replaceState(null, "", "/kanban");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    await act(async () => (host.querySelector(".ticket") as HTMLElement).click()); await flush();
    expect(window.location.pathname).toBe("/applications/7");
    const analysis = [...host.querySelectorAll("button")].find(button => button.textContent === "Open analysis") as HTMLButtonElement;
    await act(async () => analysis.click()); await flush();
    expect(window.location.pathname).toBe("/applications/7/analysis");
    await act(async () => { window.history.back(); await waitForPath("/applications/7"); });
    expect(window.location.pathname).toBe("/applications/7");
    await act(async () => { window.history.back(); await waitForPath("/kanban"); });
    expect(window.location.pathname).toBe("/kanban");
    await act(async () => { window.history.forward(); await waitForPath("/applications/7"); });
    expect(window.location.pathname).toBe("/applications/7");
    await act(async () => { window.history.forward(); await waitForPath("/applications/7/analysis"); });
    expect(window.location.pathname).toBe("/applications/7/analysis");
  });

  it.each(["/kanban", "/applications/7"])("sélectionne l’job incertaine via l’endpoint normal depuis %s", async path => {
    let current = { ...application, status: "DETECTED", eligibility_status: "ELIGIBILITY_UNCERTAIN" as const,
      eligibility_reason: "Eligibility determined by job screening.", preparation_state: undefined as "queued" | undefined };
    const fetchMock = mockApi();
    const initial = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/ai/config")) {
        const config = await (await initial(input, init)).json();
        return { ok: true, json: async () => ({ ...config, capabilities: { ...config.capabilities, structured_output: true } }) } as Response;
      }
      if (url.endsWith("/applications/7/select")) {
        current = { ...current, status: "SHORTLISTED", preparation_state: "queued" };
        return { ok: true, json: async () => current } as Response;
      }
      if (url.endsWith("/applications")) return { ok: true, json: async () => [current] } as Response;
      if (url.endsWith("/applications/7")) return { ok: true, json: async () => current } as Response;
      return initial(input, init);
    });
    const confirm = vi.spyOn(window, "confirm");
    window.history.replaceState(null, "", path);
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    const select = [...host.querySelectorAll("button")].find(button => button.textContent === "Select")!;
    expect(select.disabled).toBe(false);
    await act(async () => select.click()); await flush();
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/applications/7/select"), expect.objectContaining({ method: "POST" }));
    expect(host.textContent).toContain("Queued for preparation");
    expect(host.textContent).toContain("Eligibility uncertain");
    expect(host.textContent).toContain("Some requirements need verification.");
    expect(host.textContent).not.toMatch(/ELIGIBILITY_UNCERTAIN|screening/);
    expect(host.textContent).not.toContain("Select");
    expect(confirm).not.toHaveBeenCalled();
  });

  it("charge une URL directe et affiche une erreur pour un id inconnu", async () => {
    mockApi(); window.history.replaceState(null, "", "/applications/7/analysis");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(host.textContent).toContain("Analysis — Example Research");
    expect([...host.querySelectorAll("aside a")].find(link => link.textContent === "Applications")?.className).toBe("active");
    await act(async () => { window.history.pushState({}, "", "/applications/999"); window.dispatchEvent(new PopStateEvent("popstate")); }); await flush();
    expect(host.textContent).toContain("Application introuvable");
  });

  it.each(["/applications/7", "/applications/7/", "/applications/7/analysis"])("charge la candidature depuis %s", async path => {
    const fetchMock = mockApi(); window.history.replaceState(null, "", path);
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/applications\/7$/), expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(host.querySelector("main h2")?.textContent).toBe(path.endsWith("/analysis") ? "Analysis — Example Research" : "Example Research");
    expect(host.textContent).not.toContain("Chargement de la candidature");
  });

  it.each(["abc", "0", "-1", "12suffix"])("rejette l’identifiant invalide %s sans requête de détail", async id => {
    const fetchMock = mockApi(); window.history.replaceState(null, "", `/applications/${id}`);
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(host.textContent).toContain("Application not found.");
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith(`/applications/${id}`))).toBe(false);
  });

  it("conserve la route New job sans charger une candidature", async () => {
    const fetchMock = mockApi(); window.history.replaceState(null, "", "/applications/new");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    expect(host.querySelector("main h2")?.textContent).toBe("New job");
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/applications/new"))).toBe(false);
  });

  it.each([false, true])("ignore l’ancienne réponse de candidature après navigation (erreur : %s)", async failed => {
    const fetchMock = mockApi(); const originalFetch = fetchMock.getMockImplementation()!;
    let finish!: (response: Response) => void; let fail!: (error: Error) => void;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/applications/7")) return new Promise<Response>((resolve, reject) => { finish = resolve; fail = reject; });
      if (String(input).endsWith("/applications/8")) return { ok: true, json: async () => ({ ...application, id: 8, company: "Company actuelle" }) } as Response;
      return originalFetch(input, init);
    });
    window.history.replaceState(null, "", "/applications/7");
    await act(async () => root.render(<BrowserRouter><App /></BrowserRouter>)); await flush();
    await act(async () => { window.history.pushState({}, "", "/applications/8"); window.dispatchEvent(new PopStateEvent("popstate")); });
    expect(host.querySelector(".detail-head h2")?.textContent).toBe("Company actuelle");
    await act(async () => {
      if (failed) fail(new Error("Ancienne erreur"));
      else finish({ ok: true, json: async () => application } as Response);
    });
    expect(host.querySelector(".detail-head h2")?.textContent).toBe("Company actuelle");
    expect(host.textContent).not.toContain("Chargement de la candidature");
    expect(host.textContent).not.toContain("Ancienne erreur");
  });
});

async function waitForPath(path: string) {
  for (let attempt = 0; attempt < 20 && window.location.pathname !== path; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

async function flush() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
}

describe("viewers intégrés", () => {
  it("rend titres, gras, listes et liens Markdown avec retour", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, text: async () => "# Titre\n\n**Fort**\n\n- Élément\n\n[Lien](https://example.test)" }));
    const back = vi.fn();
    await act(async () => root.render(<MarkdownViewer application={application} kind="analysis" back={back} />));
    await flush();
    expect(host.querySelector("h1")?.textContent).toBe("Titre");
    expect(host.querySelector("strong")?.textContent).toBe("Fort");
    expect(host.querySelector("li")?.textContent).toBe("Élément");
    expect(host.querySelector("a")?.rel).toContain("noopener");
    (host.querySelector("button") as HTMLButtonElement).click(); expect(back).toHaveBeenCalledOnce();
  });

  it("utilise le même viewer pour la préparation entretien", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => "# Interview preparation" });
    vi.stubGlobal("fetch", fetchMock);
    await act(async () => root.render(<MarkdownViewer application={application} kind="interview-prep" back={() => undefined} />));
    await flush();
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/files/interview-prep"), expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(host.querySelector("h1")?.textContent).toBe("Interview preparation");
  });

  it("prévisualise le DOCX de la lettre sans demander le PDF", async () => {
    let resolveDocx!: (value: object) => void;
    const back = vi.fn();
    const fetchMock = vi.fn((_url: string) => new Promise(resolve => { resolveDocx = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    await act(async () => root.render(<CoverLetterViewer application={application} back={back} />));
    expect(host.textContent).toContain("Loading cover letter…");
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/cover-letter.docx"));
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/cover-letter.pdf"))).toBe(false);
    resolveDocx({ ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) });
    await flush();
    expect(renderAsync).toHaveBeenCalledOnce();
    expect(host.querySelector(".docx-viewer")?.classList).toContain("ready");
    expect((host.querySelector("a[download]") as HTMLAnchorElement).href).toContain("/cover-letter.docx");
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    fetchMock.mockResolvedValueOnce({ ok: true, blob: async () => new Blob(["%PDF-1.7"]), headers: { get: () => 'attachment; filename="lettre-motivation.pdf"' } });
    const exportButton = [...host.querySelectorAll("button")].find(button => button.textContent === "Exporter en PDF") as HTMLButtonElement;
    await act(async () => exportButton.click()); await flush();
    expect(fetchMock).toHaveBeenLastCalledWith(expect.stringContaining("/cover-letter.pdf?v=2026-10-04"), { cache: "no-store" });
    expect(click).toHaveBeenCalledOnce();
    (host.querySelector(".viewer-toolbar .link") as HTMLButtonElement).click(); expect(back).toHaveBeenCalledOnce();
  });

  it("distingue erreur DOCX et erreur d’export PDF", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 500 })
      .mockResolvedValueOnce({ ok: false, status: 500, json: async () => ({ detail: "Conversion Office impossible." }) });
    vi.stubGlobal("fetch", fetchMock);
    await act(async () => root.render(<CoverLetterViewer application={application} back={() => undefined} />)); await flush();
    expect(host.textContent).toContain("Unable to preview this cover letter.");
    const exportButton = [...host.querySelectorAll("button")].find(button => button.textContent === "Exporter en PDF") as HTMLButtonElement;
    await act(async () => exportButton.click()); await flush();
    expect(host.textContent).toContain("Conversion Office impossible.");
  });

  it("ouvre les documents sans lien de téléchargement", async () => {
    const open = vi.fn();
    await act(async () => root.render(<FileLink item={application} kind="cv" label="Open resume" open={open} />));
    const button = host.querySelector("button") as HTMLButtonElement;
    expect(host.querySelector("a")).toBeNull(); button.click();
    expect(open).toHaveBeenCalledWith(application, "cv");
  });

  it.each(["analysis", "cover-letter", "interview-prep"] as const)("ouvre le document %s lorsque son champ est renseigné", async kind => {
    const open = vi.fn();
    await act(async () => root.render(<FileLink item={application} kind={kind} label="Ouvrir document" open={open} />));
    const button = host.querySelector("button")!;
    expect(button).not.toBeNull();
    await act(async () => button.click());
    expect(open).toHaveBeenCalledWith(application, kind);
  });

  it("ignore l’ancien Markdown après un changement de document", async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(async input => String(input).includes("/applications/7/")
      ? new Promise<Response>(resolve => { finish = resolve; }) : { ok: true, text: async () => "# Document actuel" } as Response));
    await act(async () => root.render(<MarkdownViewer application={application} kind="analysis" back={vi.fn()} />));
    await act(async () => root.render(<MarkdownViewer application={{ ...application, id: 8 }} kind="company" back={vi.fn()} />));
    expect(host.querySelector("h1")?.textContent).toBe("Document actuel");
    await act(async () => finish({ ok: true, text: async () => "# Ancien document" } as Response));
    expect(host.querySelector("h1")?.textContent).toBe("Document actuel");
  });

  it("ignore l’ancien téléchargement DOCX après un changement de fichier", async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(async input => String(input) === "old.docx"
      ? new Promise<Response>(resolve => { finish = resolve; }) : { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) } as Response));
    vi.mocked(renderAsync).mockImplementation(async (_content, target) => { target.textContent = "Document actuel"; });
    const props = { loadingLabel: "Chargement", missingLabel: "Absent", errorLabel: "Erreur" };
    await act(async () => root.render(<DocxPreview url="old.docx" {...props} />));
    await act(async () => root.render(<DocxPreview url="current.docx" {...props} />));
    await act(async () => finish({ ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) } as Response));
    expect(renderAsync).toHaveBeenCalledOnce();
    expect(host.querySelector(".docx-viewer")?.textContent).toBe("Document actuel");
  });

  it("publie uniquement le dernier rendu DOCX lorsque l’ancien renderer termine ensuite", async () => {
    let finish!: () => void;
    vi.stubGlobal("fetch", vi.fn(async input => ({ ok: true, status: 200,
      arrayBuffer: async () => Uint8Array.of(String(input) === "old.docx" ? 7 : 8).buffer,
    }) as Response));
    vi.mocked(renderAsync).mockImplementation(async (content, target) => {
      const id = new Uint8Array(content as ArrayBuffer)[0];
      if (id === 7) await new Promise<void>(resolve => { finish = resolve; });
      target.textContent = `Document ${id}`;
    });
    const props = { loadingLabel: "Chargement", missingLabel: "Absent", errorLabel: "Erreur" };
    await act(async () => root.render(<DocxPreview url="old.docx" {...props} />));
    await act(async () => root.render(<DocxPreview url="current.docx" {...props} />));
    expect(host.querySelector(".docx-viewer.ready")?.textContent).toBe("Document 8");
    await act(async () => finish());
    expect(host.querySelector(".docx-viewer.ready")?.textContent).toBe("Document 8");
  });

  it("prévisualise le DOCX mais exporte le PDF natif du backend", async () => {
    let resolveFetch!: (value: object) => void;
    const back = vi.fn();
    const fetchMock = vi.fn((_input?: RequestInfo | URL) => new Promise(resolve => { resolveFetch = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    await act(async () => root.render(<CvViewer application={application} back={back} />));
    expect(host.textContent).toContain("Loading resume…");
    resolveFetch({ ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) });
    await flush();
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/applications/7/cv"));
    expect(renderAsync).toHaveBeenCalledOnce();
    const downloadLink = [...host.querySelectorAll("a")].find(link => link.textContent === "Download DOCX") as HTMLAnchorElement;
    expect(downloadLink.href).toContain("/applications/7/cv.docx");
    expect(downloadLink.hasAttribute("download")).toBe(true);
    downloadLink.addEventListener("click", event => event.preventDefault());
    downloadLink.click();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/cv.pdf"))).toBe(false);
    const exportButton = [...host.querySelectorAll("button")].find(button => button.textContent === "Exporter en PDF") as HTMLButtonElement;
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200, blob: async () => new Blob(["%PDF-1.7"]),
      headers: { get: (name: string) => name === "Content-Disposition" ? 'attachment; filename="resume_Example Research.pdf"' : null },
    });
    expect(exportButton.disabled).toBe(false);
    await act(async () => exportButton.click()); await flush();
    expect(fetchMock).toHaveBeenLastCalledWith(expect.stringContaining("/applications/7/cv.pdf"));
    expect(window.print).not.toHaveBeenCalled();
    expect(URL.createObjectURL).toHaveBeenCalledOnce(); expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
    expect(click).toHaveBeenCalledOnce();
    expect(host.querySelector(".docx-viewer")?.classList).not.toContain("printable-document");
    (host.querySelector(".viewer-toolbar .link") as HTMLButtonElement).click(); expect(back).toHaveBeenCalledOnce();
  });

  it("affiche l'erreur des moteurs Office renvoyée par l'export CV", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) })
      .mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({ detail: "Export PDF indisponible : Microsoft Word et LibreOffice ne sont pas installés ou accessibles of cette machine." }) });
    vi.stubGlobal("fetch", fetchMock);
    await act(async () => root.render(<CvViewer application={application} back={() => undefined} />)); await flush();
    const exportButton = [...host.querySelectorAll("button")].find(button => button.textContent === "Exporter en PDF") as HTMLButtonElement;
    await act(async () => exportButton.click()); await flush();
    expect(host.textContent).toContain("Microsoft Word et LibreOffice ne sont pas installés");
    expect(window.print).not.toHaveBeenCalled();
  });

  it("affiche les erreurs fichier absent et renderer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    await act(async () => root.render(<CvViewer application={application} back={() => undefined} />)); await flush();
    expect(host.textContent).toContain("Resume not found.");
    expect(host.textContent).not.toContain("Download DOCX");
    vi.mocked(renderAsync).mockRejectedValueOnce(new Error("render"));
    vi.mocked(fetch).mockResolvedValueOnce({ ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) } as Response);
    await act(async () => root.render(<CvViewer application={{ ...application, id: 8 }} back={() => undefined} />)); await flush();
    expect(host.textContent).toContain("Unable to preview this resume.");
  });
});
