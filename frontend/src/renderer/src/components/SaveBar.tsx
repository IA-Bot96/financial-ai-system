import { useApp } from '@/store'
import { Button } from './ui/Button'

/** Top, full-width unsaved-changes bar (frontend-spec §11). */
export function SaveBar() {
  const { workbook, save, openWorkbookPath } = useApp()
  if (!workbook.dirty) return null
  return (
    <div className="h-10 shrink-0 flex items-center gap-3 px-4 bg-amber-500/10 border-b border-amber-500/30 text-sm">
      <span className="text-amber-300">You have unsaved changes.</span>
      <div className="flex-1" />
      <Button
        variant="subtle"
        onClick={() => {
          if (workbook.filePath) openWorkbookPath(workbook.filePath, workbook.origin ?? 'excel')
        }}
      >
        Discard
      </Button>
      <Button onClick={() => save()}>Save</Button>
    </div>
  )
}
