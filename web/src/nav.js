/**
 * The sidebar, as data.
 *
 * Grouped by what someone is trying to do rather than listed flat. The old nine-item
 * list gave no signal that Rulebook and Sources are the same job, or that Run → Review →
 * Verify is a sequence you move through in order — so people hunted for the next step.
 *
 * Labels differ from route paths in two places. The paths are load-bearing (bookmarks,
 * the demo script, deep links) and are deliberately unchanged:
 *   /metrics  → "Overview"            it is the landing page, not a metrics sub-view
 *   /workflow → "Policy & sign-off"   what that page actually holds
 */
export const NAV = [
  {
    // Ungrouped: the landing page is a destination, not a member of a category.
    items: [{ to: "/metrics", label: "Overview" }],
  },
  {
    title: "Terminology",
    hint: "What counts as correct",
    items: [
      { to: "/rulebook", label: "Rulebook" },
      { to: "/sources", label: "Sources", badge: "candidates" },
    ],
  },
  {
    title: "Review cycle",
    hint: "Scan, decide, prove",
    items: [
      { to: "/run", label: "Run" },
      { to: "/review", label: "Review", badge: "pending" },
      { to: "/verify", label: "Verify" },
    ],
  },
  {
    title: "Records",
    items: [{ to: "/documents", label: "Documents" }],
  },
  {
    title: "Administration",
    items: [
      { to: "/teams", label: "Teams" },
      { to: "/workflow", label: "Policy & sign-off" },
    ],
  },
];
