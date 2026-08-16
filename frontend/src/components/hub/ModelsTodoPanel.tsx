import { MODEL_TODO_ITEMS, modelTodoStatusLabel } from '@/lib/modelTodo'
import { cn } from '@/lib/utils'

/**
 * Full in-app Models TODO panel listing backend hooks and status for each
 * incomplete model / ingest path the user still needs to implement.
 */
export function ModelsTodoPanel() {
  return (
    <div className="flex h-full flex-col">
      <header className="shrink-0 border-b border-border px-6 py-3">
        <h1 className="font-display text-sm text-foreground">Models TODO</h1>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Checklist of models and ingest decisions still required. Mirrors{' '}
          <code className="text-[11px]">docs/MODEL_TODO.md</code>.
        </p>
      </header>
      <div className="scrollbar-thin flex-1 overflow-y-auto">
        <ul className="mx-auto flex w-full max-w-2xl flex-col gap-3 px-6 py-6">
          {MODEL_TODO_ITEMS.map((item) => (
            <li key={item.id} className="panel-ops p-4">
              <div className="flex items-start justify-between gap-3">
                <h2 className="text-sm font-medium">{item.title}</h2>
                <span
                  className={cn(
                    'font-mono text-[10px] tracking-wider',
                    item.status === 'open'
                      ? 'text-critical'
                      : item.status === 'partial'
                        ? 'text-warning'
                        : 'text-success',
                  )}
                >
                  {modelTodoStatusLabel(item.status)}
                </span>
              </div>
              <p className="mt-2 text-[12px] leading-relaxed text-foreground/85">{item.note}</p>
              <p className="mt-2 font-mono text-[10px] text-muted-foreground">{item.hook}</p>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
