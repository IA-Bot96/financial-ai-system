import { useApp } from '@/store'

/**
 * Top, full-width unsaved-changes indicator (frontend-spec §11). No buttons — editing,
 * undo (Ctrl+Z) and redo (Ctrl+Y) happen in the grid's own toolbar; saving is Ctrl+S /
 * File ▸ Save / Save As. The bar simply reflects dirty state: it appears on an edit/redo
 * and disappears when changes are undone back to the saved baseline or after a save.
 */
export function SaveBar() {
  const dirty = useApp((s) => s.workbook.dirty)
  if (!dirty) return null
  return (
    <div className="h-10 shrink-0 flex items-center gap-2 px-4 bg-amber-500/10 border-b border-amber-500/30 text-sm">
      {/* warning glyph (same as the warning toast), sized to the text + amber-tinted */}
      <svg
        width="18"
        height="18"
        viewBox="0 0 20 20"
        fill="none"
        className="shrink-0 text-amber-300"
        xmlns="http://www.w3.org/2000/svg"
      >
        <path
          d="M10 13.3333V10M10 6.66667H10.0083M18.3333 10C18.3333 14.6024 14.6024 18.3333 10 18.3333C5.39763 18.3333 1.66667 14.6024 1.66667 10C1.66667 5.39763 5.39763 1.66667 10 1.66667C14.6024 1.66667 18.3333 5.39763 18.3333 10Z"
          stroke="currentColor"
          strokeWidth="1.66667"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span className="text-amber-300">You have unsaved changes.</span>
      <span className="text-amber-300/60 text-xs">Ctrl+S to save · Ctrl+Z to undo</span>
    </div>
  )
}
