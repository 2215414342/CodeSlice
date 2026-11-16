import type { ReactNode } from 'react'
import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from '../api'
import type { GitBranches, GitCommit } from '../types'
import { useApp } from '../state/AppState'
import { Icon } from './Icon'

/**
 * The single control that decides which revision the app reads from.
 *
 * A branch is only an entry point: picking one resolves its tip to a full sha,
 * so a branch that moves later cannot silently change what is on screen. Tags
 * and raw shas are the same operation with a different name.
 */
export function GitVersionPicker(): ReactNode {
  const { project, revision, setRevision } = useApp()
  const projectId = project?.id ?? null
  const [open, setOpen] = useState(false)
  const [branches, setBranches] = useState<GitBranches | null>(null)
  const [head, setHead] = useState<{ current: string | null; detached: boolean } | null>(null)
  const [commits, setCommits] = useState<GitCommit[]>([])
  const [sha, setSha] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)

  const load = useCallback(async () => {
    if (projectId === null) return
    setBusy(true)
    setError(null)
    try {
      const [refs, log] = await Promise.all([
        api.getGitBranches(projectId),
        api.listGitCommits(projectId, { limit: 15 }),
      ])
      setBranches(refs)
      setHead({ current: refs.current, detached: refs.detached })
      setCommits(log.commits)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [projectId])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  // A click anywhere else closes the menu; so does Escape.
  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  // A different project has a different history; drop what we cached.
  useEffect(() => {
    setBranches(null)
    setHead(null)
    setCommits([])
    setError(null)
    setSha('')
  }, [projectId])

  // The trigger names the branch the work tree is on, so read it before the
  // menu is opened. A failed git read simply leaves the plain "工作区" label.
  useEffect(() => {
    if (projectId === null) {
      setHead(null)
      return
    }
    const controller = new AbortController()
    api
      .getGitBranches(projectId, controller.signal)
      .then((refs) => setHead({ current: refs.current, detached: refs.detached }))
      .catch(() => {
        if (!controller.signal.aborted) setHead(null)
      })
    return () => controller.abort()
  }, [projectId])

  const pin = async (ref: string, name?: string) => {
    if (projectId === null) return
    setBusy(true)
    setError(null)
    try {
      const resolved = await api.resolveGitRev(projectId, ref)
      setRevision({
        kind: 'rev',
        sha: resolved.sha,
        shaShort: resolved.sha_short,
        name: name ?? resolved.name,
      })
      setOpen(false)
      setSha('')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  if (projectId === null) return null

  const branch = branches ? branches.current : head?.current ?? null
  const detached = branches ? branches.detached : Boolean(head?.detached)
  const worktreeName = branch ?? (detached ? '游离 HEAD' : null)
  const label =
    revision.kind === 'worktree'
      ? worktreeName
        ? `工作区 · ${worktreeName}`
        : '工作区'
      : revision.name ?? revision.shaShort
  const historical = revision.kind === 'rev'

  return (
    <div className="revision-picker" ref={rootRef}>
      <button
        className={`ghost small revision-trigger${historical ? ' active' : ''}`}
        title={
          historical
            ? `只读 · ${revision.sha}`
            : worktreeName
              ? `当前工作区：${worktreeName}（最新代码，可编辑）`
              : '当前显示工作区；可切换到某个分支或提交查看历史代码'
        }
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <Icon name="git-branch" size={12} />
        {label}
        <Icon name="chevron-down" size={10} />
      </button>

      {open ? (
        <div className="revision-menu" role="menu">
          <button
            className={`revision-item${revision.kind === 'worktree' ? ' current' : ''}`}
            title={worktreeName ? `最新代码，可编辑 · 当前分支 ${worktreeName}` : '最新代码，可编辑'}
            onClick={() => {
              setRevision({ kind: 'worktree' })
              setOpen(false)
            }}
            type="button"
          >
            <span className="revision-label">工作区{worktreeName ? ` · ${worktreeName}` : ''}</span>
            <span className="revision-meta">最新代码</span>
          </button>

          {busy && !branches ? <div className="revision-note">读取 Git 信息…</div> : null}
          {error ? <div className="revision-note error">{error}</div> : null}

          {branches && branches.branches.length > 0 ? (
            <>
              <div className="revision-group">分支</div>
              {branches.branches.map((branch) => (
                <button
                  key={`branch:${branch.name}`}
                  className="revision-item"
                  onClick={() => void pin(branch.sha, branch.name)}
                  title={`${branch.name} · ${branch.sha}`}
                  type="button"
                >
                  <span className="revision-label">{branch.name}</span>
                  {branch.current ? <em className="revision-chip">当前</em> : null}
                  <span className="revision-meta mono">{branch.sha_short}</span>
                </button>
              ))}
            </>
          ) : null}

          {branches && branches.tags.length > 0 ? (
            <>
              <div className="revision-group">标签</div>
              {branches.tags.map((tag) => (
                <button
                  key={`tag:${tag.name}`}
                  className="revision-item"
                  onClick={() => void pin(tag.sha, tag.name)}
                  title={`${tag.name} · ${tag.sha}`}
                  type="button"
                >
                  <span className="revision-label">{tag.name}</span>
                  <span className="revision-meta mono">{tag.sha_short}</span>
                </button>
              ))}
            </>
          ) : null}

          {commits.length > 0 ? (
            <>
              <div className="revision-group">最近提交</div>
              {commits.map((commit) => (
                <button
                  key={`commit:${commit.sha}`}
                  className="revision-item"
                  onClick={() => void pin(commit.sha, commit.subject)}
                  title={`${commit.subject}\n${commit.sha}\n${commit.author} · ${commit.author_time}`}
                  type="button"
                >
                  <span className="revision-label">{commit.subject}</span>
                  <span className="revision-meta mono">{commit.sha_short}</span>
                </button>
              ))}
            </>
          ) : null}

          <div className="revision-sha">
            <input
              value={sha}
              placeholder="输入提交 sha"
              onChange={(event) => setSha(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && sha.trim()) void pin(sha.trim())
              }}
            />
            <button
              className="primary small"
              disabled={busy || !sha.trim()}
              onClick={() => void pin(sha.trim())}
              type="button"
            >
              切换
            </button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
