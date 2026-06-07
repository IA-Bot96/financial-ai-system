import { ButtonHTMLAttributes } from 'react'
import { cn } from '@/lib/util'

type Variant = 'primary' | 'ghost' | 'subtle'

export function Button({
  variant = 'primary',
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  const base =
    'inline-flex items-center justify-center gap-2 rounded-lg text-sm font-medium px-3.5 py-2 ' +
    'transition-colors disabled:opacity-40 disabled:cursor-not-allowed focus:outline-none ' +
    'focus-visible:ring-2 focus-visible:ring-accent/60'
  const variants: Record<Variant, string> = {
    primary: 'bg-accent text-white hover:bg-accent/90',
    ghost: 'text-ink hover:bg-panel2',
    subtle: 'bg-panel2 text-ink border border-line hover:border-accent/60'
  }
  return <button className={cn(base, variants[variant], className)} {...props} />
}
