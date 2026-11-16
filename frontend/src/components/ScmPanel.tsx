import type { MouseEvent, ReactNode } from 'react'
import { useEffect, useMemo, useRef, useState } from 'react'
import * as api from '../api'
import { useAsync } from '../hooks/useAsync'
import type { GitCommit, ScmFile } from '../types'
import { formatRelative } from '../lib/format'
import { useApp } from '../state/AppState'
import { EmptyState, ErrorNote, Loading } from './Feedback'
import { Icon } from './Icon'
import { ScmGraph, layoutCommitGraph } from './ScmGraph'

const STATUS_LETTER: Record<ScmFile['status'], string> = {
  added: 'A',
  modified: 'M',
  deleted: 'D',
  renamed: 'R',
  copied: 'C',
  conflicted: 'U',
}

const STATUS_LABEL: Record<ScmFile['status'], string> = {
  added: '新增',
  modified: '修改',
  deleted: '删除',
  renamed: '重命名',
  copied: '复制',
  conflicted: '冲突',
}

const HOVER_CARD_WIDTH = 296

type HoverInput = { kind: 'commit'; commit: GitCommit } | { kind: 'file'; file: ScmFile }
type HoverTarget = HoverInput & { x: number; y: number }

interface CommitStats {
  files: number
  additions: number
  deletions: number
}

/**
 * Source-control sidebar: a read-only mirror of VS Code's SCM view.
 *
 * The history rows deliberately carry only the graph and the commit subject.
 * Author, hash, date and diff size live in a hover card, the same split VS
 * Code uses (`setLabel(subject, author)` plus `setupDelayedHover`).
 */
