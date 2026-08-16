import { MODEL_TODO_ITEMS, modelTodoStatusLabel } from '@/lib/modelTodo'
import { useAppStore } from '@/stores/app'
import { cn } from '@/lib/utils'

/** Compact Models TODO strip for the hub; full list lives under Models view. */
export function ModelsTodoStrip() {
  const setView = useAppStore((s) => s.setView)
  const openCount = MODEL_TODO_ITEMS.filter((i) => i.status !== 'done').length

  return (
    <section className="panel-ops p-3">
      <header className="mb-2 flex items-center justify-between">
        <h2 className="font-display text-[10px] text-muted-foreground">Models TODO</h2>
        <span className="font-mono text-[9px] text-warning">{openCount} OPEN</span>
      </header>
      <ul className="space-y-1">
        {MODEL_TODO_ITEMS.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              onClick={() => setView('models')}
              className="flex w-full cursor-pointer items-center justify-between gap-2 border-b border-border/30 py-1 text-left last:border-0 hover:text-primary"
            >
              <span className="truncate text-[11px]">{item.title}</span>
              <span
                className={cn(
                  'shrink-0 font-mono text-[8px] tracking-wider',
                  item.status === 'open'
                    ? 'text-critical'
                    : item.status === 'partial'
                      ? 'text-warning'
                      : 'text-success',
                )}
              >
                {modelTodoStatusLabel(item.status)}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <button
        type="button"
        onClick={() => setView('models')}
        className="mt-2 cursor-pointer text-left text-[10px] text-muted-foreground underline-offset-2 hover:text-primary hover:underline"
      >
        Full notes in Models view and docs/MODEL_TODO.md
      </button>
    </section>
  )
}
