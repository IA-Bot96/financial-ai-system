import { useApp } from '@/store'
import { Button } from './ui/Button'

/**
 * "Discard Changes?" confirmation shown when the user hits New (or File ▸ Open) with
 * unsaved edits. Cancel keeps them on the current page; Discard abandons the edits and
 * opens the upload screen.
 */
export function ConfirmDiscardModal() {
  const open = useApp((s) => s.confirmDiscard)
  const cancel = useApp((s) => s.cancelDiscard)
  const discard = useApp((s) => s.discardAndUpload)
  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 backdrop-blur-sm"
      onClick={cancel}
    >
      <div
        className="w-[420px] rounded-2xl bg-panel border border-line shadow-2xl p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold text-ink">Discard Changes?</h2>
        <p className="mt-2 text-sm text-muted">
          You have unsaved changes that will be lost if you continue.
        </p>
        <div className="mt-6 flex justify-end gap-3">
          <Button variant="subtle" onClick={cancel}>
            Cancel
          </Button>
          <Button
            className="bg-red-600 hover:bg-red-600/90 border-transparent"
            onClick={discard}
          >
            Discard
          </Button>
        </div>
      </div>
    </div>
  )
}
