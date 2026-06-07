import { Button } from './ui/Button'

export function ErrorScreen({
  logPath,
  onRetry
}: {
  logPath: string
  onRetry: () => void
}) {
  return (
    <div className="h-full w-full flex flex-col items-center justify-center gap-4 bg-bg px-10 text-center">
      <div className="h-12 w-12 rounded-xl bg-red-500/15 border border-red-500/40 flex items-center justify-center text-red-400 text-2xl">
        !
      </div>
      <div className="text-lg font-semibold">The analysis engine didn’t start</div>
      <p className="text-sm text-muted max-w-md">
        The local FIE backend failed to become ready. Check the backend log for details, then
        retry.
      </p>
      {logPath && (
        <code className="text-xs bg-panel border border-line rounded px-2 py-1 text-muted break-all max-w-lg">
          {logPath}
        </code>
      )}
      <Button onClick={onRetry} className="mt-2">
        Retry
      </Button>
    </div>
  )
}
