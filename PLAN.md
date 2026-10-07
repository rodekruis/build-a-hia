# Build a HIA: v1 plan

Status: agreed product direction; steps 1-8 are implemented. Open: the compatibility
tests with a real HIA app (gate 1) and the deployment.

HIA means Helpful Information App, as stated in the demo workbook. The starter's
"humanitarian impact assessments" description has been corrected.

## Scope

An access-restricted pilot that turns source documents into a reviewable HIA draft.
There are no application accounts, no permanent project library, and no automatic
publication. Each browser has an isolated, temporary session. The export is an
`.xlsx` for the user to copy into a fresh copy of the HIA template. Appending to an
existing populated HIA is out of scope: its IDs, slugs and formulas could collide.

The HIA workbook must stay clean and publication-ready: only the template's tabs
and publishable content. References, evidence, gaps and editorial notes belong on
screen and in a separate internal review workbook, never in the HIA workbook.

The AI proposes structure and grounded content. Code owns workbook structure,
identifiers, relationships and validation. Every exported content row has
`#VISIBLE = Hide`, including rows reviewed in this application.

Important: the workbook explicitly warns that even hidden rows are public. `Hide`
controls display, not confidentiality or access. Before copying any output into a
live HIA sheet, review it for public disclosure, including workbook metadata.
Permission to send a source to Foundry is not permission to publish its contents.

## Verified demo workbook contract

Inspected [helpful-info_example - Demo.xlsx](helpful-info_example%20-%20Demo.xlsx)
on 2026-10-06. This is a populated demonstration workbook, not a clean template.
Its eight tabs, in order, are `Referral Page`, `Help`, `Options`, `Categories`,
`Sub-Categories`, `Offers`, `Q&As`, and `Chat`. The HIA export has exactly these
eight tabs; `Gaps` belongs only in the separate internal review workbook.

Preserve original header text, newlines and physical column positions from the
pinned workbook. The following is the positional field map, not replacement headers:

- `Categories`: A `#ID`, B `#VISIBLE`, C `#SLUG`, D `#NAME`, E `#DESCRIPTION`,
  F `#ICON`, G public-data warning.
- `Sub-Categories`: A parent-category name selector, B `#CATEGORY`, C `#ID`,
  D `#VISIBLE`, E `#SLUG`, F `#NAME`, G `#DESCRIPTION`, H `#ICON`, I warning.
- `Offers`: A sub-category name selector, B `#SUBCATEGORY`, C `#CATEGORY`, D `#ID`,
  E `#VISIBLE`, F `#SLUG`, G `#NAME`, H `#DESCRIPTION`, I `#ICON`,
  J `#PHONENUMBERS`, K `#EMAILS`, L `#WEBURLS`, M `#ADDRESS`, N `#OPENWEEK`,
  O `#OPENWEEKEND`, P `#NEEDTOKNOW`, Q `#MOREINFO`, R `#CHAPTER`, S warning.
- `Q&As`: A sub-category name selector, B `#SUBCATEGORY`, C `#CATEGORY`,
  D `#VISIBLE`, E blank, F `#SLUG`, G `#PARENT`, H `#QUESTION`, I `#ANSWER`,
  J `#UPDATED`, K `#HIGHLIGHT`, L blank, M warning. There is no question `#ID`.
- `Referral Page`: A `#KEY`, B `#VALUE`, C `#EXAMPLE value`, D warning position,
  E `SOURCE`, F `TRANSLATION (auto)`. Configuration keys occur at fixed rows;
  do not collapse blank rows. Includes locale, title, greeting, intro, contacts,
  UI labels, feedback and a last-updated timestamp. Its `SOURCE` column is not
  a place for document provenance; any populated translation text must be public-safe.
- `Help`: icon guidance, not generated content. Preserve it as supporting material.
- `Options`: C contains `Show`/`Hide`; D contains `Yes`/`No`. Retain these values
  and positions for validation.
- `Chat`: A `#KEY`, B `#VALUE`, C `#EXAMPLE value`; includes `#system-prompt`.
  Preserve schema/example guidance, but do not generate chatbot configuration in v1.

Categories link to sub-categories through `#CATEGORY`. Offers and questions carry
both `#SUBCATEGORY` and `#CATEGORY`. Only questions use `#PARENT`, referencing a
parent question's slug, not a numeric category ID. Demo child questions can have
blank slugs; verify required versus optional slugs against the consuming HIA app.

