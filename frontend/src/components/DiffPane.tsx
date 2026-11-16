import { DiffEditor, loader } from '@monaco-editor/react'
import type { editor as MonacoEditor } from 'monaco-editor'
import type { ReactNode } from 'react'
import { useCallback, useEffect, useRef } from 'react'
import * as api from '../api'
import { useAsync } from '../hooks/useAsync'
import { useApp } from '../state/AppState'
import type { EditorTab } from '../state/AppState'
import { ErrorNote, Loading } from './Feedback'
import { Icon } from './Icon'

loader.config({ paths: { vs: './monaco/vs' } })

type DiffTab = Extract<EditorTab, { kind: 'diff' }>

/**
 * Two-sided read-only diff for one path.
 *
 * The header is the "framed" title the user asked for:
 * `file (oldSha) ↔ file (newSha)`, so it is obvious which two snapshots are on
 * screen. A note jump carries a line range, which is revealed and decorated on
 * the modified side after mount.
 */
export function DiffPane({ tab }: { tab: DiffTab }): ReactNode {
  const { project, theme } = useApp()
  const projectId = project?.id ?? null
  const pair = useAsync(
    (signal) => (projectId ? api.getScmPair(projectId, tab.path, tab.rev, signal) : Promise.resolve(null)),
    [projectId, tab.path, tab.rev],
    { enabled: projectId !== null },
  )

  const editorRef = useRef<MonacoEditor.IStandaloneDiffEditor | null>(null)
  const decorationRef = useRef<string[]>([])
  const highlight = tab.highlight

  const applyHighlight = useCallback(() => {
    const editor = editorRef.current
    const modified = editor?.getModifiedEditor()
    if (!modified || !highlight) return
    const model = modified.getModel()
    if (!model) return
    const start = Math.max(1, Math.min(highlight.start, model.getLineCount()))
    const end = Math.max(start, Math.min(highlight.end, model.getLineCount()))
    modified.revealLinesInCenter(start, end)
    decorationRef.current = modified.deltaDecorations(decorationRef.current, [
      {
        range: { startLineNumber: start, startColumn: 1, endLineNumber: end, endColumn: 1 },
        options: { isWholeLine: true, className: 'diff-note-highlight' },
      },
    ])
  }, [highlight])

  useEffect(() => {
    // Runs after the pair finishes loading, when the models exist.
    if (pair.data) applyHighlight()
  }, [pair.data, applyHighlight])

  const name = tab.path.split('/').pop() || tab.path
  const oldLabel = pair.data?.old_label ?? '…'
  const newLabel = pair.data?.new_label ?? '…'

  return (
    <div className="diff-pane">
      <div className="diff-titlebar">
        <Icon name="git-branch" size={13} />
        <span className="diff-side old">
          <span className="diff-file">{name}</span>
          <span className="diff-sha">({oldLabel})</span>
        </span>
        <span className="diff-arrow" aria-hidden="true">
          <Icon name="arrow-right" size={12} />
        </span>
        <span className="diff-side new">
          <span className="diff-file">{name}</span>
          <span className="diff-sha">({newLabel})</span>
        </span>
        <span className="spacer" />
        {tab.highlight ? <span className="badge warn">笔记引用 L{tab.highlight.start}–{tab.highlight.end}</span> : null}
        <span className="muted diff-path">{tab.path}</span>
      </div>

      {pair.loading ? <Loading text="读取两个版本…" /> : null}
      {pair.error ? <ErrorNote message={pair.error} /> : null}
      {pair.data ? (
        pair.data.binary ? (
          <div className="diff-binary">二进制文件（{pair.data.old_label} ↔ {pair.data.new_label}），不展示文本差异</div>
        ) : !pair.data.old_exists && !pair.data.new_exists ? (
          <div className="diff-binary">这个路径在两边的版本里都不存在</div>
        ) : (
          <div className="diff-editor-host">
            {pair.data.truncated ? <div className="diff-warning">文件较大，内容已截断</div> : null}
            <DiffEditor
              className="editor-surface diff-surface"
              original={pair.data.old_content}
              modified={pair.data.new_content}
              language={pair.data.language}
              theme={theme === 'dark' ? 'codeslice-dark' : 'codeslice-light'}
              onMount={(editor) => {
                editorRef.current = editor
                applyHighlight()
              }}
              options={{
                readOnly: true,
                originalEditable: false,
                renderSideBySide: true,
                enableSplitViewResizing: true,
                renderOverviewRuler: false,
                minimap: { enabled: false },
                lineNumbersMinChars: 4,
                scrollBeyondLastLine: false,
                automaticLayout: true,
                diffWordWrap: 'on',
                ignoreTrimWhitespace: false,
              }}
            />
          </div>
        )
      ) : null}
    </div>
  )
}
