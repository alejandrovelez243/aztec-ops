/**
 * "La mano invisible" — the motion engine of DESIGN.md §Motion.
 *
 * The three named springs (firme / carta / panel) are sampled from the real
 * damped-oscillator closed form into WAAPI-ready `linear()` easings, so the JS
 * curves and the CSS token approximations describe the same physics. On top of
 * them: FLIP for reorders, arc travel for remote SSE moves, count-up for
 * figures, and the reject shake.
 *
 * Invariant: every entry point is a no-op that lands the DOM in its final
 * state when `prefers-reduced-motion` is set — callers therefore mutate layout
 * FIRST and animate the transition after, never the reverse. Skipping that
 * order means reduced-motion users see stale positions, not calmer ones.
 */

/** The named springs of DESIGN.md §Motion. */
export type SpringPreset = "firme" | "carta" | "panel";

interface SpringSpec {
  /** Restoring force; higher is snappier. Mass is fixed at 1. */
  readonly stiffness: number;
  /** Friction; lower overshoots more. */
  readonly damping: number;
}

/** One sampled spring: a duration and a `linear()` easing WAAPI accepts. */
export interface SampledSpring {
  readonly duration: number;
  readonly easing: string;
}

/** Spring constants — the single source shared with tokens.css's approximations. */
const SPRINGS: Record<SpringPreset, SpringSpec> = {
  firme: { stiffness: 420, damping: 34 },
  carta: { stiffness: 320, damping: 26 },
  panel: { stiffness: 240, damping: 30 },
};

/** Number of stops sampled into each `linear()` easing. */
const SAMPLE_COUNT = 48;

/** The spring has settled when its envelope stays within this of the target. */
const SETTLE_EPSILON = 0.001;

const sampleCache = new Map<SpringPreset, SampledSpring>();

/**
 * Returns whether the user asked for reduced motion. Checked at call time, not
 * module load, so flipping the OS setting takes effect without a reload.
 */
export function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * Samples one named spring into a `linear()` easing plus its natural duration.
 *
 * Underdamped closed form with mass 1: the settle time is derived from the
 * decay envelope (`|1 - x(t)|` bounded by e^(-ζω₀t)·A), so snappier springs
 * genuinely produce shorter durations instead of sharing one magic number.
 * Results are cached per preset.
 */
export function sampleSpring(preset: SpringPreset): SampledSpring {
  const cached = sampleCache.get(preset);
  if (cached !== undefined) return cached;

  const { stiffness, damping } = SPRINGS[preset];
  const omega = Math.sqrt(stiffness);
  const zeta = damping / (2 * omega);
  const omegaD = omega * Math.sqrt(1 - zeta * zeta);
  const decay = zeta * omega;
  const amplitude = Math.sqrt(1 + (decay / omegaD) ** 2);

  const settleSeconds = Math.log(amplitude / SETTLE_EPSILON) / decay;
  const duration = Math.ceil((settleSeconds * 1000) / 10) * 10;

  const stops: string[] = [];
  for (let i = 0; i <= SAMPLE_COUNT; i += 1) {
    const t = (settleSeconds * i) / SAMPLE_COUNT;
    const x =
      1 -
      Math.exp(-decay * t) *
        (Math.cos(omegaD * t) + (decay / omegaD) * Math.sin(omegaD * t));
    stops.push((i === SAMPLE_COUNT ? 1 : x).toFixed(4));
  }

  const sampled: SampledSpring = {
    duration,
    easing: `linear(${stops.join(", ")})`,
  };
  sampleCache.set(preset, sampled);
  return sampled;
}

/**
 * FLIP: reorder or move elements with mass instead of teleporting them.
 *
 * Measures `targets`, applies `mutate` (the actual DOM change — the final
 * state), measures again, and plays each moved element from its inverted old
 * position to rest. Elements that did not move are untouched. Under reduced
 * motion the mutation still happens; only the travel is skipped.
 *
 * @param targets - Elements whose positions may change; measured before and after.
 * @param mutate - Performs the real DOM change; may be async.
 * @param preset - Spring for the travel; `carta` unless the move is page-level.
 * @returns Resolves when every travel has finished (immediately when reduced).
 */