The demo calculates category, sub-category and offer IDs with `ROW()-1` and resolves
relationships by matching parent names using named ranges. Its lookup/validation
ranges end around rows 99-100. It also contains `Hidden` scaffolding rows, a merged
phone/email example, unrelated helper formulas and date-formatted opening-hour
examples. These are reasons not to blindly clone sample data or formatting.

## 1. Getting started

- Capture country, crisis or situation, target group, regions or locations, and
  output language. Record source languages and an optional reference date.
- Map output language to `#locale.language` and confirm `#locale.dir` (`ltr`,
  `rtl` or `auto`). Do not assume the demo's alternative languages or Google
  Translate settings are appropriate for the new HIA.
- Include the approved context in every subsequent model request.
- Issue an opaque, random session identifier in a secure, HttpOnly cookie with
  an appropriate SameSite policy. Store content server-side, not in the cookie.
- Restore the session on refresh while it remains valid. Losing the cookie,
  deleting the session or reaching expiry makes the work unavailable; there is
  no cross-device recovery.

## 2. Attach sources

- Accept PDF, DOCX, XLSX, images and URLs, subject to format-specific conversion
  acceptance tests. A URL identifies a single web page or a downloadable file.
- Fetch only the submitted URL. Do not follow links or sitemaps or discover linked
  documents; users add each further page or document URL explicitly.
- Fetch static responses only, without browser rendering. Report pages that need
  JavaScript, login, paywalls or bot protection as unsupported and suggest
  uploading the document or a saved PDF instead. Do not bypass access controls.
- Capture source title, source URL where applicable, and document date when
  available. Missing dates are unknown, not assumed current.
- For URLs, preserve the final URL after redirects, retrieval time and any available
  publication/update date. Retrieval time is not proof of factual freshness.
- Enforce configurable ceilings for file bytes, session bytes, page counts,
  document counts, processing time and URL response size. Reject unsupported,
  encrypted or malformed files with an actionable message.
- Validate actual file content, not just its extension. Bound redirects and
  block URL access to local, private and infrastructure metadata addresses,
  including after redirects and DNS resolution.

## 3. Convert in the background

- Use Docling to convert supported sources to markdown, retaining page, sheet,
  table and source references alongside the text. Verify OCR and table fidelity
  on representative humanitarian documents before enabling each format.
- Run URL fetching and conversion in queue-triggered Container Apps Jobs with
  shared temporary storage, not in web request handlers, a process-local
  dictionary or a Flask cookie.
- Show queued and processing stages, completed document counts and individual
  failures. Use percentages only where measurable, such as converted PDF page
  chunks. Support cancellation and failed-job retry.
- Expect CPU conversion to take minutes per document. While conversion runs,
  collect context questions not answered on the first screen. These answers
  become part of the approved context.
- Let the user retry, exclude failed sources or explicitly continue with partial
  sources. Extraction failures must not be described as missing source facts.
  A fact not found in collected sources is not proof that it does not exist;
  carry conversion limitations into review and gaps.
- Remove originals after successful conversion when retries no longer need them.
  Failed originals remain subject to session expiry and deletion.
- Split converted text into chunks labeled with stable IDs that carry source and
  page/sheet references, such as `[S3-p12-c2]`. Chunks exist for citation, not retrieval.
- After conversion, count source tokens against a configurable session ceiling
  sized so all sources fit one model request with room for instructions and
  output. When exceeded, say so clearly and let the user exclude sources.

### Docling practices

- Install CPU-only `torch`/`torchvision` from an explicit PyTorch CPU index via
  `[tool.uv.sources]`, with RapidOCR on ONNX Runtime for OCR. Do not ship CUDA wheels.
- Import Docling lazily inside the conversion service so the web app and unrelated
  tests never load it.
- Count PDF pages with `pypdfium2` before conversion and enforce page ceilings
  from that count.
- Convert PDFs in small page-range chunks (5 pages) to avoid
  native out-of-memory errors (`std::bad_alloc`). Track each chunk's page range so
  page references stay correct.
- Set Docling's `document_timeout` per chunk from its page count (30 s per page, bounded to 60-600 s).
- Convert with the standard layout pipeline first, without OCR. Retry only failed
  chunks and empty pages with full-page OCR (`OcrAutoOptions(mode=OcrMode.FULL_PAGE)`)
  for scanned or image-only pages. Reuse converters across chunks so models load once.
- Call `convert(..., raises_on_error=False)`. Accept `SUCCESS` and
  `PARTIAL_SUCCESS`; record partial and failed chunks as conversion limitations.
