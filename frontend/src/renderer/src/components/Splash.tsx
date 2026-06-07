export function Splash({ message }: { message: string }) {
  return (
    <div className="h-full w-full flex flex-col items-center justify-center gap-5 bg-bg">
      <div className="h-12 w-12 rounded-xl bg-accent/15 border border-accent/40 flex items-center justify-center">
        <span className="text-accent text-xl font-bold">FI</span>
      </div>
      <div className="text-center">
        <div className="text-lg font-semibold">AI Financial Intelligence</div>
        <div className="mt-1 text-sm text-muted flex items-center gap-2">
          <span className="h-3 w-3 rounded-full border-2 border-muted border-t-transparent animate-spin" />
          {message}
        </div>
      </div>
    </div>
  )
}
