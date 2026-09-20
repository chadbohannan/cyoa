// Shared by useStory and useRuns: wrap a mutation so a failed request lands in
// the shell's error line instead of vanishing in a promise. A failed call
// resolves to undefined so callers can bail.
export const guardWith =
  (setError: (e: string | null) => void) =>
  <A extends unknown[], R>(fn: (...a: A) => Promise<R>) =>
  async (...a: A): Promise<R | undefined> => {
    try {
      setError(null)
      return await fn(...a)
    } catch (e) {
      setError(String(e))
      return undefined
    }
  }