export function ScmPanel(): ReactNode {
  const { project, revision, setRevision, openDiff, scmFocus, refreshKey } = useApp()
  const projectId = project?.id ?? null
  const scopeRev = revision.kind === 'rev' ? revision.sha : null
  // A note jump opens a historical file beside the worktree; the sidebar follows
  // that transient focus without moving the app's global scope.
  const rev = scmFocus ? scmFocus.rev : scopeRev

  const changes = useAsync(
    (signal) => (projectId ? api.getScmChanges(projectId, rev, signal) : Promise.resolve(null)),
    [projectId, rev, refreshKey],
    { enabled: projectId !== null },
  )
  const commits = useAsync(
    (signal) => (projectId ? api.listGitCommits(projectId, { limit: 50, topo: true }, signal) : Promise.resolve(null)),
    [projectId, refreshKey],
    { enabled: projectId !== null },
  )

  const [hover, setHover] = useState<HoverTarget | null>(null)
  const [commitStats, setCommitStats] = useState<Record<string, CommitStats>>({})
  const pendingStats = useRef(new Set<string>())
  const hoverCooldown = useRef(0)

  const hoveredSha = hover?.kind === 'commit' ? hover.commit.sha : null
  useEffect(() => {
    if (!projectId || !hoveredSha) return
    if (commitStats[hoveredSha] || pendingStats.current.has(hoveredSha)) return
    pendingStats.current.add(hoveredSha)
    api
      .getScmChanges(projectId, hoveredSha)
      .then((data) => {
        setCommitStats((current) => ({
          ...current,
          [hoveredSha]: {
            files: data.files.length,
            additions: data.additions,
            deletions: data.deletions,
          },
        }))
      })
      .catch(() => {
        pendingStats.current.delete(hoveredSha)
      })
  }, [commitStats, hoveredSha, projectId])

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

  const graph = useMemo(() => layoutCommitGraph(commits.data?.commits ?? []), [commits.data])
  const graphWidth = Math.max(24, (Math.min(6, Math.max(1, graph.laneCount)) + 1) * 11 + 4)

  if (projectId === null) {
    return <EmptyState icon="git-branch" title="还没有打开项目" hint="先打开一个本地 Git 仓库，再看它的更改。" />
  }

  const showHover = (event: MouseEvent<HTMLElement>, target: HoverInput) => {
    if (changes.loading || Date.now() < hoverCooldown.current) return
    const rect = event.currentTarget.getBoundingClientRect()
    const preferred = rect.right + 8
    const rightmost = Math.max(8, window.innerWidth - HOVER_CARD_WIDTH - 8)
    const x = preferred + HOVER_CARD_WIDTH <= window.innerWidth - 8 ? preferred : rightmost
    const y = Math.min(Math.max(8, rect.top - 4), Math.max(8, window.innerHeight - 232))
    setHover({ ...target, x, y })
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

      <div className="scm-scroll" onScroll={() => setHover(null)}>
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
                const name = fileBaseName(file.path)
                const dir = fileDirName(file.path)
                return (
                  <button
                    key={`${file.status}:${file.path}`}
                    type="button"
                    data-scm-path={file.path}
                    className={`scm-file${focused ? ' focused' : ''}`}
                    onClick={() => openDiff(file.path, rev ?? undefined)}
                    onMouseEnter={(event) => showHover(event, { kind: 'file', file })}
                    onMouseLeave={() => setHover(null)}
                  >
                    <span className={`scm-status ${file.status}`}>{STATUS_LETTER[file.status]}</span>
                    <span className="scm-file-name">{name}</span>
                    {dir ? <span className="scm-file-dir">{dir}</span> : null}
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
        <div className="scm-timeline" onMouseLeave={() => setHover(null)}>
          {graph.rows.map((row, index) => (
            <CommitRow
              key={row.commit.sha}
              row={row}
              width={graphWidth}
              head={index === 0}
              active={row.commit.sha === rev}
              onHover={(event) => showHover(event, { kind: 'commit', commit: row.commit })}
              onLeave={() => setHover(null)}
              onClick={() => {
                hoverCooldown.current = Date.now() + 2000
                setHover(null)
                setRevision({
                  kind: 'rev',
                  sha: row.commit.sha,
                  shaShort: row.commit.sha_short,
                  name: row.commit.subject,
                })
              }}
            />
          ))}
          {commits.data && commits.data.commits.length === 0 ? (
            <div className="scm-empty">这个仓库还没有提交</div>
          ) : null}
        </div>
      </div>

      {hover ? (
        <div
          className="scm-hover-card"
          style={{ left: hover.x, top: hover.y, width: HOVER_CARD_WIDTH }}
          role="tooltip"
          aria-live="polite"
        >
          {hover.kind === 'commit' ? (
            <CommitHover commit={hover.commit} stats={commitStats[hover.commit.sha]} />
          ) : (
            <FileHover file={hover.file} />
          )}
        </div>
      ) : null}
    </div>
  )
}

function CommitRow({
  row,
  width,
  head,
  active,
  onHover,
  onLeave,
  onClick,
}: {
  row: ReturnType<typeof layoutCommitGraph>['rows'][number]
  width: number
  head: boolean
  active: boolean
  onHover: (event: MouseEvent<HTMLElement>) => void
  onLeave: () => void
  onClick: () => void
}): ReactNode {
  const { commit } = row
  return (
    <button
      type="button"
      className={`scm-commit${active ? ' active' : ''}`}
      onClick={onClick}
      onMouseEnter={onHover}
      onMouseLeave={onLeave}
      aria-label={`${commit.subject}，${commit.author}，${formatRelative(commit.author_time)}`}
    >
      <ScmGraph row={row} width={width} head={head} selected={active} />
      <span className="scm-subject">{commit.subject}</span>
    </button>
  )
}

function CommitHover({ commit, stats }: { commit: GitCommit; stats?: CommitStats }): ReactNode {
  return (
    <>
      <div className="scm-hover-title">{commit.subject}</div>
      <div className="scm-hover-meta">
        <span>{commit.author}</span>
        <span className="scm-hover-dot">·</span>
        <span>{formatRelative(commit.author_time)}</span>
      </div>
      <div className="scm-hover-time">{formatAbsoluteTime(commit.author_time)}</div>
      <div className="scm-hover-sha">{commit.sha}</div>
      {commit.parents.length > 1 ? (
        <div className="scm-hover-note">合并提交 · {commit.parents.length} 个父提交</div>
      ) : null}
      <div className="scm-hover-stats">
        {stats ? (
          <>
            <span>{stats.files} 个文件</span>
            <span className="scm-add">+{stats.additions}</span>
            <span className="scm-del">−{stats.deletions}</span>
          </>
        ) : (
          <span className="muted">读取改动…</span>
        )}
      </div>
      <div className="scm-hover-hint">点击切换代码、图谱与笔记到此提交</div>
    </>
  )
}

function FileHover({ file }: { file: ScmFile }): ReactNode {
  const name = fileBaseName(file.path)
  return (
    <>
      <div className="scm-hover-title">{name}</div>
      <div className="scm-hover-path">
        {file.old_path ? (
          <>
            <span className="scm-old-path">{file.old_path}</span>
            <Icon name="arrow-right" size={10} />
            <span>{file.path}</span>
          </>
        ) : (
          file.path
        )}
      </div>
      <div className="scm-hover-stats">
        <span>{STATUS_LABEL[file.status]}</span>
        {file.binary ? (
          <span className="muted">二进制文件</span>
        ) : (
          <>
            <span className="scm-add">+{file.additions}</span>
            <span className="scm-del">−{file.deletions}</span>
          </>
        )}
      </div>
      <div className="scm-hover-hint">点击打开并排差异</div>
    </>
  )
}

function fileBaseName(path: string): string {
  const slash = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  return slash >= 0 ? path.slice(slash + 1) : path
}

function fileDirName(path: string): string {
  const slash = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  return slash > 0 ? path.slice(0, slash) : ''
}

function formatAbsoluteTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}
