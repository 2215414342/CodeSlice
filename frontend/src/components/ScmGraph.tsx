import type { ReactNode } from 'react'
import { useMemo } from 'react'
import type { GitCommit } from '../types'

/**
 * VS Code's SCM history graph geometry, kept deliberately small:
 * 22px rows, 11px lanes, 5px curves, 4px commit dots. The commit list is laid
 * out into input/output swimlanes first, then each row draws only the segment
 * that belongs to it, so consecutive SVGs join into one continuous rail.
 */
const LANE_WIDTH = 11
const ROW_HEIGHT = 22
const CURVE_RADIUS = 5
const DOT_RADIUS = 4
const PALETTE_SIZE = 6

export interface GraphLane {
  id: string
  color: number
}

export interface GraphRow {
  commit: GitCommit
  input: GraphLane[]
  output: GraphLane[]
  circleIndex: number
  circleColor: number
}

export interface GraphLayout {
  rows: GraphRow[]
  laneCount: number
}

export function layoutCommitGraph(commits: GitCommit[]): GraphLayout {
  let lanes: GraphLane[] = []
  let nextColor = 0
  let laneCount = 0
  const rows: GraphRow[] = []

  const takeColor = () => {
    const color = nextColor % PALETTE_SIZE
    nextColor += 1
    return color
  }

  for (const commit of commits) {
    const input = lanes.map((lane) => ({ ...lane }))
    const inputIndex = input.findIndex((lane) => lane.id === commit.sha)
    const circleIndex = inputIndex >= 0 ? inputIndex : input.length
    let circleColor = input[circleIndex]?.color ?? takeColor()
    const output: GraphLane[] = []
    let firstParentAdded = false

    if (commit.parents.length > 0) {
      for (const lane of input) {
        if (lane.id === commit.sha) {
          if (!firstParentAdded) {
            output.push({ id: commit.parents[0], color: circleColor })
            firstParentAdded = true
          }
          continue
        }
        output.push(lane)
      }
    }

    for (let index = firstParentAdded ? 1 : 0; index < commit.parents.length; index += 1) {
      const parentId = commit.parents[index]
      if (output.some((lane) => lane.id === parentId)) continue
      output.push({ id: parentId, color: takeColor() })
    }

    circleColor = output[circleIndex]?.color ?? input[circleIndex]?.color ?? circleColor
    laneCount = Math.max(laneCount, input.length, output.length, circleIndex + 1)
    rows.push({ commit, input, output, circleIndex, circleColor })
    lanes = output
  }

  return { rows, laneCount }
}

function laneX(index: number): number {
  return LANE_WIDTH * (index + 1)
}

function laneColor(color: number): string {
  return `var(--scm-lane-${color + 1})`
}

export function ScmGraph({
  row,
  width,
  head,
  selected,
}: {
  row: GraphRow
  width: number
  head: boolean
  selected: boolean
}): ReactNode {
  const paths = useMemo(() => {
    const segments: ReactNode[] = []
    const { commit, input, output, circleIndex, circleColor } = row
    let outputIndex = 0

    input.forEach((lane, index) => {
      if (lane.id === commit.sha) {
        if (index !== circleIndex) {
          // Base commit: the lane bends left into the commit dot.
          const x = laneX(index)
          segments.push(
            <path
              key={`base-${index}`}
              d={`M ${x} 0 A ${LANE_WIDTH} ${LANE_WIDTH} 0 0 1 ${x - LANE_WIDTH} ${ROW_HEIGHT / 2} H ${laneX(circleIndex)}`}
              stroke={laneColor(lane.color)}
            />,
          )
        }
        return
      }

      const next = output[outputIndex]
      if (next && next.id === lane.id) {
        if (index === outputIndex) {
          segments.push(
            <path
              key={`straight-${index}`}
              d={`M ${laneX(index)} 0 V ${ROW_HEIGHT}`}
              stroke={laneColor(lane.color)}
            />,
          )
        } else {
          // Lane collapses into an adjacent rail with two soft quarter curves.
          const x = laneX(index)
          const target = laneX(outputIndex)
          segments.push(
            <path
              key={`bend-${index}`}
              d={`M ${x} 0 V 6 A ${CURVE_RADIUS} ${CURVE_RADIUS} 0 0 1 ${x - CURVE_RADIUS} ${ROW_HEIGHT / 2} H ${target + CURVE_RADIUS} A ${CURVE_RADIUS} ${CURVE_RADIUS} 0 0 0 ${target} ${ROW_HEIGHT / 2 + CURVE_RADIUS} V ${ROW_HEIGHT}`}
              stroke={laneColor(lane.color)}
            />,
          )
        }
        outputIndex += 1
      }
    })

    // Additional parents branch down-right from the merge commit.
    for (let parentIndex = 1; parentIndex < commit.parents.length; parentIndex += 1) {
      let parentLane = -1
      for (let laneIndex = row.output.length - 1; laneIndex >= 0; laneIndex -= 1) {
        if (row.output[laneIndex].id === commit.parents[parentIndex]) {
          parentLane = laneIndex
          break
        }
      }
      if (parentLane < 0) continue
      const x = LANE_WIDTH * parentLane
      segments.push(
        <path
          key={`parent-${parentIndex}`}
          d={`M ${x} ${ROW_HEIGHT / 2} H ${laneX(circleIndex)} M ${x} ${ROW_HEIGHT / 2} A ${LANE_WIDTH} ${LANE_WIDTH} 0 0 1 ${laneX(parentLane)} ${ROW_HEIGHT}`}
          stroke={laneColor(row.output[parentLane].color)}
        />,
      )
    }

    if (input.length > 0) {
      const inputIndex = input.findIndex((lane) => lane.id === commit.sha)
      if (inputIndex >= 0) {
        segments.push(
          <path
            key="to-dot"
            d={`M ${laneX(circleIndex)} 0 V ${ROW_HEIGHT / 2}`}
            stroke={laneColor(input[inputIndex].color)}
          />,
        )
      }
    }

    if (commit.parents.length > 0) {
      segments.push(
        <path
          key="from-dot"
          d={`M ${laneX(circleIndex)} ${ROW_HEIGHT / 2} V ${ROW_HEIGHT}`}
          stroke={laneColor(circleColor)}
        />,
      )
    }

    return segments
  }, [row])

  const ring = head || selected

  return (
    <svg
      className="scm-graph"
      width={width}
      height={ROW_HEIGHT}
      viewBox={`0 0 ${width} ${ROW_HEIGHT}`}
      aria-hidden="true"
    >
      {paths}
      {ring ? (
        <>
          <circle
            cx={laneX(row.circleIndex)}
            cy={ROW_HEIGHT / 2}
            r={DOT_RADIUS + 2}
            fill="var(--scm-panel-bg)"
            stroke={laneColor(row.circleColor)}
            strokeWidth={2}
          />
          <circle
            cx={laneX(row.circleIndex)}
            cy={ROW_HEIGHT / 2}
            r={DOT_RADIUS - 2}
            fill={laneColor(row.circleColor)}
            stroke="none"
          />
        </>
      ) : (
        <circle
          cx={laneX(row.circleIndex)}
          cy={ROW_HEIGHT / 2}
          r={DOT_RADIUS}
          fill={laneColor(row.circleColor)}
          stroke="var(--scm-panel-bg)"
          strokeWidth={2}
        />
      )}
    </svg>
  )
}
