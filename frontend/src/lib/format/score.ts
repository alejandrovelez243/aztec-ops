/**
 * Formats a priority score (0–100) for a tabular readout.
 *
 * Invariant: at most one decimal place. The score is a ranking figure read in a column
 * against its neighbours, so `73.49` and `73.51` must render as the same glanceable value;
 * the justification lives in the breakdown beside it, never in extra digits.
 *
 * Edge cases:
 *
 * - Whole numbers render without a decimal (`73`, not `73.0`): the digit carries no
 *   information and alignment comes from `tabular-nums` on the cell, not from padding.
 * - One decimal is kept when it is significant (`73.5`).
 * - Rounding happens exactly once at one decimal, so `73.45` → `73.5` and a `-0` result of
 *   rounding renders as `0`.
 * - Out-of-range input is not clamped: 0–100 is the API's invariant (`Score.value`), and
 *   silently clamping a `140` would hide a backend bug behind a plausible number.
 */
export function formatScore(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  return (Object.is(rounded, -0) ? 0 : rounded).toString();
}
