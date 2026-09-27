import { useEffect, useMemo, useState } from 'react'
import { connectStream, type ConnectionStatus } from './client'
import { applyMessage, emptyFleet, newConnection } from './fleet'
import { replayAt, type Recording } from './recording'

export function useTelemetry(
  mode: 'replay' | 'live',
  recording: Recording | null,
  base: string,
) {
  const [liveFleet, setLiveFleet] = useState(emptyFleet)
  const [connection, setConnection] = useState<ConnectionStatus>('disconnected')
  const [error, setError] = useState<string | null>(null)
  const [retry, setRetry] = useState(0)
  const [now, setNow] = useState(Date.now)
  const [offset, setOffset] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const duration = recording ? (recording.end - recording.start) / 1000 : 0
  const isPlaying = playing && offset < duration
  useEffect(() => {
    if (mode !== 'live') return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    let stop = () => {}
    let active = true
    try {
      stop = connectStream(base, {
        onConnection: (status) => {
          if (!active) return
          setConnection(status)
          if (status === 'connected') setError(null)
          if (status === 'connecting') setLiveFleet(newConnection)
        },
        onMessage: (message) => {
          if (active)
            setLiveFleet((old) => applyMessage(old, message, Date.now()))
        },
        onError: (message) => {
          if (active) setError(message)
        },
      })
    } catch (e) {
      queueMicrotask(() => {
        if (active)
          setError(e instanceof Error ? e.message : 'Неверный адрес API.')
      })
    }
    return () => {
      active = false
      stop()
      window.clearInterval(timer)
    }
  }, [mode, base, retry])
  useEffect(() => {
    if (!isPlaying || mode !== 'replay' || !duration) return
    let previous = performance.now()
    const timer = window.setInterval(() => {
      const current = performance.now(),
        elapsed = Math.max(0, current - previous) / 1000
      previous = current
      setOffset((old) => Math.min(duration, old + elapsed * speed))
    }, 100)
    return () => window.clearInterval(timer)
  }, [isPlaying, mode, duration, speed])
  const at = mode === 'replay' ? (recording?.start ?? now) + offset * 1000 : now
  const fleet = useMemo(
    () =>
      mode === 'live'
        ? liveFleet
        : recording
          ? replayAt(recording, at)
          : emptyFleet(),
    [mode, liveFleet, recording, at],
  )
  return {
    fleet,
    at,
    offset,
    duration,
    speed,
    setSpeed,
    connection,
    error: mode === 'live' ? error : null,
    playing: isPlaying,
    play() {
      if (offset >= duration) setOffset(0)
      setPlaying((p) => (offset >= duration ? true : !p))
    },
    seek(value: number) {
      setPlaying(false)
      setOffset(Math.max(0, Math.min(duration, value)))
    },
    reconnect() {
      setError(null)
      setRetry((x) => x + 1)
    },
  }
}
