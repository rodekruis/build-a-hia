# AI-Proposed HIA Icons

Status: agreed plan; implementation deferred.

## Goal

Propose icons when generating the HIA structure, let reviewers change them, and
export the approved choices for categories, sub-categories, and offers.

## Current Behavior

- Structure nodes do not store icon choices.
- The workbook already has `#ICON` columns for categories, sub-categories, and
  offers, but the exporter leaves them blank.
- The deployed HIA displays transparent placeholders when icons are missing.
- HIA supports icon URLs. Ionicons names alone are not available by default in
  the deployed app; export actual asset URLs instead.

## Icon Sources and Rules

- Use icons from https://rodekruis.github.io/OCHA-Humanitarian-Icons and/or
  https://ionic.io/ionicons.
- All categories must have distinct icons.
- Sub-categories must have distinct icons within their parent category.
- Sub-categories in different categories may reuse the same icon.
- There is no uniqueness requirement between a category and its sub-categories.
- Offers may all share one icon.
- Different colors or outline variants of the same symbol do not count as
  distinct icons.

## Implementation Plan

1. Create a curated, verified catalogue of suitable OCHA and Ionicons icons.
   Store stable IDs, labels, topic keywords, preview URLs, and export URLs.
   Check licensing and attribution. Start with a manageable selection rather
   than sending thousands of icons to the model.

2. Extend structure generation to propose catalogue IDs for each category and
   sub-category, plus one shared offer icon. Icons are presentation metadata,
   not source-backed facts, so they do not require citations.

3. Enforce catalogue membership and uniqueness in application code, not just
   through prompt instructions. Apply the category and sibling sub-category
   uniqueness rules after generation and manual structure edits.

4. Automatically resolve missing, unknown, or conflicting AI suggestions.
   Prefer suitable unused candidates; if none is available, silently assign
   another valid unused catalogue icon. An imperfect semantic match is
   acceptable because reviewers can inspect and replace it. Do not require
   model retries or manual intervention merely to resolve a poor suggestion.
   Fallbacks must still obey the uniqueness rules; ensure the catalogue has
   enough distinct symbols for the supported structure limits.

5. Add icon previews and a searchable visual picker to structure review.
   Reviewers can replace category and sub-category choices and the shared offer
   icon. Validate conflicts after additions, moves, merges, and revisions.
   Prevent invalid or duplicate manual selections from being approved.

6. Persist approved icon IDs and carry them through assembly. Resolve IDs to
   asset URLs in the existing workbook `#ICON` columns. Give all offers the
   selected shared default. Existing structures must support adding icons
   without regenerating their content. Icon-only edits should not invalidate
   generated content unnecessarily.

7. Add focused tests for catalogue validation, fallback assignment, uniqueness
   scopes, manual edits, persistence, legacy structures, and exact workbook
   values. Verify exported icons in HIA on desktop and mobile, including
   contrast and failed image loading. Run the repository's required lint,
   formatting, type, and pytest checks.

## Design and Hosting Decisions

- Prefer a coherent visual family: use OCHA where suitable, with a restrained
  Ionicons fallback for abstract topics such as privacy or troubleshooting.
- Strict uniqueness may reduce semantic precision for closely related topics.
  Keep the agreed rules, with reviewers responsible for final visual choices.
- Hosted URLs are simplest but introduce an external dependency. Prefer
  version-pinned assets where possible. Hosting approved assets alongside the
  deployed HIA is more reliable but requires separate deployment changes.
- Confirm the deployed HIA's rendering behavior for category, sub-category,
  and offer icons before finalizing asset formats and hosting.

## Alternatives

- Deliver the picker and export support first, then AI suggestions. This is a
  simpler staged implementation but leaves initial selection to reviewers.
- Use a single icon library for stronger visual consistency, at the cost of
  narrower topic coverage.

The agreed first version is a curated catalogue, AI proposals during structure
creation, automatic valid fallback assignment, a reviewer picker, and one
shared offer icon.