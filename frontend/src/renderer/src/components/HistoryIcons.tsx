// Icons for the History feature (provided SVGs, recolored to currentColor so they read on the
// dark theme). Stroke-based line icons + the two "empty state" emoji glyphs.

export function ArrowGlyph({ className = '' }: { className?: string }): JSX.Element {
  return (
    <svg width="15" height="11" viewBox="0 0 18 14" fill="none" className={`inline-block shrink-0 ${className}`} aria-hidden="true">
      <path d="M1 7H17M11 13L17 7L11 1" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function SearchIcon({ className = '' }: { className?: string }): JSX.Element {
  return (
    <svg viewBox="0 0 20 20" fill="none" className={className} aria-hidden="true">
      <path
        d="M19 19L13.0001 13M15 8C15 11.866 11.866 15 8 15C4.13401 15 1 11.866 1 8C1 4.13401 4.13401 1 8 1C11.866 1 15 4.13401 15 8Z"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function CrossIcon({ className = '' }: { className?: string }): JSX.Element {
  return (
    <svg viewBox="0 0 12 12" fill="none" className={className} aria-hidden="true">
      <path d="M11 1L1 11M1 1L11 11" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/** Clock with a small "rewind" arrow — the History toggle. */
export function ClockHistoryIcon({ className = '' }: { className?: string }): JSX.Element {
  return (
    <svg viewBox="0 0 22 20" fill="none" className={className} aria-hidden="true">
      <path
        d="M20.7 11.5L18.7005 9.5L16.7 11.5M19 10C19 14.9706 14.9706 19 10 19C5.02944 19 1 14.9706 1 10C1 5.02944 5.02944 1 10 1C13.3019 1 16.1885 2.77814 17.7545 5.42909M10 5V10L13 12"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

/** Sad document — "no history yet". */
export function SadFileIcon({ className = '' }: { className?: string }): JSX.Element {
  return (
    <svg viewBox="0 0 32 32" fill="none" className={className} aria-hidden="true">
      <g stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeMiterlimit={10}>
        <polyline points="21.5,1.5 4.5,1.5 4.5,30.5 27.5,30.5 27.5,7.5" />
        <polyline points="21.5,1.5 27.479,7.5 21.5,7.5 21.5,4" />
        <path d="M14.5,18.5c0-0.83,0.67-1.5,1.5-1.5s1.5,0.67,1.5,1.5" />
        <path d="M20.75,15.5c0,0.41,0.34,0.75,0.75,0.75s0.75-0.34,0.75-0.75s-0.34-0.75-0.75-0.75S20.75,15.09,20.75,15.5z" />
        <path d="M11.25,15.5c0,0.41-0.34,0.75-0.75,0.75s-0.75-0.34-0.75-0.75s0.34-0.75,0.75-0.75S11.25,15.09,11.25,15.5z" />
      </g>
      <path
        d="M21.5,14.75c0.41,0,0.75,0.34,0.75,0.75s-0.34,0.75-0.75,0.75s-0.75-0.34-0.75-0.75S21.09,14.75,21.5,14.75z"
        fill="currentColor"
      />
      <path
        d="M10.5,14.75c0.41,0,0.75,0.34,0.75,0.75s-0.34,0.75-0.75,0.75s-0.75-0.34-0.75-0.75S10.09,14.75,10.5,14.75z"
        fill="currentColor"
      />
    </svg>
  )
}

/** Sad magnifier — "nothing matches your search / filters". */
export function SadSearchIcon({ className = '' }: { className?: string }): JSX.Element {
  return (
    <svg viewBox="0 0 32 32" fill="none" className={className} aria-hidden="true">
      <g stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeMiterlimit={10}>
        <line x1="23.43" x2="21.214" y1="23.401" y2="21.186" />
        <path d="M29.914,27.086l-3.5-3.5c-0.756-0.756-2.072-0.756-2.828,0C23.208,23.964,23,24.466,23,25s0.208,1.036,0.586,1.414l3.5,3.5c0.378,0.378,0.88,0.586,1.414,0.586s1.036-0.208,1.414-0.586S30.5,29.034,30.5,28.5S30.292,27.464,29.914,27.086z" />
        <circle cx="13" cy="13" r="11.5" />
        <path d="M12,15.521c0-0.55,0.45-1,1-1s1,0.45,1,1" />
        <path d="M17.5,13c0.27,0,0.5,0.23,0.5,0.5S17.77,14,17.5,14S17,13.77,17,13.5S17.23,13,17.5,13z" />
        <path d="M8.5,13C8.77,13,9,13.23,9,13.5S8.77,14,8.5,14S8,13.77,8,13.5S8.23,13,8.5,13z" />
      </g>
      <path d="M17.5,13c0.27,0,0.5,0.23,0.5,0.5S17.77,14,17.5,14S17,13.77,17,13.5S17.23,13,17.5,13z" fill="currentColor" />
      <path d="M8.5,13C8.77,13,9,13.23,9,13.5S8.77,14,8.5,14S8,13.77,8,13.5S8.23,13,8.5,13z" fill="currentColor" />
    </svg>
  )
}
