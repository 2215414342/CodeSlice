import type { ReactNode } from 'react'
import { useEffect, useMemo, useRef } from 'react'
import * as api from '../api'
import { useAsync } from '../hooks/useAsync'
import type { GitCommit, ScmFile } from '../types'
import { formatRelative } from '../lib/format'
import { useApp } from '../state/AppState'
import { EmptyState, ErrorNote, Loading } from './Feedback'
import { Icon } from './Icon'

const STATUS_LETTER: Record<ScmFile['status'], string> = {
  added: 'A',
  modified: 'M',
  deleted: 'D',
  renamed: 'R',
  copied: 'C',
  conflicted: 'U',
}

/**
 * Source-control sidebar: a read-only mirror of VS Code's SCM view.
 *
 * Top half is "changes": the selected commit against its first parent, or the
 * work tree against HEAD. Bottom half is the commit timeline; clicking a row
 * moves the app's revision scope, so code, graph and notes follow together.
 *
 * There is deliberately no commit/stage/discard control here — the version
 * feature is read-only, and a button that looks like it writes would be a lie.
 */
export function ScmPanel(): ReactNode {
  const { project, revision, setRevision, openDiff, scmFocus, refreshKey } = useApp()
  const projectId = project?.id ?? null
  const rev = revision.kind === 'rev' ? revision.sha : null

  const changes = useAsync(
    (signal) => (projectId ? api.getScmChanges(projectId, rev, signal) : Promise.resolve(null)),
    [projectId, rev, refreshKey],
    { enabled: projectId !== null },
  )
  const commits = useAsync(
    (signal) => (projectId ? api.listGitCommits(projectId, { limit: 40 }, signal) : Promise.resolve(null)),
    [projectId, refreshKey],
    { enabled: projectId !== null },
  )

  const listRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!scmFocus?.path || !listRef.current) return
    const row = listRef.current.querySelector<HTMLElement>(`[data-scm-path="${CSS.escape(scmFocus.path)}"]`)
    row?.scrollIntoView({ block: 'nearest' })
  }, [scmFocus?.nonce, scmFocus?.path, changes.data])

  const rows = changes.data?.files ?? []
  const totals = useMemo(
    () => ({
      additions: changes.data?.additions ?? 0,
      deletions: changes.data?.deletions ?? 0,
    }),
    [changes.data],
  )

  if (projectId === null) {
    return <EmptyState icon="git-branch" title="还没有打开项目" hint="先打开一个本地 Git 仓库，再看它的更改。" />
  }

  return (
    <div className="sidebar-section scm-panel">
      <div className="sidebar-toolbar">
        <span className="sidebar-title">
          <Icon name="git-branch" size={13} />
          源代码管理
          <span className="badge mute">{rev ? rev.slice(0, 7) : '工作区'}</span>
        </span>
        <button
          type="button"
          className="icon"
          title="刷新更改与提交"
          aria-label="刷新更改与提交"
          onClick={() => {
            changes.reload()
            commits.reload()
          }}
        >
          <Icon name="refresh" size={12} />
        </button>
      </div>

      <div className="scm-scroll">
        <div className="scm-summary">
          {rev ? (
            <>
              <span className="muted">本次提交</span>
              <span className="scm-sha">{changes.data?.base_short ?? '空'}</span>
              <Icon name="arrow-right" size={11} />
              <span className="scm-sha accent">{rev.slice(0, 7)}</span>
            </>
          ) : (
            <>
              <span className="muted">未提交更改</span>
              <span className="scm-sha">{changes.data?.base_short ?? 'HEAD'}</span>
              <Icon name="arrow-right" size={11} />
              <span className="scm-sha accent">工作区</span>
            </>
          )}
          <span className="spacer" />
          <span className="scm-add">+{totals.additions}</span>
          <span className="scm-del">−{totals.deletions}</span>
        </div>

        {changes.loading ? <Loading text="读取更改…" /> : null}
        {changes.error ? <ErrorNote message={changes.error} /> : null}
        {changes.data && changes.data.is_repo === false ? (
          <div className="scm-empty">这个目录不是 Git 仓库</div>
        ) : null}

        {!changes.loading && changes.data && changes.data.is_repo !== false ? (
          <>
            <div className="scm-group-head">
              <Icon name="chevron-down" size={12} />
              <span>{rev ? '提交中的更改' : '更改'}</span>
              <span className="scm-count">{rows.length}</span>
            </div>
            <div className="scm-files" ref={listRef}>
              {rows.length === 0 ? (
                <div className="scm-empty">{rev ? '这次提交没有文件变更' : '工作区干净，没有未提交改动'}</div>
              ) : null}
              {rows.map((file) => {
                const focused = scmFocus?.path === file.path && (scmFocus.rev ?? null) === rev
                return (
                  <button
                    key={`${file.status}:${file.path}`}
                    type="button"
                    data-scm-path={file.path}
                    className={`scm-file${focused ? ' focused' : ''}`}
                    title={`${file.path}${file.old_path ? ` ← ${file.old_path}` : ''}`}
                    onClick={() => openDiff(file.path, rev ?? undefined)}
                  >
                    <span className={`scm-status ${file.status}`}>{STATUS_LETTER[file.status]}</span>
                    <span className="scm-path">
                      {file.old_path ? <span className="scm-old-path">{file.old_path} → </span> : null}
                      {file.path}
                    </span>
                    {file.binary ? (
                      <span className="muted">bin</span>
                    ) : (
                      <span className="scm-num">
                        {file.additions ? <span className="scm-add">+{file.additions}</span> : null}
                        {file.deletions ? <span className="scm-del">−{file.deletions}</span> : null}
                      </span>
                    )}
                  </button>
                )
              })}
            </div>
          </>
        ) : null}

        <div className="scm-group-head scm-timeline-head">
          <Icon name="chevron-down" size={12} />
          <span>提交时间线</span>
          <span className="spacer" />
          <button
            type="button"
            className={`scm-worktree${rev === null ? ' active' : ''}`}
            onClick={() => {
              setRevision({ kind: 'worktree' })
            }}
          >
            工作区
          </button>
        </div>

        {commits.loading ? <Loading text="读取提交…" /> : null}
        {commits.error ? <ErrorNote message={commits.error} /> : null}
        <div className="scm-timeline">
          {(commits.data?.commits ?? []).map((commit) => (
            <CommitRow
              key={commit.sha}
              commit={commit}
              active={commit.sha === rev}
              onClick={() => {
                setRevision({ kind: 'rev', sha: commit.sha, shaShort: commit.sha_short, name: commit.subject })
              }}
            />
          ))}
          {commits.data && commits.data.commits.length === 0 ? (
            <div className="scm-empty">这个仓库还没有提交</div>
          ) : null}
        </div>
      </div>
    </div>
  )
}

function CommitRow({
  commit,
  active,
  onClick,
}: {
  commit: GitCommit
  active: boolean
  onClick: () => void
}): ReactNode {
  const parents = commit.parents.length > 1
  return (
    <button type="button" className={`scm-commit${active ? ' active' : ''}`} onClick={onClick} title={commit.subject}>
      <span className="scm-node" aria-hidden="true" />
      <span className="scm-commit-main">
        <span className="scm-subject">{commit.subject}</span>
        <span className="scm-meta">
          <span className="scm-sha">{commit.sha_short}</span>
          <span>{commit.author}</span>
          <span>{formatRelative(commit.author_time)}</span>
          {parents ? <span className="badge mute">merge</span> : null}
        </span>
      </span>
    </button>
  )
}