- Delete local temporary files in `finally` blocks.
- In the image, install `libgl1`, `libxcb1` and `libglib2.0-0`, run
  `uv sync --frozen --no-dev`, and set `PYTHONUNBUFFERED=1`.
- Download Docling and OCR models at image build time:
  every job execution starts a fresh replica and must not fetch models on start.

## 4. Propose the structure

- Use a selected, tested Azure AI Foundry deployment. Configure deployment,
  credentials, token budgets and timeouts through application settings.
- Supply versioned HIA examples as structural and stylistic guidance only;
  never use their factual content as evidence for the new context.
- Send all labeled source chunks with the approved context. Do not summarize or
  pre-filter sources in v1.
- Treat uploaded text as untrusted evidence, never as instructions to the model.

## 5. Review the structure

- Provide an editable tree with rename, add, remove, reorder and change-parent
  controls, plus a free-text box for broader changes.
- Support merging with a preview of the resulting children and explicit approval.
  Prevent cycles and invalid parent relationships.
- Keep exported category and sub-category names unambiguous for the template's
  name-based selectors. Warn about duplicate names before approval.
- Repeat proposal and review until approved. Freeze a structure version before
  content generation.
- Version source sets and context too. Changes invalidate affected generated
  results; stale background jobs must not overwrite a newer draft.

## 6. Generate offers and Q&As

- Generate one approved sub-category per request from the full labeled sources and
  the approved context. Keep instructions, sources and context as an identical
  prompt prefix across requests so prompt caching applies; put the sub-category
  task last.
- Require structured JSON validated with Pydantic. Apply separate evidence and
  semantic checks: schema-valid output is not necessarily factually supported.
- Require the model to cite chunk IDs for factual claims. Code rejects unknown IDs
  and maps valid ones to source, page/sheet references and supporting passages.
  Preserve provenance as separate structured data and expose it during
  review; do not embed audit citations or internal source identifiers in public
  descriptions, answers or other HIA fields.
- Keep links that help the public act, such as service websites, application forms
  and official guidance. A useful public link is not an internal evidence marker;
  include it only when relevant, supported and suitable for public disclosure.
- Leave unsupported fields blank. Flag conflicting and potentially outdated
  information rather than choosing an unsupported answer.
- Preserve names, addresses, numbers and eligibility details during translation.
  Flag uncertain translations for review.
- Code assigns category, sub-category and offer IDs, slugs, `#CATEGORY` and
  `#SUBCATEGORY` links. For nested Q&As, code writes `#PARENT` as the parent
  question's slug and validates references and cycles. Do not add question IDs.
- Map offer eligibility and access requirements to `#NEEDTOKNOW`, additional
  detail to `#MOREINFO`, and source-supported grouping to optional `#CHAPTER`.
  Keep phone, email, website, address and opening-hour fields separate.
- Set question `#UPDATED` to the generated/edited content date in `YYYY-MM-DD`
  format, not an invented source verification date. Default `#HIGHLIGHT` to `No`.
- Use conservative Markdown, with one-line questions and level-3-or-lower headings
  in offers/answers. Sanitize rendered previews; do not generate raw HTML or SVG.
  Leave optional icons blank unless an approved icon catalogue is used.
- Set every exported content row to `#VISIBLE = Hide`. Nothing is published.

## 7. Review content and gaps

- Show generated offers and Q&As alongside their supporting evidence. Allow
  editing and targeted regeneration; identify human edits separately from
  source-supported claims. Structure approval is not content approval.
- Group gaps by sub-category and distinguish missing facts, conversion failures,
  conflicts, uncertain translations and potentially outdated information.
- For each gap, show the affected item/field, issue, source references where
  available, suggested action, and resolution status. Export these only to the
  separate internal review workbook, not to any HIA tab or field.
- Name a suggested contact only when supported by the documents. Otherwise leave
  the owner blank for the National Society to assign.
- Review contacts, internal referral details and other sensitive information for
  public disclosure. Exclude unsuitable details even if they are source-supported;
  `Hide` does not make them safe to copy into a public sheet.
- Ask whether to keep or drop sub-categories with no supporting content.
- Allow clean draft exports with unresolved issues recorded in the internal
  review workbook. Block structurally invalid rows and public-disclosure failures;
  factual blanks become internal gaps rather than invented data or public TODOs.

## 8. Download

