import React, { memo } from 'react'
import { Box } from '@hermes/ink'
import { Banner, Panel, SessionPanel } from './branding.js'
import { MessageLine } from './messageLine.js'
import type { Msg } from '../types.js'

export interface TranscriptRowViewProps {
  msg: Msg
  cols: number
  bodyCols?: number
  theme: any
  sid?: string | null
  compact?: boolean
  detailsMode?: any
  detailsModeCommandOverride?: any
  sections?: any
  timestamps?: boolean
  prev?: Msg
}

export const TranscriptRowView = memo(function TranscriptRowView({
  msg,
  cols,
  bodyCols = cols,
  theme,
  sid,
  compact = false,
  detailsMode,
  detailsModeCommandOverride,
  sections,
  timestamps = false,
  prev
}: TranscriptRowViewProps) {
  if (msg.kind === 'intro') {
    return (
      <Box flexDirection="column" paddingTop={1}>
        <Banner maxWidth={Math.max(1, cols - 2)} t={theme} />
        {msg.info && (
          <SessionPanel info={msg.info} maxWidth={Math.max(1, cols - 2)} sid={sid} t={theme} />
        )}
      </Box>
    )
  }

  if (msg.kind === 'panel' && msg.panelData) {
    return (
      <Panel sections={msg.panelData.sections} t={theme} title={msg.panelData.title} />
    )
  }

  return (
    <MessageLine
      cols={bodyCols}
      compact={compact}
      detailsMode={detailsMode}
      detailsModeCommandOverride={detailsModeCommandOverride}
      msg={msg}
      prev={prev}
      sections={sections}
      t={theme}
      timestamps={timestamps}
    />
  )
})
