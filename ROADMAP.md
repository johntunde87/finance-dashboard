# MoneyMap — Roadmap

## Completed (v1 + v2)
- [x] Dashboard with KPI cards, trend charts, category donut
- [x] CSV upload with Chase format parsing + SHA-256 deduplication
- [x] Transaction browser with filters (date, amount range, category, search, type, sort)
- [x] Category rules engine with auto-recategorization
- [x] Savings goals with progress bars and income projections (monthly/quarterly/yearly)
- [x] Weekly Analytics module (week list + detail view with chart interactions)
- [x] Monthly Calendar view (hybrid calendar grid + detail panel)
- [x] Category filtering on weekly and monthly views
- [x] Responsive UI: mobile hamburger nav, stacked cards on mobile, tables on desktop
- [x] Day-selection interaction: tap bar to filter transactions, tap again to reset

---

## Priority 3: KPI Expansion

### Time horizons to support
Add KPI coverage for: Daily, Weekly, Monthly, Quarterly, Annual, Year-to-Date, and Compare Years (e.g., 2026 vs 2025).

### KPI groups
Each group should have averages at each time horizon:

**Income KPIs:** average daily income, average weekly income, average monthly income, average quarterly income, annual income

**Spending KPIs:** average daily spend, average weekly spend, average monthly spend, average quarterly spend, annual spend

**Net Savings KPIs:** average daily net savings, average weekly net savings, average monthly net savings, average quarterly net savings, annual net savings

### Actual vs Projected toggle
Add a toggle so the user can view: Actual, Projected, or Actual + Projected side by side.

**Projection logic for MVP:**
- Rolling 4-week average for daily/weekly projections
- Rolling 3-month average for monthly/quarterly/annual projections
- Surface the methodology in the UI (tooltip or info icon)
- Keep the logic modular so it can be upgraded later (e.g., seasonal weighting, ML)

### Year comparison
Add a basic year-over-year comparison mode:
- Current year vs prior year (e.g., 2026 vs 2025)
- Can be represented with KPI cards, summary table, charts, or a combination
- Only show if prior year data exists

### Implementation notes
- Consider a new `/api/kpis` endpoint that accepts `horizon`, `mode`, and `view` (actual/projected/both) params
- The projection functions should be standalone helpers in `app.py` so they can be reused
- KPI section could be a new page (`/kpis`) or an expanded section on the dashboard — user preference TBD

---

## Priority 4: Goal Tracker Enhancement

### Current state
Goals already support: name, target amount, current amount, target date, goal type, progress bar, monthly/quarterly/yearly needed, projected income targets, on-track/behind/complete status.

### Enhancements needed

**A. Feasibility calculation based on net savings:**
- Estimate what percentage of net savings would be required to meet the goal by target date
- Show whether the goal appears feasible at current savings rate

**B. Shortfall calculation:**
If user is not on track, show:
- Additional income needed to close the gap
- Broken down as average daily / weekly / monthly additional amount needed

**C. UI improvements:**
- On-track / at-risk visual indicator (more prominent than current badge)
- Recommended monthly contribution amount
- Archive / complete goal state (currently only delete)
- Consider a goal progress timeline or burndown chart

### Implementation notes
- Feasibility calc should use the same projection logic from Priority 3
- Add `status` field to `savings_goals` table: active, completed, archived
- Add `completed_at` timestamp for archived goals

---

## Priority 5: Categorization Learning

### Current behavior
- Keyword rules table scanned in priority order on CSV upload
- Manual re-categorization updates only that specific transaction
- System does NOT learn from manual overrides
- Adding a rule on the Rules page does persist and auto-categorizes future uploads

### Recommended approach

**A. Auto-rule creation from manual overrides:**
When user re-categorizes a transaction, extract the merchant name from the description and prompt: "Apply this category to all future transactions from [MERCHANT]?" If yes, create a persistent rule.

**B. Schema change:**
Add `source` column to `category_rules`: `default`, `user_override`, `learned`

**C. Priority handling:**
- User-created rules from overrides should have higher priority than default rules
- User corrections always win over defaults

**D. Confidence-based matching (v2+):**
- Add confidence score to rules
- Exact keyword matches → high confidence
- Fuzzy matches → flagged for review
- Prevents bad auto-rules from propagating

**E. Implementation path:**
1. Add `source` column to `category_rules` (with migration for existing rows → `default`)
2. On transaction re-categorize, extract cleanest merchant substring from description
3. Show "Create rule?" prompt in the UI
4. If confirmed, insert rule with `source='user_override'` and priority higher than defaults
5. Future: batch re-categorize existing transactions matching new rule

---

## Future (not yet specced)

### Bank syncing via Plaid
- Phase 2 feature: connect Chase via Plaid Link widget
- Plaid Development tier (free, 100 connections)
- Auto-import transactions on schedule or on-demand
- Map Plaid's JSON format into same DB schema as CSV imports
- Do NOT build until CSV workflow is fully polished

### Brevo email summaries
- Weekly or monthly email digest with key KPIs
- Uses existing Brevo integration from Capture My Proposal
- Low priority, nice-to-have

### Multi-account support
- Small possibility of adding other bank accounts
- Would need account_id foreign key on transactions table
- Separate CSV parsers per bank format
- Cross-account dashboard view