- Use a pinned, versioned workbook contract. Do not fetch a mutable Google Sheet
  as the schema during generation or export.
- Provide two separate, clearly labeled downloads from the same draft snapshot:
  `hia.xlsx` for publication and `review-internal.xlsx` for internal handover.
  Downloading one must not require downloading the other.
- `hia.xlsx` contains exactly the verified template's eight tabs and column
  headers. Preserve blank columns and warning positions. Do not add provenance
  columns or tabs, or export the demo's service/contact data.
- Exclude evidence markers, source filenames/page references, raw source chunks,
  gaps, internal contacts and editorial notes from HIA cells, comments, hidden
  sheets, links and workbook metadata. Preserve explicitly approved public service
  links and contacts. `Hide` remains the visibility default, not a privacy measure.
- Preserve `Help`, `Options` and `Chat` support structures. For `Referral Page`,
  retain keys and example guidance, clear demo-specific values and translation
  samples, and populate only reviewed settings. Do not infer logos or contacts.
- `review-internal.xlsx` contains `Gaps`, `Evidence` and `Sources` tabs, with a
  clear internal-use warning. It is not a publication artifact and must not be
  copied into a live HIA sheet. Include review notes and fetch/conversion
  limitations here, not in the HIA workbook.
- Give its `Gaps` tab stable columns: `Sub-Category ID`, `Sub-Category`, `Item Type`,
  `Item ID/Slug`, `Field`, `Issue Type`, `Issue`, `Source Reference`,
  `Suggested Action`, `Suggested Contact`, `Status`.
- In `Evidence`, associate item IDs/slugs and fields with supporting passages,
  source/page/sheet references, review status and human-edit notes. In `Sources`,
  record source identifiers, titles, URLs, available dates, retrieval times and
  fetch/conversion status. Include a snapshot identifier and export time in
  the internal workbook so its references match the accompanying HIA draft.
- Full evidence remains available on screen until session expiry. Offer the
  internal workbook for download before evidence is deleted. User-downloaded
  copies are outside application retention and must be stored appropriately.
- Preserve text values such as leading-zero phone numbers. Prevent generated or
  uploaded text from becoming executable spreadsheet formulas in either workbook.
- Write opening hours as source-supported text, not Excel dates or numbers; the
  demo's numeric date cells are not a meaningful opening-hours convention.
- Export code-computed literal IDs and links without depending on spreadsheet
  recalculation. Decide during compatibility testing whether target formula
  columns should be preserved when pasting; give a tested column-specific copy
  procedure rather than assuming arbitrary row pasting is safe.
- Validate ID uniqueness within each entity type, slug scope, parent integrity,
  non-ambiguous selectors, column positions and hidden visibility. Retain necessary
  `Hidden` scaffolding only as confirmed by the consuming app; never export other
  demo records. Extend or explicitly enforce supported target lookup ranges.
- Test copying into a fresh real template and loading it in HIA. Include nested
  questions, duplicate names, non-Latin names/slugs, RTL content, blank factual
  fields and target-range boundaries. Opening the export alone is insufficient.
- A successful download does not delete the session immediately, so the user can
  review or download again before expiry. Offer an explicit delete-session action.

## Temporary storage, access and cost

- Restrict pilot access at the network or hosting layer without adding application
  accounts. Anonymous session identifiers are not sufficient access control.
- Isolate all source, job, status and download access by session. Retain CSRF
  protection and validate form input with WTForms.
- Use shared temporary storage accessible to the web app and jobs, with jobs
  that survive web restarts. Session expiry applies to queued jobs too.
- Proposed pilot expiry defaults: two hours of user inactivity and an absolute
  lifetime of 24 hours. Background activity does not extend that hard lifetime.
- Delete session content on explicit deletion or expiry. Cancel associated work
  and prevent late job results from recreating deleted content.
- Run cleanup independently of browser activity, including abandoned sessions,
  failed jobs, queue payloads, derived text, model outputs, exports and temp files.
  Exclude session content from backups and persistent job histories.
- Limit concurrent work per session and globally, rate-limit submissions, and
  enforce token/spending budgets before dispatching model requests. Return clear
  capacity errors rather than accepting unlimited work.
- Do not log documents, personal data, prompts, model responses or session tokens.
  Operational metrics may record counts, durations and sanitized failure codes.
- Source content may be sent to the approved Foundry deployment. Verify its
  processing, retention, abuse-monitoring and telemetry policies before making
  privacy claims. Application deletion does not prove provider-side deletion.