export async function flip(
  targets: Iterable<HTMLElement>,
  mutate: () => void | Promise<void>,
  preset: SpringPreset = "carta",
): Promise<void> {
  const elements = [...targets];
  if (prefersReducedMotion() || elements.length === 0) {
    await mutate();
    return;
  }

  const before = new Map<HTMLElement, DOMRect>();
  for (const el of elements) before.set(el, el.getBoundingClientRect());

  await mutate();

  const { duration, easing } = sampleSpring(preset);
  const animations: Animation[] = [];
  for (const el of elements) {
    const first = before.get(el);
    if (first === undefined) continue;
    const last = el.getBoundingClientRect();
    const dx = first.left - last.left;
    const dy = first.top - last.top;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
    animations.push(
      el.animate(
        [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }],
        { duration, easing },
      ),
    );
  }
  await Promise.allSettled(animations.map((a) => a.finished));
}

/**
 * Plays an element from a previous position into its current one along a
 * slight arc — the remote-move signature (DESIGN.md §Motion): the DOM is
 * already at its destination; this makes the journey visible.
 *
 * The arc bows perpendicular to the travel direction (upward bias), scaled to
 * the distance and clamped so short hops stay subtle and long glides stay
 * believable.
 *
 * @param el - The element to animate; must already sit at its destination.
 * @param dx - Old x minus new x, in px.
 * @param dy - Old y minus new y, in px.
 * @returns Resolves when the glide settles (immediately under reduced motion).
 */
export async function travelBy(
  el: HTMLElement,
  dx: number,
  dy: number,
): Promise<void> {
  if (prefersReducedMotion()) return;
  const distance = Math.hypot(dx, dy);
  if (distance < 1) return;

  const bow = Math.min(48, Math.max(8, distance * 0.12));
  // Perpendicular of (−dx, −dy), flipped when needed so the bow points upward.
  let nx = dy / distance;
  let ny = -dx / distance;
  if (ny > 0) {
    nx = -nx;
    ny = -ny;
  }

  const { duration, easing } = sampleSpring("carta");
  const animation = el.animate(
    [
      { transform: `translate(${dx}px, ${dy}px)` },
      {
        transform: `translate(${dx / 2 + nx * bow}px, ${dy / 2 + ny * bow}px) scale(1.03)`,
        offset: 0.5,
      },
      { transform: "none" },
    ],
    { duration, easing },
  );
  await animation.finished.catch(() => undefined);
}

/** Formats a value for {@link countUp}; defaults to `Math.round` + `String`. */
export type CountFormat = (value: number) => string;

/**
 * Animates a numeric text node from one value to another over ~300ms.
 *
 * The element's text is the single source of the rendered figure: the final
 * frame always writes the exact formatted target, so a dropped frame can never
 * leave a rounded-off lie on screen. Reduced motion writes the target at once.
 */
export function countUp(
  el: HTMLElement,
  from: number,
  to: number,
  format: CountFormat = (value) => String(Math.round(value)),
): void {
  const finish = (): void => {
    el.textContent = format(to);
  };
  if (prefersReducedMotion() || from === to) {
    finish();
    return;
  }
  const duration = 300;
  const start = performance.now();
  const step = (now: number): void => {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - (1 - t) ** 3;
    el.textContent = format(from + (to - from) * eased);
    if (t < 1) requestAnimationFrame(step);
    else finish();
  };
  requestAnimationFrame(step);
}

/**
 * The reject shake: an illegal drop or a refused action answers physically
 * (DESIGN.md §Motion, "Reject"). One ±4px oscillation pair, then rest.
 */
export async function shake(el: HTMLElement): Promise<void> {
  if (prefersReducedMotion()) return;
  const animation = el.animate(
    [
      { transform: "translateX(0)" },
      { transform: "translateX(-4px)" },
      { transform: "translateX(4px)" },
      { transform: "translateX(-3px)" },
      { transform: "translateX(3px)" },
      { transform: "translateX(0)" },
    ],
    { duration: 260, easing: "ease-out" },
  );
  await animation.finished.catch(() => undefined);
}
