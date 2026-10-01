/**
 * Tiny module-scoped recorder for the most recent save operation.
 *
 * The Zustand store (useReview) tracks save *status* but not the arguments of
 * the last patch, so a "retry" affordance has nothing to re-run. Producers of
 * save operations (FieldRow, LineItemsTable) record a closure here that re-runs
 * the exact same call; the SaveIndicator in ReviewPage invokes it on click.
 *
 * Kept out of the store on purpose — it is transient UI plumbing, not state
 * that should trigger re-renders.
 */

type SaveRetry = () => void;

let lastSave: SaveRetry | null = null;

/** Record the action that should be re-run if the save fails. */
export function recordLastSave(retry: SaveRetry): void {
  lastSave = retry;
}

/** Re-run the last recorded save, if any. */
export function retryLastSave(): void {
  lastSave?.();
}

/** Whether a retryable save has been recorded this session. */
export function hasLastSave(): boolean {
  return lastSave !== null;
}
