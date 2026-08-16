import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'

interface Props {
  children: ReactNode
  /** Short label for the surface that failed (e.g. "Settings"). */
  label?: string
  /** Optional reset hook (e.g. navigate back to hub). */
  onReset?: () => void
}

interface State {
  error: Error | null
}

/**
 * Catch render failures so a single pane cannot blank the whole shell.
 * Class component required — React has no hook equivalent for getDerivedStateFromError.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  /** Flip into the fallback UI when a child throws during render. */
  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  /** Log the failure once for diagnostics; avoid noisy re-logs on re-render. */
  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(`[Jarvis] ${this.props.label ?? 'UI'} crashed`, error, info.componentStack)
  }

  /** Clear the error and optionally run the caller's reset (navigation, reload). */
  private handleRetry = () => {
    this.setState({ error: null })
    this.props.onReset?.()
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    const label = this.props.label ?? 'This view'

    return (
      <div
        className="flex h-full flex-col items-start justify-center gap-3 px-8 py-10"
        role="alert"
      >
        <h2 className="text-sm font-semibold text-destructive">{label} failed to render</h2>
        <p className="max-w-md text-sm text-muted-foreground">
          {error.message || 'An unexpected error occurred.'}
        </p>
        <Button type="button" size="sm" variant="outline" onClick={this.handleRetry}>
          Retry
        </Button>
      </div>
    )
  }
}