- Describe the policy as temporary processing with automatic deletion, not literal
  zero retention. State the expiry and cleanup behavior before uploads.

## Technology stack

Keep the existing Python 3.12+, Flask factory, blueprints and Flask-independent
services. Use `uv`, Ruff, ty and pytest as already required by the repository.
Review and pin new dependencies during implementation; this plan does not install
them or provision infrastructure.

| Layer | v1 choice |
| --- | --- |
| Web/UI | Flask, Gunicorn and Jinja, with a small vanilla-JS poller for job status; no separate React SPA |
| Structure editor | Server-rendered forms for rename, add, move, re-parent and merge; hierarchy rules enforced server-side |
| Hosting | Azure Container Apps: Flask web app plus Container Apps Jobs |
| Background work | Azure Storage Queues triggering event-driven Container Apps Jobs; scheduled Container Apps Job for cleanup |
| Workflow/session state | Azure Table Storage for versions, job status and expiry |
| Temporary artifacts | Private Azure Blob Storage, isolated by session |
| AI | Selected Foundry deployment's official API SDK plus Pydantic schemas |
| URL fetching | Single-URL HTTP fetch with address, redirect and size validation; no crawler or browser rendering |
| Conversion | Docling on CPU (PyTorch CPU, RapidOCR), with format-specific acceptance tests |
| Workbook export | openpyxl for both separate exports and precise cell types |
| Credentials | System-assigned managed identities; reuse Key Vault when needed |
| Operations | Container Apps logs in Log Analytics; Application Insights optional |

- Use explicit Python services for model calls and workflow state, not
  LangChain or an autonomous agent framework. Select the Foundry API/deployment
  before choosing its SDK; avoid hand-maintaining raw HTTP model calls or building
  a universal multi-provider abstraction.
- No retrieval in v1: send the full labeled sources within the session token
  ceiling. If the pilot needs larger source sets, let the structure proposal return
  relevant chunk IDs per sub-category and send generation only those chunks.
  Do not add keyword search or embeddings.
- Do not introduce Azure AI Search, a vector database, Redis, PostgreSQL, Service
  Bus or Kubernetes without a measured requirement that justifies the complexity.
- Each queue message triggers one job execution that dequeues the message,
  processes it, deletes it and exits. Use separate queues and event-driven jobs for
  conversion (sized for Docling) and generation (model calls, smaller replicas),
  built from the same image. Cap parallel executions to bound global concurrency.
- Storage Queues can redeliver messages. Make work idempotent, set the message
  visibility timeout longer than the job's replica timeout so no lease renewal is
  needed, bound retries with the dequeue count and replica retry limit, and mark
  poison messages failed without content logs. Queue payloads carry opaque
  job/session identifiers and versions, not documents, prompts or model responses.
- Expect start latency from the scaler polling interval, image pull and model
  loading. Keep the image lean and show queued status in the UI.
- Authenticate queue scale rules and storage access with managed identity where
  supported.
- Use conditional Table Storage updates to prevent concurrent or stale jobs
  from overwriting newer drafts. Check expiry/deletion before work and before
  committing results; reconcile interrupted job dispatch and abandoned work.
- Store documents, chunks and large outputs in blobs, not Table entities or queue
  messages. Use shared storage rather than process memory for durable workflow
  state; size the conversion job's CPU/memory for Docling separately.
- Deny access immediately on expiry. The scheduled cleanup job deletes artifacts
  independently of browser activity; storage lifecycle rules alone cannot enforce
  two-hour idle expiry. Define and monitor the cleanup schedule and deletion lag.
- Use a dedicated temporary Storage account for blobs, queues and tables. Configure
  soft deletion, versioning and backup policies deliberately: retained copies can
  invalidate deletion claims. Delete job-local temporary files after each
  execution, even though job replicas are ephemeral.
- Restrict pilot ingress through the organisation's network/hosting controls.
  Isolate URL fetching from private services and infrastructure metadata.
  Use least-privilege managed identity
  access for application services; do not expose credentials to fetched pages.
- Keep logs/traces sanitized, with no content capture in telemetry. Monitor queue
  age, retries, cleanup lag, resource ceilings and model usage/spend.

## Azure resource budget

Aim for eight core resources, reusing existing organisational services where
available. This counts resource-level infrastructure, not each blob, queue, table,
role assignment or model endpoint operation.

| Core resource | Count |
| --- | ---: |
| Container Apps environment | 1 |
| Container App: Flask web | 1 |
| Event-driven Container Apps Job: conversion | 1 |
| Event-driven Container Apps Job: generation | 1 |
| Scheduled Container Apps Job: cleanup | 1 |
| Temporary Storage account: blobs, queues and tables | 1 |
| Log Analytics workspace | 1 |
| Application Insights, optional | 1 |

Reuse an existing Foundry account/project/deployment, Key Vault, container registry
and approved network setup where possible. System-assigned identities do not
require separate user-assigned identity resources.

If unavailable, provision the required Foundry resources, Key Vault, Azure Container
Registry (or use an approved existing registry/GHCR), and network/private DNS
resources separately. Budget roughly 10-13 new resources for a typical standalone
setup, or 7-9 with substantial reuse. These are planning estimates, not a ceiling:
Foundry child resources and private networking can increase the exact count.
Inventory existing resources and confirm the deployment topology before provisioning.

Resource count is not the primary cost driver. Model tokens and conversion-job
CPU/memory for Docling determine much of the operating cost; set budgets and
capacity limits before the pilot.

## Implementation gates

1. Workbook inspection is complete; the positional contract is recorded above.
  Pin its version and verify required fields, slug uniqueness scope, optional
  child-question slugs, name lookup behavior, `Hidden` scaffolding and copy/paste
  behavior against the consuming HIA app and a fresh target template. Exported
  Excel validations using Google Sheets functions such as `REGEXMATCH` are not
  a substitute for application validation or real integration testing.
2. Select the Foundry deployment and spot-check structured output, language quality,
   grounding behavior, cost and relevant data policies on representative sources.
   Confirm its context window and prompt caching support, and set the session
   source-token ceiling from them.
3. Confirm the Container Apps/Storage topology, existing services to reuse, ingress
  restriction, identities, resource inventory, job sizing, replica timeouts and
  start latency. Calibrate upload, page, concurrency, time and cost ceilings;
  approve the proposed session expiry
  defaults, cleanup schedule and storage deletion/backup configuration.
4. Validate conversion and source-reference fidelity for each supported format.
  Calibrate Docling chunk size, timeouts and OCR fallback on CPU.
  Correct the starter's product description to Helpful Information App.
5. Test single-URL fetching on representative Red Cross pages and documents,
  including redirects, multilingual pages, direct PDF links, JavaScript-only
  and blocked pages.

## Delivery and acceptance

Implement in small stages: template/export contract; anonymous session lifecycle
and queue-triggered jobs; intake/URL fetching/conversion; structure review;
grounded generation/content review; separate HIA/internal review exports and
end-to-end pilot validation.

Focused tests must cover workbook compatibility, literal IDs, nested question
parents, preserved blank columns, helper ranges, public-disclosure review and
hidden rows, unsupported and conflicting facts, translation fidelity, stale job results, cross-session access,
job retry/cancellation, deletion during processing, expiry after job failure,
resource ceilings, source-token ceiling, unknown chunk citations, URL restrictions
and spreadsheet formula injection.
Infrastructure integration checks must cover queue redelivery, visibility timeout
versus replica timeout, bounded retries/poison jobs, conditional state conflicts,
dispatch recovery, web restart recovery, expiry access denial and cleanup across
blobs, tables, queues and job-local files. Verify least-privilege identity access and sanitized logs.
Export tests must assert that the HIA workbook has exactly the eight template
tabs and no internal evidence, gaps, notes or private metadata, including in
comments and hidden cells/sheets. Verify that the separate review workbook maps
evidence and gaps to the same draft snapshot and remains independently downloadable.
Conversion tests must cover PDF chunking with correct page references, OCR
fallback for scanned pages, per-chunk timeouts and partial/failed chunk reporting.
URL fetch tests must cover redirect/DNS restrictions, response size limits,
content-type validation, unsupported JavaScript-only or blocked pages, and that
links are never followed.

Follow the existing Flask factory, blueprint and service boundaries. Before
submitting implementation changes, run `uv run ruff check .`,
`uv run ruff format --check .`, `uv run ty check`, and
`uv run python -m pytest`. Test the review workflow on desktop and mobile.

Out of scope: automatic publication, permanent projects, application accounts,
cross-device recovery, live template synchronization, unbounded processing,
whole-site crawling and link following, browser rendering of JavaScript pages,
chatbot configuration generation, appending to populated HIA sheets and
systematic AI quality evaluation (reference datasets, quality metrics and prompt
regression testing).